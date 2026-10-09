"""CLI translation for the harness-adapter ``meridian idle`` contract."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Never, cast

from cyclopts import Parameter

from meridian.cli.app_tree import idle_app

if TYPE_CHECKING:
    from meridian.lib.harness.bundle import HarnessBundle
    from meridian.lib.harness.idle_types import DetectIdleTtl, PinnedIdleSession
    from meridian.lib.idle.service import IdleService

DraftFact = Literal["yes", "no", "unknown"]


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _error_message(exc: Exception) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc).strip() or exc.__class__.__name__


def _fail(message: str) -> Never:
    _emit_json({"error": message})
    raise SystemExit(1)


def _json_operation(operation: Callable[[], object]) -> None:
    try:
        payload = operation()
    except Exception as exc:
        _fail(_error_message(exc))
    _emit_json(payload)


def _normalized_harness(value: str) -> str:
    from meridian.lib.core.types import HarnessId

    try:
        return HarnessId(value.strip().lower()).value
    except ValueError as exc:
        raise ValueError(f"unknown harness: {value}") from exc


def _bundle(harness: str) -> HarnessBundle[Any]:
    from meridian.lib.core.types import HarnessId
    from meridian.lib.harness.registry import get_harness_bundle

    return get_harness_bundle(HarnessId(harness))


def _service(
    *,
    env: Mapping[str, str] | None = None,
    interactive: bool = False,
) -> IdleService:
    from meridian.lib.idle.service import IdleService

    return IdleService(
        env=os.environ if env is None else env,
        interactive=interactive,
    )


def _configured_harness() -> str:
    from meridian.cli.main import get_global_options

    resolved = get_global_options().harness or os.environ.get("_MERIDIAN_HARNESS")
    if resolved is None:
        raise ValueError("--harness is required")
    return _normalized_harness(resolved)


def _required_harness(value: str | None) -> str:
    from meridian.cli.main import get_global_options

    resolved = value or get_global_options().harness
    if resolved is None:
        raise ValueError("--harness is required")
    return _normalized_harness(resolved)


def _config_payload(*, interactive: bool) -> dict[str, object]:
    harness = _configured_harness()
    result = _service(interactive=interactive).config_for(harness)
    role = os.environ.get("MERIDIAN_SESSION_ROLE")

    reason = result.reason
    if result.reason == "not-primary" and role is None and not interactive:
        reason = "interactive"
    elif result.reason == "not-primary":
        reason = "role"

    policy = result.policy
    payload: dict[str, object] = {
        "enabled": reason is None,
        "push_seconds": policy.push_seconds,
        "warn_minutes": policy.warn_minutes,
        "compact_minutes": policy.compact_minutes,
        "compact": policy.compact,
        "min_compact_tokens": policy.min_compact_tokens,
    }
    if reason is not None:
        payload["reason"] = reason
    if policy.ttl_seconds is not None:
        payload["ttl_seconds"] = policy.ttl_seconds
    return payload


@idle_app.command(name="config")
def cmd_idle_config(
    *,
    interactive: Annotated[
        bool,
        Parameter(name="--interactive", help="Assert that the adapter owns a TUI."),
    ] = False,
) -> None:
    """Resolve idle policy and apply the adapter role gate."""

    _json_operation(lambda: _config_payload(interactive=interactive))


def _detected_ttl(
    *,
    service: IdleService,
    harness: str,
    session: str,
    cwd: Path | None,
    provider: str | None,
    explicit_ttl: int | None,
) -> int | None:
    if explicit_ttl is not None:
        return explicit_ttl

    config_result = service.config_for(harness)
    if not config_result.enabled:
        return None
    if config_result.policy.ttl_seconds is not None:
        return None

    bundle = _bundle(harness)
    detector: DetectIdleTtl | None = bundle.detect_ttl
    if detector is None:
        return None
    return detector(
        session_id=session,
        cwd=cwd,
        provider=provider,
        env=os.environ,
    )


def _arm_payload(
    *,
    harness: str | None,
    session: str,
    ttl: int | None,
    provider: str | None,
    cwd: Path | None,
    implies_return: bool,
    turn_id: str | None,
    interactive: bool,
) -> dict[str, object]:
    normalized = _required_harness(harness)
    service = _service(interactive=interactive)
    detected_ttl = _detected_ttl(
        service=service,
        harness=normalized,
        session=session,
        cwd=cwd,
        provider=provider,
        explicit_ttl=ttl,
    )
    result = service.arm(
        harness=normalized,
        session=session,
        ttl_seconds=ttl if ttl is not None else detected_ttl,
        implies_return=implies_return,
        turn_id=turn_id,
    )
    payload: dict[str, object] = {
        "stretch": result.stretch,
        "anchor": result.anchor,
    }
    for field in ("push_at", "warn_at", "compact_at"):
        value = getattr(result, field)
        if value is not None:
            payload[field] = value
    return payload


@idle_app.command(name="arm")
def cmd_idle_arm(
    *,
    harness: Annotated[
        str | None,
        Parameter(name="--harness", help="Harness id."),
    ] = None,
    session: Annotated[str, Parameter(name="--session", help="Native session id.")],
    ttl: Annotated[
        int | None,
        Parameter(name="--ttl", help="Detected cache TTL in seconds."),
    ] = None,
    provider: Annotated[
        str | None,
        Parameter(name="--provider", help="Harness model provider."),
    ] = None,
    cwd: Annotated[
        Path | None,
        Parameter(name="--cwd", help="Harness session working directory."),
    ] = None,
    implies_return: Annotated[
        bool,
        Parameter(name="--implies-return", help="This turn proves a user return."),
    ] = False,
    turn_id: Annotated[
        str | None,
        Parameter(name="--turn-id", help="Turn id used for deduplication."),
    ] = None,
    interactive: Annotated[
        bool,
        Parameter(
            name="--interactive",
            help="Required on every adapter call from a TUI outside Meridian.",
        ),
    ] = False,
) -> None:
    """Open or re-anchor; same stretch and anchor means keep your timers."""

    _json_operation(
        lambda: _arm_payload(
            harness=harness,
            session=session,
            ttl=ttl,
            provider=provider,
            cwd=cwd,
            implies_return=implies_return,
            turn_id=turn_id,
            interactive=interactive,
        )
    )


def _return_payload(
    *,
    harness: str | None,
    session: str,
    user_prompt: bool,
    interactive: bool,
) -> dict[str, object]:
    if not user_prompt:
        raise ValueError("--user-prompt is required")
    result = _service(interactive=interactive).return_(
        harness=_required_harness(harness),
        session=session,
        user_prompt=True,
    )
    return {
        "stretch_closed": result.stretch_closed is not None,
        "was_open": result.was_open,
    }


@idle_app.command(name="return")
def cmd_idle_return(
    *,
    harness: Annotated[
        str | None,
        Parameter(name="--harness", help="Harness id."),
    ] = None,
    session: Annotated[str, Parameter(name="--session", help="Native session id.")],
    user_prompt: Annotated[
        bool,
        Parameter(name="--user-prompt", help="Confirm a positively identified user prompt."),
    ] = False,
    interactive: Annotated[
        bool,
        Parameter(
            name="--interactive",
            help="Required on every adapter call from a TUI outside Meridian.",
        ),
    ] = False,
) -> None:
    """Close an idle stretch after a positively identified user prompt."""

    _json_operation(
        lambda: _return_payload(
            harness=harness,
            session=session,
            user_prompt=user_prompt,
            interactive=interactive,
        )
    )


def _draft_fact(value: str) -> DraftFact:
    normalized = value.strip().lower()
    if normalized not in {"yes", "no", "unknown"}:
        raise ValueError("--draft must be one of: yes, no, unknown")
    return cast("DraftFact", normalized)


def _fire_payload(
    stage: str,
    *,
    harness: str | None,
    session: str,
    stretch: int,
    anchor: int,
    draft: str,
    busy: bool,
    agents_running: int,
    context_tokens: int | None,
    harness_autocompact_off: bool,
    interactive: bool,
) -> dict[str, object]:
    from meridian.lib.harness.idle_types import IdleFacts

    if stage not in {"push", "warn", "compact"}:
        raise ValueError(f"unknown idle stage: {stage}")
    if stretch < 1:
        raise ValueError("--stretch must be greater than zero")
    if anchor < 1:
        raise ValueError("--anchor must be greater than zero")
    if agents_running < 0:
        raise ValueError("--agents-running must not be negative")
    if context_tokens is not None and context_tokens < 0:
        raise ValueError("--context-tokens must not be negative")

    normalized = _required_harness(harness)
    env_autocompact_off = False
    if stage == "compact":
        autocompact_off = _bundle(normalized).autocompact_off
        if autocompact_off is not None:
            env_autocompact_off = bool(autocompact_off(os.environ))

    facts = IdleFacts(
        draft=_draft_fact(draft),
        busy=busy,
        agents_running=agents_running,
        context_tokens=context_tokens,
        harness_autocompact_off=harness_autocompact_off or env_autocompact_off,
    )
    result = _service(interactive=interactive).fire(
        cast("Literal['push', 'warn', 'compact']", stage),
        harness=normalized,
        session=session,
        stretch=stretch,
        anchor=anchor,
        facts=facts,
    )
    return {"decision": result.decision, "reason": result.reason}


@idle_app.command(name="fire")
def cmd_idle_fire(
    stage: Annotated[str, Parameter(help="Stage: push, warn, or compact.")],
    *,
    harness: Annotated[
        str | None,
        Parameter(name="--harness", help="Harness id."),
    ] = None,
    session: Annotated[str, Parameter(name="--session", help="Native session id.")],
    stretch: Annotated[int, Parameter(name="--stretch", help="Stretch number.")],
    anchor: Annotated[int, Parameter(name="--anchor", help="Anchor version.")],
    draft: Annotated[
        str,
        Parameter(name="--draft", help="Draft state: yes, no, or unknown."),
    ] = "unknown",
    busy: Annotated[
        bool,
        Parameter(name="--busy", help="The harness is currently busy."),
    ] = False,
    agents_running: Annotated[
        int,
        Parameter(name="--agents-running", help="Number of running harness agents."),
    ] = 0,
    context_tokens: Annotated[
        int | None,
        Parameter(name="--context-tokens", help="Current context token count."),
    ] = None,
    harness_autocompact_off: Annotated[
        bool,
        Parameter(
            name="--harness-autocompact-off",
            help="The harness's own automatic compaction is disabled.",
        ),
    ] = False,
    interactive: Annotated[
        bool,
        Parameter(
            name="--interactive",
            help="Required on every adapter call from a TUI outside Meridian.",
        ),
    ] = False,
) -> None:
    """Evaluate guards and claim one due stage."""

    _json_operation(
        lambda: _fire_payload(
            stage,
            harness=harness,
            session=session,
            stretch=stretch,
            anchor=anchor,
            draft=draft,
            busy=busy,
            agents_running=agents_running,
            context_tokens=context_tokens,
            harness_autocompact_off=harness_autocompact_off,
            interactive=interactive,
        )
    )


def _done_payload(
    stage: str,
    *,
    harness: str | None,
    session: str,
    stretch: int,
    result: str,
    reason: str | None,
    interactive: bool,
) -> dict[str, object]:
    if stage != "compact":
        raise ValueError(f"unknown idle completion stage: {stage}")
    if stretch < 1:
        raise ValueError("--stretch must be greater than zero")
    if result not in {"ok", "failed", "vetoed"}:
        raise ValueError("--result must be one of: ok, failed, vetoed")
    _service(interactive=interactive).done(
        "compact",
        harness=_required_harness(harness),
        session=session,
        stretch=stretch,
        result=cast("Literal['ok', 'failed', 'vetoed']", result),
        detail=reason,
    )
    return {}


@idle_app.command(name="done")
def cmd_idle_done(
    stage: Annotated[str, Parameter(help="Completed stage (compact).")],
    *,
    harness: Annotated[
        str | None,
        Parameter(name="--harness", help="Harness id."),
    ] = None,
    session: Annotated[str, Parameter(name="--session", help="Native session id.")],
    stretch: Annotated[int, Parameter(name="--stretch", help="Stretch number.")],
    result: Annotated[
        str,
        Parameter(name="--result", help="Result: ok, failed, or vetoed."),
    ],
    reason: Annotated[
        str | None,
        Parameter(name="--reason", help="Optional failure or veto detail."),
    ] = None,
    interactive: Annotated[
        bool,
        Parameter(
            name="--interactive",
            help="Required on every adapter call from a TUI outside Meridian.",
        ),
    ] = False,
) -> None:
    """Record the result of a compaction attempt."""

    _json_operation(
        lambda: _done_payload(
            stage,
            harness=harness,
            session=session,
            stretch=stretch,
            result=result,
            reason=reason,
            interactive=interactive,
        )
    )


def _event_payload(
    *,
    harness: str | None,
    payload: str | None,
    interactive: bool,
) -> dict[str, object]:
    normalized = _required_harness(harness)
    raw_payload = payload if payload is not None else sys.stdin.read()
    if not raw_payload.strip():
        raise ValueError("idle event requires a payload argument or stdin")

    bundle = _bundle(normalized)
    parser = bundle.parse_idle_event
    if parser is None:
        raise ValueError(f"harness {normalized} does not accept idle events")
    service = _service(interactive=interactive)

    def read_session(session: str) -> PinnedIdleSession | None:
        from meridian.lib.harness.idle_types import PinnedIdleSession

        states = service.status(harness=normalized, session=session)
        if not states:
            return None
        return PinnedIdleSession(last_input_count=states[0].last_input_count)

    event = parser(raw_payload, session_reader=read_session)
    if event is not None:
        service.event(event, harness=normalized)
        event_applied = bundle.idle_event_applied
        if event_applied is not None:
            event_applied(raw_payload, os.environ)
    return {}


@idle_app.command(name="event")
def cmd_idle_event(
    payload: Annotated[
        str | None,
        Parameter(help="Harness-native event payload; stdin when omitted."),
    ] = None,
    *,
    harness: Annotated[
        str | None,
        Parameter(name="--harness", help="Harness id."),
    ] = None,
    interactive: Annotated[
        bool,
        Parameter(
            name="--interactive",
            help="Required on every adapter call from a TUI outside Meridian.",
        ),
    ] = False,
) -> None:
    """Parse and apply one harness-native idle event."""

    _json_operation(
        lambda: _event_payload(
            harness=harness,
            payload=payload,
            interactive=interactive,
        )
    )


def _status_rows(*, interactive: bool = False) -> list[dict[str, object]]:
    states = _service(interactive=interactive).status()
    return [cast("dict[str, object]", state.model_dump(mode="json")) for state in states]


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
    json_mode: Annotated[
        bool,
        Parameter(name="--json", help="Print open stretches as JSON."),
    ] = False,
    interactive: Annotated[
        bool,
        Parameter(
            name="--interactive",
            help="Required on every adapter call from a TUI outside Meridian.",
        ),
    ] = False,
) -> None:
    """List open stretches with their stored schedules."""

    try:
        rows = _status_rows(interactive=interactive)
        from meridian.cli.main import get_global_options

        if json_mode or get_global_options().output.format == "json":
            _emit_json(rows)
        else:
            _print_status_table(rows)
    except Exception as exc:
        _fail(_error_message(exc))


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
        _fail(_error_message(exc))


__all__ = [
    "cmd_idle_arm",
    "cmd_idle_config",
    "cmd_idle_done",
    "cmd_idle_event",
    "cmd_idle_fire",
    "cmd_idle_mod_path",
    "cmd_idle_return",
    "cmd_idle_status",
]
