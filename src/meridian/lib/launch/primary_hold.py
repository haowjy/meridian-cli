"""One primary row held by a process: launch slot, session scope, row, identity, outcome.

A hold owns only its own row. Process-wide concerns (signal masking, import warming)
belong to the caller, so one process can hold several rows and conclude each on its own.
The calls block; from an event loop, run them in a thread. A row's control route is
served with `serving_route` on whatever loop the caller owns.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.domain import PrimarySessionMetadata
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.spawn_start import SpawnStartMetadata
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.bundle import get_process_ports
from meridian.lib.launch.artifact_io import LifecycleLog, record_identity_failure
from meridian.lib.launch.continue_launch import ContinueHold
from meridian.lib.launch.extract import FinalizeExtraction, enrich_finalize
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.native_run import (
    NativeRun,
    NativeRunOutcome,
    bind_entry,
    bind_observed_entry,
    conclude_native_run,
)
from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.session_scope import SessionAttempt, session_scope
from meridian.lib.spawn.lifecycle import SpawnReservation
from meridian.lib.spawn.outcome import ExecutionTerminalFacts
from meridian.lib.spawn.service import SpawnApplicationService
from meridian.lib.state.launch_lock import LaunchSlot
from meridian.lib.state.native_identity import NativeIdentityError
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.session_store import update_session_spawn_id
from meridian.lib.state.spawn.model import FOREGROUND_LAUNCH_MODE, SpawnRecord
from meridian.lib.streaming.control_socket import control_socket_path

if TYPE_CHECKING:
    from pydantic import JsonValue

    from meridian.lib.harness.adapter import SubprocessHarness
    from meridian.lib.launch.context import LaunchContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrimaryRow:
    """What a primary row is told when its launch starts."""

    spawn_id: SpawnId
    session_metadata: PrimarySessionMetadata
    session_request: SessionRequest
    prompt: str
    control_root: Path
    execution_cwd: Path
    # The session's task checkout, for its record; the row's own is set once resolved.
    task_cwd: Path | None = None
    # Reopen the chat's session when set.
    resume_chat_id: str | None = None
    # A reopen carries this launch's explicit work; a fresh start gets the chat's.
    work_id: str | None = None
    harness_session_id: str = ""
    goal: str | None = None
    usage_seed: dict[str, JsonValue] | None = None
    # ``None`` for a row held from outside any runner: its liveness is the launch lock alone.
    runner_pid: int | None = None


def _reservation(row: PrimaryRow, *, chat_id: str | None, work_id: str | None) -> SpawnReservation:
    # A primary's goal is its conversation's identity, so a `--continue` inherits it
    # from the row.
    return SpawnReservation(
        chat_id=chat_id,
        session_metadata=row.session_metadata,
        spawn_id=str(row.spawn_id),
        kind="primary",
        prompt=row.prompt,
        harness_session_id=row.harness_session_id,
        control_root=str(row.control_root),
        task_cwd=None,
        execution_cwd=str(row.execution_cwd),
        launch_mode=FOREGROUND_LAUNCH_MODE,
        work_id=work_id,
        goal=row.goal,
        runner_pid=row.runner_pid,
        metadata=SpawnStartMetadata(usage_seed=row.usage_seed),
    )


class PrimaryHold:
    def __init__(
        self,
        row: PrimaryRow,
        *,
        spawn_service: SpawnApplicationService,
        slot: LaunchSlot,
        attempt: SessionAttempt,
        reopened: SpawnRecord | None,
    ) -> None:
        self._row = row
        self.spawn_service = spawn_service
        self._slot = slot
        self.attempt = attempt
        self.reopened = reopened
        self.launch: LaunchPaths | None = None
        self.log: LifecycleLog | None = None
        self.native_run: NativeRun | None = None

    @property
    def chat_id(self) -> str:
        return self.attempt.chat_id

    def start(self, *, work_id: str | None) -> LaunchPaths:
        """Write the row (a reopened one already is) and point the session at it."""
        if self.reopened is not None:
            launch = LaunchPaths.for_row(self.attempt.runtime_root, self.reopened)
        else:
            launch = self.spawn_service.lifecycle.start(
                _reservation(self._row, chat_id=self.chat_id, work_id=work_id),
                launch_lock=self._slot,
            )
        # The start event already names the spawn; the spawn's history ID only
        # exists once its row is written, and the session listing matches
        # archived primaries to their session by it.
        update_session_spawn_id(self.attempt.runtime_root, self.chat_id, str(launch.spawn_id))
        self.launch = launch
        self.log = LifecycleLog.for_launch(launch)
        return launch

    def bind_identity(
        self,
        spec: ResolvedLaunchSpec,
        *,
        harness: str,
        policy: LaunchPolicySnapshot | None,
    ) -> NativeRun:
        """Bind the native identity a launch spec assigns, before the harness runs."""
        self.native_run = bind_entry(self.attempt, spec, harness=harness, policy=policy)
        return self.native_run

    def bind_observed_identity(
        self,
        session_id: str,
        *,
        harness: str,
        policy: LaunchPolicySnapshot | None,
        observed_model: str | None = None,
    ) -> NativeRun:
        """Bind a native session the caller observed running: exact, nothing to assign.

        ``observed_model`` is the harness's name for the model it runs, which a continue
        replays."""
        self.native_run = bind_observed_entry(
            self.attempt, harness=harness, session_id=session_id, policy=policy
        )
        if observed_model:
            self.attempt.record_observed_model(
                harness=harness, harness_session_id=session_id, model=observed_model
            )
        return self.native_run

    def conclude_identity(
        self,
        adapter: SubprocessHarness,
        *,
        context: LaunchContext,
        child_env: Mapping[str, str],
        child_cwd: Path,
        pid: int | None,
        started: bool,
        started_at_epoch: float | None,
        prior_error: NativeIdentityError | None,
        prior_error_phase: str,
        facts: AttemptFacts,
        probe_turn: bool,
    ) -> NativeRunOutcome | None:
        """Conclude identity once, after the child, teardown and observer ended."""
        if self.native_run is not None and self.launch is not None and self.log is not None:
            return conclude_native_run(
                self.native_run,
                adapter,
                context=context,
                spawn_id=self.launch.spawn_id,
                child_env=child_env,
                child_cwd=child_cwd,
                pid=pid,
                started=started,
                started_at_epoch=started_at_epoch,
                prior_error=prior_error,
                prior_error_phase=prior_error_phase,
                facts=facts,
                connection_session_id=None,
                lifecycle=self.log,
                probe_turn=probe_turn,
            )
        if prior_error is not None and self.log is not None:
            record_identity_failure(prior_error, phase="pre_exec", lifecycle=self.log)
        return None

    def extract(
        self,
        harness_id: HarnessId,
        *,
        facts: AttemptFacts,
        policy: LaunchPolicySnapshot | None,
    ) -> FinalizeExtraction:
        """Read the harness's report and usage for the outcome; needs a launched harness."""
        assert self.launch is not None
        return enrich_finalize(
            extractor=get_process_ports(harness_id).extractor,
            facts=facts,
            native_key=self.native_run.entry.complete() if self.native_run else None,
            launch=self.launch,
            harness_id=harness_id,
            policy=policy,
        )

    def complete(
        self,
        *,
        exit_code: int,
        extraction: FinalizeExtraction | None,
        started_monotonic: float,
        cancellation_observed: bool,
        identity_error: NativeIdentityError | None,
        launch_failure: str | None,
    ) -> int:
        """Write the row's outcome, after identity is concluded; return its resolved exit code.

        ``extraction`` is ``None`` for a session whose harness leaves no report or usage.
        """
        launch = self.launch
        if launch is None:
            return exit_code
        duration = (
            max(0.0, time.monotonic() - started_monotonic) if started_monotonic > 0.0 else None
        )
        identity_failure = identity_error.failure_code if identity_error else None
        execution_outcome = asyncio.run(
            self.spawn_service.complete_execution(
                launch.spawn_id,
                ExecutionTerminalFacts(
                    exit_code=exit_code,
                    failure_reason=(
                        identity_failure
                        or ("cancelled" if cancellation_observed else launch_failure)
                    ),
                    cancellation_observed=cancellation_observed,
                    durable_report_completion=(
                        extraction is not None
                        and extraction.durable_report_completion
                        and identity_failure is None
                    ),
                ),
                origin="launcher",
                duration_secs=duration,
                usage=extraction.usage if extraction is not None else None,
            )
        )
        if not execution_outcome.completion.wrote:
            logger.info(
                "Launcher finalize skipped; spawn already terminal or missing: %s",
                launch.spawn_id,
            )
        return execution_outcome.resolved.exit_code


@contextmanager
def primary_hold(
    row: PrimaryRow,
    *,
    spawn_service: SpawnApplicationService,
    continue_hold: ContinueHold | None = None,
) -> Generator[PrimaryHold]:
    """Hold ``row``'s launch slot and session scope; a ``continue_hold`` reopens its row."""
    runtime_root = spawn_service.runtime_root
    reopened: SpawnRecord | None = None
    if continue_hold is not None:
        # The hold is the launch lock: any control socket here is a dead launcher's.
        # Removing it before the spawn reads as active sends a sender to the
        # normal poll instead of a refused connection.
        control_socket_path(runtime_root, row.spawn_id).unlink(missing_ok=True)
        reopened = spawn_service.lifecycle.reopen(
            _reservation(row, chat_id=row.resume_chat_id, work_id=row.work_id),
            launch_lock=continue_hold.slot,
        )
    with ExitStack() as stack:
        slot = stack.enter_context(
            nullcontext(continue_hold.slot) if continue_hold is not None else LaunchSlot()
        )
        try:
            attempt = stack.enter_context(
                session_scope(
                    runtime_root=runtime_root,
                    metadata=row.session_metadata,
                    request=row.session_request,
                    chat_id=row.resume_chat_id,
                    control_root=str(row.control_root),
                    task_cwd=row.task_cwd.as_posix() if row.task_cwd is not None else None,
                    execution_cwd=str(row.execution_cwd),
                    kind="primary",
                    spawn_id=str(row.spawn_id),
                    startup_attempt_id=uuid.uuid4().hex,
                    launch_slot=slot,
                )
            )
        except BaseException as exc:
            if reopened is not None:
                # The next launch was committed but its session could not start (the chat is
                # held, or Ctrl-C arrived while waiting): no runner will write its outcome,
                # so write it before the launch lock is released.
                asyncio.run(
                    spawn_service.complete_spawn(
                        SpawnId(reopened.id),
                        "failed",
                        1,
                        origin="launcher",
                        error=str(exc) or type(exc).__name__,
                    )
                )
            raise
        yield PrimaryHold(
            row, spawn_service=spawn_service, slot=slot, attempt=attempt, reopened=reopened
        )
