"""CLI translation for the harness-adapter ``meridian idle`` contract."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, Never, Protocol, cast

from cyclopts import Parameter

from meridian.cli.app_tree import idle_app
from meridian.lib.harness.idle_types import IdleFacts
from meridian.lib.idle.service import IdleService
from meridian.lib.state.idle_store import CompactResultValue, Stage


def _option(name: str, help_text: str) -> Parameter:
    return Parameter(name=name, help=help_text)


HarnessOption = Annotated[str | None, _option("--harness", "Harness id.")]
SessionOption = Annotated[str, _option("--session", "Native session id.")]
StretchOption = Annotated[int, _option("--stretch", "Stretch number.")]
AnchorOption = Annotated[int, _option("--anchor", "Anchor version.")]
TtlOption = Annotated[int | None, _option("--ttl", "Detected cache TTL in seconds.")]
ProviderOption = Annotated[str | None, _option("--provider", "Harness model provider.")]
CwdOption = Annotated[Path | None, _option("--cwd", "Harness session working directory.")]
StageOption = Annotated[str, Parameter(help="Stage: push, warn, or compact.")]
DoneStage = Annotated[str, Parameter(help="Completed stage (compact).")]
ImpliesReturnOption = Annotated[
    bool, _option("--implies-return", "This turn proves a user return.")
]
TurnIdOption = Annotated[str | None, _option("--turn-id", "Turn id used for deduplication.")]
UserPromptOption = Annotated[bool, _option("--user-prompt", "Confirm a user prompt.")]
DraftOption = Annotated[str, _option("--draft", "Draft state: yes, no, or unknown.")]
BusyOption = Annotated[bool, _option("--busy", "The harness is currently busy.")]
AgentsRunningOption = Annotated[int, _option("--agents-running", "Number of running agents.")]
ContextTokensOption = Annotated[int | None, _option("--context-tokens", "Current token count.")]
AutocompactOffOption = Annotated[
    bool, _option("--harness-autocompact-off", "Harness autocompact is off.")
]
ResultOption = Annotated[str, _option("--result", "Result: ok, failed, or vetoed.")]
ReasonOption = Annotated[str | None, _option("--reason", "Optional result detail.")]
PayloadArgument = Annotated[str | None, Parameter(help="Native event payload; stdin if omitted.")]
JsonOption = Annotated[bool, _option("--json", "Print open stretches as JSON.")]
InteractiveOption = Annotated[bool, _option("--interactive", "Assert adapter TUI ownership.")]


class _WireResult(Protocol):
    def to_wire(self) -> dict[str, object]: ...


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _fail(exc: Exception) -> Never:
    if isinstance(exc, KeyError) and exc.args:
        message = str(exc.args[0])
    else:
        message = str(exc).strip() or exc.__class__.__name__
    _emit_json({"error": message})
    raise SystemExit(1)


def _json(
    harness: str | None,
    interactive: bool,
    operation: Callable[[IdleService, str], _WireResult | None],
    *,
    env_fallback: bool = False,
    checks: tuple[tuple[bool, str], ...] = (),
) -> None:
    try:
        normalized = _harness(harness, env_fallback=env_fallback)
        service = _service(normalized, interactive=interactive)
        for valid, error in checks:
            if not valid:
                raise ValueError(error)
        result = operation(service, normalized)
    except Exception as exc:
        _fail(exc)
    _emit_json({} if result is None else result.to_wire())


def _event_payload(payload: str | None) -> str:
    resolved_payload = payload if payload is not None else sys.stdin.read()
    if not resolved_payload.strip():
        raise ValueError("idle event requires a payload argument or stdin")
    return resolved_payload


def _harness(value: str | None, *, env_fallback: bool = False) -> str:
    from meridian.cli.main import get_global_options
    from meridian.lib.core.types import HarnessId

    resolved = value or get_global_options().harness
    if resolved is None and env_fallback:
        resolved = os.environ.get("_MERIDIAN_HARNESS")
    if resolved is None:
        raise ValueError("--harness is required")
    try:
        return HarnessId(resolved.strip().lower()).value
    except ValueError as exc:
        raise ValueError(f"unknown harness: {resolved}") from exc


def _service(harness: str | None = None, *, interactive: bool = False) -> IdleService:
    if harness is None:
        return IdleService(env=os.environ, interactive=interactive)

    from meridian.lib.core.types import HarnessId
    from meridian.lib.harness.registry import get_harness_bundle

    bundle = get_harness_bundle(HarnessId(harness))
    return IdleService(
        env=os.environ,
        interactive=interactive,
        autocompact_off=bundle.autocompact_off,
        detect_ttl=bundle.detect_ttl,
        native_event_parser=bundle.parse_idle_event,
        native_event_applied=bundle.idle_event_applied,
    )


@idle_app.command(name="config")
def cmd_idle_config(*, interactive: InteractiveOption = False) -> None:
    """Resolve idle policy and apply the adapter role gate."""

    _json(
        None,
        interactive,
        lambda service, harness: service.config_for(harness),
        env_fallback=True,
    )


@idle_app.command(name="arm")
def cmd_idle_arm(
    *,
    harness: HarnessOption = None,
    session: SessionOption,
    ttl: TtlOption = None,
    provider: ProviderOption = None,
    cwd: CwdOption = None,
    implies_return: ImpliesReturnOption = False,
    turn_id: TurnIdOption = None,
    interactive: InteractiveOption = False,
) -> None:
    """Open or re-anchor; same stretch and anchor means keep your timers."""

    _json(
        harness,
        interactive,
        lambda service, normalized: service.arm(
            harness=normalized,
            session=session,
            ttl_seconds=ttl,
            provider=provider,
            cwd=cwd,
            implies_return=implies_return,
            turn_id=turn_id,
        ),
    )


@idle_app.command(name="return")
def cmd_idle_return(
    *,
    harness: HarnessOption = None,
    session: SessionOption,
    user_prompt: UserPromptOption = False,
    interactive: InteractiveOption = False,
) -> None:
    """Close an idle stretch after a positively identified user prompt."""

    _json(
        harness,
        interactive,
        lambda service, normalized: service.return_(harness=normalized, session=session),
        checks=((user_prompt, "--user-prompt is required"),),
    )


@idle_app.command(name="fire")
def cmd_idle_fire(
    stage: StageOption,
    *,
    harness: HarnessOption = None,
    session: SessionOption,
    stretch: StretchOption,
    anchor: AnchorOption,
    draft: DraftOption = "unknown",
    busy: BusyOption = False,
    agents_running: AgentsRunningOption = 0,
    context_tokens: ContextTokensOption = None,
    harness_autocompact_off: AutocompactOffOption = False,
    interactive: InteractiveOption = False,
) -> None:
    """Evaluate guards and claim one due stage."""

    normalized_draft = draft.strip().lower()
    facts = IdleFacts(
        draft=cast("Literal['yes', 'no', 'unknown']", normalized_draft),
        busy=busy,
        agents_running=agents_running,
        context_tokens=context_tokens,
        harness_autocompact_off=harness_autocompact_off,
    )
    _json(
        harness,
        interactive,
        lambda service, normalized: service.fire(
            cast("Stage", stage),
            harness=normalized,
            session=session,
            stretch=stretch,
            anchor=anchor,
            facts=facts,
        ),
        checks=(
            (stage in {"push", "warn", "compact"}, f"unknown idle stage: {stage}"),
            (stretch >= 1, "--stretch must be greater than zero"),
            (anchor >= 1, "--anchor must be greater than zero"),
            (
                normalized_draft in {"yes", "no", "unknown"},
                "--draft must be one of: yes, no, unknown",
            ),
            (agents_running >= 0, "--agents-running must not be negative"),
            (
                context_tokens is None or context_tokens >= 0,
                "--context-tokens must not be negative",
            ),
        ),
    )


@idle_app.command(name="done")
def cmd_idle_done(
    stage: DoneStage,
    *,
    harness: HarnessOption = None,
    session: SessionOption,
    stretch: StretchOption,
    result: ResultOption,
    reason: ReasonOption = None,
    interactive: InteractiveOption = False,
) -> None:
    """Record the result of a compaction attempt."""

    _json(
        harness,
        interactive,
        lambda service, normalized: service.done(
            cast("Literal['compact']", stage),
            harness=normalized,
            session=session,
            stretch=stretch,
            result=cast("CompactResultValue", result),
            detail=reason,
        ),
        checks=(
            (stage == "compact", f"unknown idle completion stage: {stage}"),
            (stretch >= 1, "--stretch must be greater than zero"),
            (
                result in {"ok", "failed", "vetoed"},
                "--result must be one of: ok, failed, vetoed",
            ),
        ),
    )


@idle_app.command(name="event")
def cmd_idle_event(
    payload: PayloadArgument = None,
    *,
    harness: HarnessOption = None,
    interactive: InteractiveOption = False,
) -> None:
    """Parse and apply one harness-native idle event."""

    _json(
        harness,
        interactive,
        lambda service, normalized: service.apply_native_event(
            _event_payload(payload),
            harness=normalized,
        ),
    )


def _print_status_table(rows: list[dict[str, object]]) -> None:
    if not rows:
        print("No open idle stretches.")
        return
    print(f"{'HARNESS':<10} {'SESSION':<24} {'STRETCH':>7} {'ANCHOR':>6} SCHEDULE")
    for row in rows:
        schedule = cast("dict[str, object]", row["schedule"])
        deadlines = ", ".join(
            f"{stage}={schedule[f'{stage}_at']}"
            for stage in ("push", "warn", "compact")
            if schedule.get(f"{stage}_at") is not None
        )
        print(
            f"{row['harness']!s:<10} {row['session']!s:<24} "
            f"{int(cast('int', row['stretch'])):>7} "
            f"{int(cast('int', row['anchor'])):>6} {deadlines}"
        )


@idle_app.command(name="status")
def cmd_idle_status(
    *,
    json_mode: JsonOption = False,
    interactive: InteractiveOption = False,
) -> None:
    """List open stretches with their stored schedules."""

    try:
        rows = [
            cast("dict[str, object]", state.model_dump(mode="json"))
            for state in _service(interactive=interactive).status()
        ]
        from meridian.cli.main import get_global_options

        if json_mode or get_global_options().output.format == "json":
            _emit_json(rows)
        else:
            _print_status_table(rows)
    except Exception as exc:
        _fail(exc)


@idle_app.command(name="mod-path")
def cmd_idle_mod_path() -> None:
    """Print the bundled Claude idle mod directory."""

    try:
        from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime

        resolution = resolve_claude_runtime()
        if not resolution.plugin_dirs:
            raise FileNotFoundError("bundled Claude idle mod was not found")
        print(resolution.plugin_dirs[0])
    except Exception as exc:
        _fail(exc)
