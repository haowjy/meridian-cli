import json
import os
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import structlog

from meridian.lib.config.loader import load_launch_config
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.native_identity import (
    LaunchIntent,
    NativeIdentity,
    NativeIdentityError,
    NativeKey,
    NativeKeyFields,
    NativeObservationBatch,
    NativeSessionUnavailable,
    NativeStoreMove,
    Operation,
    PostExit,
)
from meridian.lib.core.types import HarnessId, SpawnId, TransportId
from meridian.lib.harness.adapter import (
    CLAUDE_SPAWN_USAGE_VARIANTS,
    BaseHarnessAdapter,
    BootstrapMode,
    ForkMaterializationMode,
    HarnessCapabilities,
    HarnessContract,
    HarnessPrelaunchState,
    NativeRelocation,
    NativeRelocationResult,
    RecordConfigDirFn,
    RunPromptPolicy,
    SpawnParams,
)
from meridian.lib.harness.bundle import (
    HarnessBundle,
    HarnessProjectionPorts,
    register_harness_bundle,
)
from meridian.lib.harness.claude_boundary import (
    claude_boundary_journal_path,
    read_observations,
)
from meridian.lib.harness.claude_idle import detect_ttl
from meridian.lib.harness.claude_inbox import claude_primary_message_route
from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime
from meridian.lib.harness.claude_sessions import (
    candidate_claude_project_dirs,
    claude_session_links,
    claude_transcript_first_cwd,
    claude_turn_since,
    ensure_claude_session_accessible,
    link_claude_session,
    locate_claude_session_files,
    reconcile_tui_trampoline_session_id,
    resolve_claude_config_root,
    resolve_claude_native_session_file,
    validate_claude_session_file,
)
from meridian.lib.harness.claude_sessions import (
    project_slug as project_slug,
)
from meridian.lib.harness.claude_settings import (
    claude_network_config_warnings,
    claude_settings_block_plugin_hooks,
    read_claude_settings_permissions,
)
from meridian.lib.harness.claude_version import claude_version
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.extractors.claude import CLAUDE_EXTRACTOR
from meridian.lib.harness.launch_types import SessionSeed
from meridian.lib.harness.projections.network_claude import (
    effective_claude_policy,
)
from meridian.lib.harness.projections.permissions_claude import (
    compile_claude_permissions,
)
from meridian.lib.harness.projections.project_claude import (
    project_claude_settings_json,
    project_claude_spec_to_cli_args,
)
from meridian.lib.harness.semantics import (
    MERIDIAN_CONNECTION_CLOSED_EVENT,
    EventSemantics,
    HarnessSemantics,
    PrimaryEventScope,
    TerminalEventOutcome,
    connection_closed_outcome,
    stringify_terminal_error,
)
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    ProjectedContent,
    ProjectionChannels,
    build_reference_routing,
    join_content_blocks,
    render_system_instruction_blocks,
    render_task_context,
)
from meridian.lib.launch.constants import (
    BASE_COMMAND_CLAUDE_SUBPROCESS,
    PRIMARY_BASE_COMMAND_CLAUDE,
)
from meridian.lib.launch.launch_types import (
    CompositionWarning,
    ResolvedLaunchSpec,
    TerminalSurfaceMode,
)
from meridian.lib.safety.policy import (
    CONSTRAINED_SANDBOX_MODES,
    NetworkPolicy,
    network_allows_local_services,
)
from meridian.lib.state.paths import LAUNCH_FILE_NAMES, LaunchPaths

logger = structlog.get_logger(__name__)

_BOUNDARY_PLUGIN = "meridian-session-boundary"
_IDLE_PLUGIN = "meridian-idle"
_SPAWN_WATCH_PLUGIN = "meridian-spawn-watch"
FIRST_TURN_PLUGIN = "meridian-first-turn"
FIRST_TURN_PATH_ENV = "_MERIDIAN_CLAUDE_FIRST_TURN_PATH"
# Oldest Claude build verified to run a hooks-module mod that calls
# ``$.prompt.submit({asUser})`` from ``session.start``; the mods API has no
# per-feature version record, so this is the build the types were generated from.
_FIRST_TURN_MIN_VERSION = (2, 1, 314)
# Modes in which Claude loads no hooks module from a plugin.
_NO_MOD_FLAGS = frozenset({"--bare", "--safe-mode"})
_NO_MOD_ENV = ("CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SAFE_MODE")
# Claude's own truthiness set for those switches.
_ENV_TRUE = frozenset({"1", "true", "yes", "on"})


def _delivers_first_turn_by_mod(
    spec: ResolvedLaunchSpec, env: Mapping[str, str], *, hooks_blocked: bool
) -> bool:
    """Whether the starting prompt reaches Claude through the first-turn mod, not argv.

    A command-line prompt is visible to ``ps`` and to ``pkill -f``. Keep it on argv
    whenever the mod will not run: not found, a mode that loads no plugin hooks module
    (``--bare``, ``--safe-mode``, their env switches, or settings that disable plugin hooks),
    or a Claude too old for the API.
    """
    if not (
        spec.interactive
        and spec.user_turn_content
        and any(Path(d).name == FIRST_TURN_PLUGIN for d in spec.claude_runtime.plugin_dirs)
    ):
        return False
    if hooks_blocked or _NO_MOD_FLAGS.intersection(spec.extra_args):
        return False
    if any(env.get(name, "").strip().lower() in _ENV_TRUE for name in _NO_MOD_ENV):
        return False
    version = claude_version(env)
    if version is None or version < _FIRST_TURN_MIN_VERSION:
        logger.warning(
            "Keeping the starting prompt on the Claude command line: claude version is "
            "missing, unreadable or older than the first-turn mod requires",
            version=version,
            minimum=_FIRST_TURN_MIN_VERSION,
        )
        return False
    return True


def build_claude_adhoc_agent_json(*, name: str) -> str:
    """Define the agent as a stub so ``--agent <name>`` resolves; the body rides the prompt file.

    A main-thread agent's ``prompt`` replaces Claude's default system prompt, and an empty
    one restores the default, so the stub is one space: non-empty, and nothing but
    whitespace before the appended file's text.
    """

    normalized_name = name.strip()
    if not normalized_name:
        return ""

    payload = {normalized_name: {"description": "Meridian agent profile", "prompt": " "}}
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


_candidate_claude_project_dirs = candidate_claude_project_dirs


def bind_claude_spec(
    spec: ResolvedLaunchSpec,
    env: Mapping[str, str],
    child_cwd: Path,
) -> ResolvedLaunchSpec:
    """Resolve settings-file allows once from the launch environment and cwd."""

    settings_allows = read_claude_settings_permissions(child_cwd, env)
    hooks_blocked = claude_settings_block_plugin_hooks(child_cwd, env)
    return spec.model_copy(
        update={
            "claude_runtime": replace(
                spec.claude_runtime,
                settings_allows=settings_allows,
                first_turn_by_mod=_delivers_first_turn_by_mod(
                    spec, env, hooks_blocked=hooks_blocked
                ),
            )
        }
    )


def _extract_passthrough_session_id(args: tuple[str, ...]) -> str:
    for i, token in enumerate(args):
        if token == "--session-id" and i + 1 < len(args):
            return args[i + 1].strip()
        if token.startswith("--session-id="):
            return token.partition("=")[2].strip()
    return ""


class ClaudeAdapter(BaseHarnessAdapter[ResolvedLaunchSpec]):
    """SubprocessHarness implementation for `claude`."""

    native_identity = True
    refused_identity_flags = frozenset(
        ["--session-id", "--resume", "--continue", "--fork-session", "-r", "-c"]
    )
    resolves_untracked_source = True
    native_transcript_choice = True

    BASE_COMMAND: ClassVar[tuple[str, ...]] = BASE_COMMAND_CLAUDE_SUBPROCESS
    PRIMARY_BASE_COMMAND: ClassVar[tuple[str, ...]] = PRIMARY_BASE_COMMAND_CLAUDE
    _CONSUMED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "prompt",
            "model",
            "effort",
            "autocompact",
            "autocompact_pct",
            "skills",
            "agent",
            "adhoc_agent_payload",
            "extra_args",
            "control_root",
            "interactive",
            "continue_harness_session_id",
            "continue_fork",
            "appended_system_prompt",
            "projected_roots",
            "user_turn_content",
        }
    )
    _EXPLICITLY_IGNORED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "model_override_explicit",
            "reference_items",
            "task_cwd",
            "pi_harness_profile",
        }
    )

    @property
    def id(self) -> HarnessId:
        return HarnessId.CLAUDE

    @property
    def contract(self) -> HarnessContract:
        return HarnessContract(
            capabilities=self.capabilities,
            transport_ids=(TransportId.STREAMING,),
            launch_spec_cls="ResolvedLaunchSpec",
            bootstrap_mode=BootstrapMode.SUBPROCESS_ONLY,
            fork_materialization=ForkMaterializationMode.NATIVE_CONTINUE_FORK,
        )

    @property
    def consumed_fields(self) -> frozenset[str]:
        return self._CONSUMED_FIELDS

    @property
    def explicitly_ignored_fields(self) -> frozenset[str]:
        return self._EXPLICITLY_IGNORED_FIELDS

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            supports_stdin_prompt=True,
            supports_session_resume=True,
            supports_session_fork=True,
            supports_native_agents=True,
            agent_body_in_system_prompt=True,
            supports_primary_launch=True,
            supports_named_primary_resume=True,
            supports_native_file_injection=False,
            captures_blackbox_output=True,
            terminal_surface_mode=TerminalSurfaceMode.PTY_MEDIATED,
        )

    def run_prompt_policy(self) -> RunPromptPolicy:
        return RunPromptPolicy(
            spawn_usage_contract_variants=CLAUDE_SPAWN_USAGE_VARIANTS,
        )

    def shell_network_guidance(
        self,
        *,
        sandbox: str | None,
        network: NetworkPolicy,
        native_config: Mapping[str, object],
    ) -> str | None:
        _ = native_config
        if (
            not sys.platform.startswith("linux")
            or sandbox not in CONSTRAINED_SANDBOX_MODES
            or not network_allows_local_services(network)
        ):
            return None
        return (
            "To reach a service on the host through Claude's sandbox proxy, bypass "
            "NO_PROXY (for curl: `curl --noproxy '' ...`)."
        )

    def build_adhoc_agent_payload(self, *, name: str, description: str, prompt: str) -> str:
        _ = description, prompt
        return build_claude_adhoc_agent_json(name=name)

    def launch_files(self, spec: ResolvedLaunchSpec) -> dict[Path, str]:
        content = project_claude_settings_json(spec)
        if spec.settings_file_path is None or content is None:
            return {}
        return {Path(spec.settings_file_path): content}

    def native_store_for_launch(
        self,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
        spawn_id: SpawnId,
        operation: Operation,
        interactive: bool,
    ) -> str:
        return str(
            (
                resolve_claude_config_root(child_env, child_cwd)
                / "projects"
                / project_slug(child_cwd)
            ).resolve()
        )

    def classify_store_move(self, *, recorded: str, attempted: str) -> NativeStoreMove | None:
        # A store is <config root>/projects/<slug(control_root)>.
        root = Path(recorded).parent.parent
        if root == Path(attempted).parent.parent:
            return NativeStoreMove(recorded, attempted, "project_moved")
        return NativeStoreMove(
            recorded,
            attempted,
            "config_root",
            fix=f"is recorded under the Claude config root {root}: set CLAUDE_CONFIG_DIR={root}",
            also_moved=Path(recorded).name != Path(attempted).name,
        )

    def locate_native_session(self, *, session_id: str, recorded_store: Path) -> tuple[Path, ...]:
        # The recorded config root only: the store check already holds the child to it.
        found = locate_claude_session_files(session_id, (recorded_store.parent.parent,))
        return tuple(file for _, file in found)

    def native_cwd(
        self,
        identity: NativeKeyFields,
        *,
        recorded_roots: Sequence[Path] = (),
    ) -> Path | None:
        if identity.native_store is None:
            return None
        store = Path(identity.native_store)
        # Compare forward: a slug can't be reversed (`my_app`, `my.app` and `my-app`
        # share one), so only a recorded root whose slug names the store is trusted.
        for root in recorded_roots:
            if project_slug(root) == store.name:
                return root
        if identity.session_id is None:
            return None
        return claude_transcript_first_cwd(store / f"{identity.session_id}.jsonl")

    def _plan_native_relocate(
        self,
        identity: NativeKeyFields,
        *,
        cwd_from: Path,
        cwd_to: Path,
        native: Path | None,
    ) -> NativeRelocation | None:
        if identity.native_store is None or identity.session_id is None:
            return None
        recorded, session_id = Path(identity.native_store), identity.session_id

        def refuse(reason: str) -> NativeRelocation:
            return NativeRelocation(identity, cwd_from, cwd_to, "", refusal=reason)

        if native is not None:
            if native.parent.parent.name != "projects":
                return refuse(f"{native} is not inside a Claude <config root>/projects/<folder>")
            try:
                validate_claude_session_file(native, session_id)
            except NativeIdentityError as exc:
                return refuse(f"{native} is not this chat's transcript: {exc}")
            config_root = native.parent.parent.parent
            source: Path | None = native.resolve()
        else:
            config_root = recorded.parent.parent
            source = None
        env = {"CLAUDE_CONFIG_DIR": str(config_root)}
        # The store the next launch computes from the moved root, not a restated formula.
        env_with_store = dict(env)
        self.pin_native_store(env_with_store, str(recorded))
        new_store = Path(self._store(env_with_store, cwd_to, SpawnId(""), "resume", False))
        if source is None:
            candidates = (recorded / f"{session_id}.jsonl", new_store / f"{session_id}.jsonl")
            source = next((c.resolve() for c in candidates if c.is_file()), None)
        if source is None:
            # The miss path: the transcript under the recorded config root, and under the
            # root the child env resolves to when that differs.
            recorded_root = recorded.parent.parent
            found = locate_claude_session_files(
                session_id, (recorded_root, resolve_claude_config_root(env, Path.cwd()))
            )
            if len(found) > 1 or any(root != recorded_root for root, _ in found):
                listing = ", ".join(str(file) for _, file in found)
                return refuse(
                    f"{session_id} is not in {recorded}, but exists at {listing}; "
                    "pick one with --native"
                )
            source = found[0][1] if found else None
        store_change = f"native store {recorded} -> {new_store}"
        if source is None:
            return NativeRelocation(
                identity,
                cwd_from,
                cwd_to,
                f"{store_change} (transcript not found anywhere)",
                native_store=str(new_store),
            )
        links, conflict = claude_session_links(source, new_store)
        if conflict is not None:
            return refuse(f"{conflict}; remove it, or choose a transcript with --native")
        if not links and str(new_store) == identity.native_store:
            return None
        change = store_change
        if links:
            folder = " and its folder" if source.with_suffix("").is_dir() else ""
            change += f"; link {session_id}.jsonl{folder} from {source.parent}"
        if native is not None and config_root != resolve_claude_config_root(os.environ, Path.cwd()):
            change += f"; continue with CLAUDE_CONFIG_DIR={config_root}"
        return NativeRelocation(
            identity,
            cwd_from,
            cwd_to,
            change,
            source=source if links else None,
            native_store=str(new_store),
        )

    def apply_native_relocations(
        self, plans: Sequence[NativeRelocation]
    ) -> tuple[NativeRelocationResult, ...]:
        return tuple(
            NativeRelocationResult(
                plan,
                link_claude_session(plan.source, Path(plan.native_store))
                if plan.source is not None and plan.native_store is not None
                else None,
            )
            for plan in plans
        )

    def assign_session_id(self, intent: LaunchIntent, *, store: Path) -> str | None:
        return (
            str(uuid4())
            if intent.operation == "create"
            else super().assign_session_id(intent, store=store)
        )

    def resolve_launch_spec(self, run: SpawnParams) -> ResolvedLaunchSpec:
        normalized_effort = None if run.effort is None else str(run.effort).strip()
        continue_session_id = (run.continue_harness_session_id or "").strip() or None

        # prompt_file_path is owned by bind_launch_context, which sets it to
        # <spawn-log-dir>/system-prompt.md (the single artifact-dir authority).
        prompt_file_path: str | None = None
        user_turn_content = getattr(run, "user_turn_content", None)
        plugin_names = (_BOUNDARY_PLUGIN,)
        if run.interactive:
            plugin_names += (_IDLE_PLUGIN, FIRST_TURN_PLUGIN)
            if load_launch_config(run.control_root).harness.claude.spawn_watch.enabled:
                plugin_names += (_SPAWN_WATCH_PLUGIN,)
        return ResolvedLaunchSpec(
            harness=HarnessId.CLAUDE,
            model=str(run.model).strip() if run.model else None,
            effort=normalized_effort,
            autocompact=run.autocompact,
            autocompact_pct=run.autocompact_pct,
            prompt=run.prompt,
            continue_session_id=continue_session_id,
            continue_fork=run.continue_fork and continue_session_id is not None,
            extra_args=run.extra_args,
            interactive=run.interactive,
            projected_roots=run.projected_roots,
            appended_system_prompt=run.appended_system_prompt,
            agents_payload=run.adhoc_agent_payload.strip() or None,
            agent_name=run.agent,
            prompt_file_path=prompt_file_path,
            user_turn_content=user_turn_content,
            claude_runtime=resolve_claude_runtime(plugin_names=plugin_names),
        )

    def launch_spec_warnings(self, spec: ResolvedLaunchSpec) -> tuple[CompositionWarning, ...]:
        return spec.claude_runtime.warnings

    def launch_environment_warnings(
        self,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
    ) -> tuple[CompositionWarning, ...]:
        return claude_network_config_warnings(child_cwd, child_env)

    def project_launch_env(
        self,
        *,
        spec: ResolvedLaunchSpec,
        system_prompt_path: Path,
        env: Mapping[str, str],
    ) -> dict[str, str]:
        _ = env
        overrides: dict[str, str] = {}
        if spec.claude_runtime.first_turn_by_mod:
            overrides[FIRST_TURN_PATH_ENV] = str(
                system_prompt_path.with_name(LAUNCH_FILE_NAMES.starting_prompt)
            )
        if not spec.interactive:
            # Do not let a primary's inherited one-hour TTL become the spawn default.
            overrides["CLAUDE_CODE_PROMPT_CACHE_TTL"] = "5m"
        if spec.autocompact_pct is not None:
            overrides["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(spec.autocompact_pct)
        return overrides

    def blocked_child_env_vars(self) -> frozenset[str]:
        # Meridian manages nesting limits itself; suppress Claude's parent-session
        # sentinel so child Claude spawns can run under Meridian control.
        return frozenset({"CLAUDECODE"})

    def prepare_prelaunch(
        self,
        *,
        launch: LaunchPaths,
        identity: NativeIdentity | None,
        child_cwd: Path,
        child_env: dict[str, str],
        resolved_harness_session_id: str,
        record_effective_config_dir: RecordConfigDirFn | None = None,
    ) -> HarnessPrelaunchState:
        effective_config_root = resolve_claude_config_root(child_env, child_cwd)
        if record_effective_config_dir is not None:
            record_effective_config_dir(str(effective_config_root))

        if identity is not None and identity.source is not None and identity.source_session_id:
            source_store = identity.source.parent
            if identity.located_from is not None:
                # The recorded file was missing: expose the one found in the recorded
                # store, so the entry check and the next launch see it there.
                refusal = link_claude_session(identity.located_from, Path(identity.native_store))
                if refusal is not None:
                    raise NativeSessionUnavailable(
                        identity.source_session_id, "missing", hint=refusal
                    )
                print(
                    f"Claude session {identity.source_session_id} was missing from "
                    f"{identity.native_store}; resuming from {identity.located_from} "
                    "(linked into the recorded store).",
                    file=sys.stderr,
                )
                source_store = Path(identity.native_store)
            ensure_claude_session_accessible(
                source_session_id=identity.source_session_id,
                child_cwd=child_cwd,
                source_native_store=source_store,
                target_config_root=effective_config_root,
            )

        return HarnessPrelaunchState(
            env_overrides={
                "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH": str(claude_boundary_journal_path(launch)),
                "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE": secrets.token_hex(32),
                "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PYTHON": sys.executable,
            }
        )

    def cleanup_prelaunch(
        self,
        *,
        runtime_root: Path,
        spawn_id: SpawnId,
        chat_id: str | None,
        state: HarnessPrelaunchState,
    ) -> None:
        _ = runtime_root, spawn_id, chat_id, state

    def seed_session(
        self,
        *,
        is_resume: bool,
        harness_session_id: str,
        passthrough_args: tuple[str, ...],
    ) -> SessionSeed:
        normalized_harness_session_id = harness_session_id.strip()
        # Resume and fork both provide an explicit harness session id. Fork is
        # represented as is_resume=False with harness_session_id set.
        if normalized_harness_session_id:
            return SessionSeed(session_id=normalized_harness_session_id)

        passthrough_session_id = _extract_passthrough_session_id(passthrough_args)
        if passthrough_session_id:
            return SessionSeed(session_id=passthrough_session_id)

        # resolve_launch_spec() seeds --session-id for all launches (interactive
        # and non-interactive).  No seed needed from the session-access layer.
        return SessionSeed()

    def project_content(self, content: ComposedLaunchContent) -> ProjectedContent:
        # The profile body leads the file: Claude puts the stub agent prompt first and
        # appends this file after it, which is where the inline agent prompt used to sit.
        system_prompt = join_content_blocks(
            content.agent_profile_body,
            render_system_instruction_blocks(replace(content, agent_profile_body="")),
        )
        reference_routing = build_reference_routing(content.reference_items)
        task_context = render_task_context(
            content.reference_items,
            reference_routing,
            content.prior_output,
        )
        user_turn = join_content_blocks(task_context, content.user_task_prompt)

        return ProjectedContent(
            system_prompt=system_prompt,
            user_turn_content=user_turn,
            reference_routing=reference_routing,
            channels=ProjectionChannels(
                system_instruction=("append-system-prompt" if system_prompt.strip() else "none"),
                user_task_prompt="user-turn",
                task_context="user-turn",
            ),
        )

    def observe_after_exit(
        self,
        identity: NativeIdentity,
        entry: NativeKeyFields,
        *,
        child_env: Mapping[str, str],
        child_cwd: Path,
        pid: int | None,
        started_at_epoch: float | None,
    ) -> PostExit:
        _ = child_env, pid
        successor = reconcile_tui_trampoline_session_id(
            project_root=child_cwd,
            recorded_session_id=entry.session_id or "",
            started_at_epoch=started_at_epoch,
            native_store=Path(identity.native_store),
        )
        return PostExit(
            trampoline_successor_id=successor if successor != entry.session_id else None,
        )

    def turn_since(self, key: NativeKey, *, started_at_epoch: float) -> bool | None:
        return claude_turn_since(key.native_store, key.session_id, started_at_epoch)

    def native_observation_reader(
        self,
        *,
        child_env: Mapping[str, str],
        pid: int,
    ) -> Callable[[int], NativeObservationBatch] | None:
        raw_path = child_env.get("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH")
        nonce = child_env.get("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE")
        if not raw_path or not nonce:
            return None
        path = Path(raw_path)
        return lambda cursor: read_observations(path, nonce=nonce, pid=pid, cursor=cursor)

    def resolve_native_session_file(
        self,
        *,
        session_id: str,
        native_store: Path,
    ) -> Path | None:
        return resolve_claude_native_session_file(
            session_id=session_id,
            native_store=native_store,
        )

    def resolve_session_file(
        self,
        *,
        project_root: Path,
        session_id: str,
        config_root_hint: Path | None = None,
    ) -> Path | None:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return None
        project_dirs = _candidate_claude_project_dirs(project_root, config_root_hint)
        matches = [
            directory / f"{normalized_session_id}.jsonl"
            for directory in project_dirs
            if (directory / f"{normalized_session_id}.jsonl").is_file()
        ]
        if len(matches) > 1:
            raise NativeSessionUnavailable(normalized_session_id, "ambiguous_native_file")
        if not matches:
            return None
        return matches[0]

    def owns_untracked_session(self, *, project_root: Path, session_ref: str) -> bool:
        normalized_session_ref = session_ref.strip()
        if not normalized_session_ref:
            return False
        for project_dir in _candidate_claude_project_dirs(project_root):
            session_file = project_dir / f"{normalized_session_ref}.jsonl"
            if session_file.is_file():
                return True
        return False


def _resolve_claude_terminal(event: RawHarnessEvent) -> TerminalEventOutcome | None:
    if event.event_type == MERIDIAN_CONNECTION_CLOSED_EVENT:
        return connection_closed_outcome(event)
    if bool(event.payload.get("is_error")):
        error = (
            stringify_terminal_error(event.payload.get("result"))
            or stringify_terminal_error(event.payload.get("error"))
            or "claude_result_error"
        )
        return TerminalEventOutcome(status=SpawnStatus.FAILED, exit_code=1, error=error)

    subtype = str(event.payload.get("subtype", "")).strip().lower()
    terminal_reason = str(event.payload.get("terminal_reason", "")).strip().lower()
    if subtype in {"", "success"} and terminal_reason in {"", "completed"}:
        return TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0)
    if terminal_reason == "completed":
        return TerminalEventOutcome(status=SpawnStatus.SUCCEEDED, exit_code=0)

    error = stringify_terminal_error(event.payload.get("result"))
    if subtype not in {"", "success"}:
        error = error or f"claude_result_{subtype}"
    elif terminal_reason:
        error = error or f"claude_terminal_{terminal_reason}"
    else:
        error = error or "claude_result_unknown"
    return TerminalEventOutcome(status=SpawnStatus.FAILED, exit_code=1, error=error)


class _ClaudeSemantics(HarnessSemantics):
    def normalize(
        self,
        event: RawHarnessEvent,
        *,
        primary_event_scope: PrimaryEventScope | None = None,
    ) -> EventSemantics:
        # Background Agent frames share stdout, but cannot reset the parent's wait.
        if event.event_type == "assistant" and event.payload.get("parent_tool_use_id"):
            return EventSemantics()
        # `init` opens every turn, a model call ahead of `assistant`; until then the hold
        # would close a child that is starting one. It is not work, so it renews nothing.
        if event.event_type == "system" and event.payload.get("subtype") == "init":
            return EventSemantics(activity="turn_active")
        return super().normalize(event, primary_event_scope=primary_event_scope)


CLAUDE_SEMANTICS = _ClaudeSemantics(
    events={
        # Load-bearing: clears awaiting-done and resets the resident nudge timer.
        "assistant": EventSemantics(activity="turn_active", work=True),
        "result": EventSemantics(clears_signal=True),
        MERIDIAN_CONNECTION_CLOSED_EVENT: EventSemantics(),
    },
    payload_resolvers={
        "result": _resolve_claude_terminal,
        MERIDIAN_CONNECTION_CLOSED_EVENT: _resolve_claude_terminal,
    },
)

register_harness_bundle(
    HarnessBundle(
        harness_id=HarnessId.CLAUDE,
        adapter=ClaudeAdapter(),
        spec_cls=ResolvedLaunchSpec,
        extractor=CLAUDE_EXTRACTOR,
        connections={TransportId.STREAMING: ClaudeConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_claude_spec_to_cli_args,
            permission_report=lambda spec: compile_claude_permissions(spec)[1],
            effective_policy=effective_claude_policy,
            bind_spec=bind_claude_spec,
        ),
        semantics=CLAUDE_SEMANTICS,
        primary_message_route=claude_primary_message_route,
        detect_ttl=detect_ttl,
    )
)
