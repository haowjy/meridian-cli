"""Pi completion precedence as a pure profile decision table."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.streaming.completion_contracts import (
    CompletionDirectives,
    CompletionEvaluation,
    CompletionState,
    DiagnosticBlocker,
    WorkAssessment,
)
from meridian.lib.streaming.pi_completion_profile import (
    PiCompletionCleanupPort,
    PiCompletionEvidenceView,
    PiCompletionProfile,
)
from meridian.lib.streaming.pi_work_ledger import PiPrivateWorkLedger

_SUCCESS = TerminalEventOutcome(status="succeeded", exit_code=0)
_READY = WorkAssessment(disposition="ready", blockers=(), generation=1)
_BLOCKED = WorkAssessment(
    disposition="blocked",
    blockers=(DiagnosticBlocker(source="test", code="active", identity="child"),),
    generation=1,
)


def _profile(tmp_path: Any, *, notification: bool, child: bool, nudge: bool) -> PiCompletionProfile:
    ledger = PiPrivateWorkLedger()
    if notification:
        ledger.note_notification_started(
            "n1", phase="queued", observation_monotonic=0.0, notification_timeout_seconds=5.0
        )
    evidence = cast(
        "PiCompletionEvidenceView",
        SimpleNamespace(
            tracker=SimpleNamespace(notification_failure_error=None),
            quiescence_tracker=SimpleNamespace(parent_idle=True),
            session_seen=False,
            session_phase_emitted=False,
            has_pending_children=lambda: child,
            pending_child_count=lambda: int(child),
        ),
    )
    profile = PiCompletionProfile(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p1"),
        session_role="spawned",
        child_wave_timeout_seconds=5.0,
        emit_phase=lambda **_payload: None,
        send_done_nudge=None,
        evidence=evidence,
        private_work_ledger=ledger,
        stabilization_seconds=0.05,
        clock=lambda: 5.0,
    )
    profile.last_successful_terminal = _SUCCESS
    if child:
        profile.child_wave_started_monotonic = 0.0
        profile.child_wave_deadline_monotonic = 5.0
    if nudge:
        profile.next_done_nudge_monotonic = 5.0
    profile.bind_cleanup(
        cast("PiCompletionCleanupPort", SimpleNamespace(prepare_child_timeout=lambda _t: None))
    )
    return profile


@pytest.mark.parametrize(
    ("done", "notification", "child", "nudge", "action", "error"),
    [
        pytest.param(True, True, True, True, "complete", None, id="done-before-all-timeouts"),
        pytest.param(
            False,
            True,
            True,
            True,
            "fail",
            "pi_notification_timeout",
            id="notification-before-child-and-nudge",
        ),
        pytest.param(
            False, False, True, True, "cleanup", "pi_child_wave_timeout", id="child-before-nudge"
        ),
        pytest.param(False, False, False, True, "wait", None, id="nudge-when-no-terminal-priority"),
    ],
)
def test_timeout_priority(
    tmp_path: Any,
    done: bool,
    notification: bool,
    child: bool,
    nudge: bool,
    action: str,
    error: str | None,
) -> None:
    profile = _profile(tmp_path, notification=notification, child=child, nudge=nudge)
    assessment = _BLOCKED if child else _READY
    decision = profile.evaluate(
        CompletionEvaluation(
            state=CompletionState("waiting", _SUCCESS, assessment, 5.0, None, None),
            trigger="timeout",
            now=5.0,
            directives=CompletionDirectives(done=done),
            assessment=assessment,
            deadline_expired=True,
            profile_timer_due=nudge,
            candidate=_SUCCESS,
        )
    )

    assert decision.action == action
    if error is None:
        assert decision.outcome is None or decision.outcome == _SUCCESS
    else:
        assert decision.outcome is not None
        assert decision.outcome.error is not None
        assert decision.outcome.error.startswith(error)
