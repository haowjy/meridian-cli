from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.core.lifecycle import LifecycleEvent
from meridian.lib.hooks.dispatch import HookDispatcher
from meridian.lib.hooks.types import (
    Hook,
    HookContext,
    HookEventName,
    HookOutcome,
    HookResult,
    HookWhen,
    SpawnStatus,
)
from meridian.lib.ops.runtime import resolve_runtime_authority_for_write
from meridian.lib.service_context import ApplicationContext


class StubRegistry:
    def __init__(self, hooks: tuple[Hook, ...]) -> None:
        self._hooks = hooks

    def get_hooks_for_event(self, event: HookEventName) -> list[Hook]:
        return list(self._hooks)


class StubIntervalTracker:
    def __init__(self, *, should_run_by_hook: dict[str, bool] | None = None) -> None:
        self._should_run_by_hook = should_run_by_hook or {}
        self.should_run_calls: list[tuple[str, str]] = []
        self.mark_run_calls: list[str] = []

    def should_run(self, hook_name: str, interval: str) -> bool:
        self.should_run_calls.append((hook_name, interval))
        return self._should_run_by_hook.get(hook_name, True)

    def mark_run(self, hook_name: str) -> None:
        self.mark_run_calls.append(hook_name)


class StubExternalRunner:
    def __init__(self, *, results: dict[str, HookResult | Exception]) -> None:
        self._results = results
        self.calls: list[tuple[str, int]] = []
        self.contexts: list[HookContext] = []

    def run(self, hook: Hook, context: HookContext, *, timeout_secs: int) -> HookResult:
        self.calls.append((hook.name, timeout_secs))
        self.contexts.append(context)
        result = self._results[hook.name]
        if isinstance(result, Exception):
            raise result
        return result


class StubBuiltin:
    def __init__(
        self,
        *,
        requirements_ok: bool,
        requirements_error: str | None = None,
        result: HookResult | Exception | None = None,
    ) -> None:
        self._requirements_ok = requirements_ok
        self._requirements_error = requirements_error
        self._result = result
        self.executed = False
        self.name = "git-autosync"
        self.requirements: tuple[str, ...] = ("git",)
        self.default_events: tuple[str, ...] = ("spawn.finalized",)
        self.default_interval: str | None = "10m"

    def check_requirements(self) -> tuple[bool, str | None]:
        return self._requirements_ok, self._requirements_error

    def execute(self, context: HookContext, config: Hook) -> HookResult:
        self.executed = True
        if isinstance(self._result, Exception):
            raise self._result
        if self._result is not None:
            return self._result
        return HookResult(
            hook_name=config.name,
            event=context.event_name,
            outcome="success",
            success=True,
        )


class RecordingLogger:
    def __init__(self) -> None:
        self.info_calls: list[tuple[str, dict[str, object]]] = []
        self.warning_calls: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, /, **kwargs: object) -> None:
        self.info_calls.append((event, kwargs))

    def warning(self, event: str, /, **kwargs: object) -> None:
        self.warning_calls.append((event, kwargs))


def _hook(
    name: str,
    *,
    event: HookEventName = "spawn.finalized",
    command: str | None = "./hook.sh",
    builtin: str | None = None,
    enabled: bool = True,
    when: HookWhen | None = None,
    interval: str | None = None,
    timeout_secs: int | None = None,
) -> Hook:
    return Hook(
        name=name,
        event=event,
        source="project",
        command=command,
        builtin=builtin,
        enabled=enabled,
        when=when,
        interval=interval,
        timeout_secs=timeout_secs,
    )


def _context(
    *,
    event_name: HookEventName = "spawn.finalized",
    spawn_status: SpawnStatus | None = "success",
    spawn_agent: str | None = "reviewer",
) -> HookContext:
    return HookContext(
        event_name=event_name,
        event_id=uuid4(),
        timestamp="2026-04-20T00:00:00+00:00",
        project_root="/repo",
        runtime_root="/repo/.meridian",
        spawn_id="p123",
        spawn_status=spawn_status,
        spawn_agent=spawn_agent,
    )


def _result(
    name: str,
    event_name: HookEventName,
    *,
    outcome: HookOutcome = "success",
    error: str | None = None,
) -> HookResult:
    return HookResult(
        hook_name=name,
        event=event_name,
        outcome=outcome,
        success=outcome == "success",
        error=error,
    )


def test_dispatch_skips_hook_when_event_does_not_match(tmp_path: Path) -> None:
    hook = _hook("notify", event="work.done")
    runner = StubExternalRunner(results={"notify": _result("notify", "work.done")})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
    )

    results = dispatcher.fire(_context(event_name="spawn.finalized"))

    assert len(results) == 1
    assert results[0].outcome == "skipped"
    assert results[0].skip_reason == "event_mismatch"
    assert runner.calls == []


def test_dispatch_skips_disabled_hook(tmp_path: Path) -> None:
    hook = _hook("notify", enabled=False)
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
    )

    results = dispatcher.fire(_context())

    assert results[0].outcome == "skipped"
    assert results[0].skip_reason == "disabled"
    assert runner.calls == []


def test_dispatch_runs_only_when_all_conditions_match(tmp_path: Path) -> None:
    hook = _hook(
        "notify",
        when=HookWhen(status=("success",), agent="reviewer"),
    )
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
    )

    matched = dispatcher.fire(_context(spawn_status="success", spawn_agent="reviewer"))
    mismatched = dispatcher.fire(_context(spawn_status="failure", spawn_agent="reviewer"))

    assert matched[0].outcome == "success"
    assert mismatched[0].outcome == "skipped"
    assert mismatched[0].skip_reason == "condition_not_met"
    assert len(runner.calls) == 1


def test_dispatch_runs_hook_without_when_filters_on_matching_event(tmp_path: Path) -> None:
    hook = _hook("notify", when=None)
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
    )

    result = dispatcher.fire(_context(spawn_status=None, spawn_agent=None))

    assert result[0].outcome == "success"
    assert runner.calls == [("notify", 60)]


def test_dispatch_builds_context_from_shared_authority(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    authority = resolve_runtime_authority_for_write(project_root)
    hook = _hook("notify")
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    dispatcher = HookDispatcher(
        tmp_path / "other-project",
        tmp_path / "other-runtime",
        authority=authority,
        registry=StubRegistry((hook,)),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
    )

    event = LifecycleEvent(
        event_id=uuid4(),
        event_type="spawn.finalized",
        timestamp=datetime.now(UTC),
        spawn_id="p123",
        parent_id=None,
        chat_id="chat-1",
        work_id=None,
        agent="reviewer",
        model="gpt-5.4",
        harness="codex",
        status="succeeded",
        origin="runner",
    )

    dispatcher.on_event(event)

    assert runner.calls == [("notify", 60)]
    assert runner.contexts[0].project_root == authority.project_root.as_posix()
    assert runner.contexts[0].runtime_root == authority.runtime_root.as_posix()


def test_dispatch_from_application_context_requires_runtime_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runtime_root"):
        HookDispatcher.from_application_context(
            ApplicationContext(project_root=tmp_path, runtime_root=None)
        )


def test_dispatch_from_application_context_uses_authority_roots_when_missing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    authority = resolve_runtime_authority_for_write(project_root)

    dispatcher = HookDispatcher.from_application_context(
        ApplicationContext(project_root=None, runtime_root=None, authority=authority),
        registry=StubRegistry(tuple()),
        interval_tracker=StubIntervalTracker(),
        external_runner=StubExternalRunner(results={}),
    )

    assert authority.runtime_root is not None
    assert dispatcher._project_root == authority.project_root.resolve()
    assert dispatcher._runtime_root == authority.runtime_root.resolve()


def test_dispatch_on_event_normalizes_lifecycle_terminal_status(tmp_path: Path) -> None:
    hook = _hook(
        "notify",
        when=HookWhen(status=("success",), agent="reviewer"),
    )
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
    )

    event = LifecycleEvent(
        event_id=uuid4(),
        event_type="spawn.finalized",
        timestamp=datetime.now(UTC),
        spawn_id="p123",
        parent_id=None,
        chat_id="chat-1",
        work_id=None,
        agent="reviewer",
        model="gpt-5.4",
        harness="codex",
        status="succeeded",
        origin="runner",
    )

    dispatcher.on_event(event)

    assert runner.calls == [("notify", 60)]


def test_dispatch_skips_hook_when_interval_is_throttled(tmp_path: Path) -> None:
    hook = _hook("notify", interval="10m")
    tracker = StubIntervalTracker(should_run_by_hook={"notify": False})
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=tracker,
        external_runner=runner,
    )

    results = dispatcher.fire(_context())

    assert results[0].outcome == "skipped"
    assert results[0].skip_reason == "throttled"
    assert tracker.should_run_calls == [("notify", "10m")]
    assert tracker.mark_run_calls == []
    assert runner.calls == []


def test_dispatch_without_interval_runs_on_every_matching_event(tmp_path: Path) -> None:
    hook = _hook("notify", interval=None)
    tracker = StubIntervalTracker()
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=tracker,
        external_runner=runner,
    )

    first = dispatcher.fire(_context())
    second = dispatcher.fire(_context())

    assert [result.outcome for result in first + second] == ["success", "success"]
    assert tracker.should_run_calls == []
    assert runner.calls == [("notify", 60), ("notify", 60)]


def test_dispatch_marks_interval_after_successful_execution(tmp_path: Path) -> None:
    hook = _hook("notify", interval="10m")
    tracker = StubIntervalTracker()
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=tracker,
        external_runner=runner,
    )

    results = dispatcher.fire(_context())

    assert results[0].outcome == "success"
    assert tracker.should_run_calls == [("notify", "10m")]
    assert tracker.mark_run_calls == ["notify"]


@pytest.mark.parametrize(
    ("event_name", "expected_timeout"),
    [
        ("spawn.created", 30),
        ("spawn.finalized", 60),
    ],
)
def test_dispatch_fail_open_on_timeout_for_observe_and_post_events(
    tmp_path: Path,
    event_name: HookEventName,
    expected_timeout: int,
) -> None:
    hooks = (
        _hook("first", event=event_name),
        _hook("second", event=event_name),
    )
    runner = StubExternalRunner(
        results={
            "first": _result("first", event_name, outcome="timeout", error="Timed out after 30s."),
            "second": _result("second", event_name),
        }
    )
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry(hooks),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
    )

    results = dispatcher.fire(_context(event_name=event_name))

    assert [result.hook_name for result in results] == ["first", "second"]
    assert [result.outcome for result in results] == ["timeout", "success"]
    assert runner.calls == [("first", expected_timeout), ("second", expected_timeout)]


def test_dispatch_uses_explicit_hook_timeout_override(tmp_path: Path) -> None:
    hook = _hook("notify", timeout_secs=7)
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
    )

    dispatcher.fire(_context())

    assert runner.calls == [("notify", 7)]


def test_dispatch_executes_builtin_hook_with_internal_implementation(tmp_path: Path) -> None:
    hook = _hook("git-autosync", command=None, builtin="git-autosync")
    builtin = StubBuiltin(
        requirements_ok=True,
        result=HookResult(
            hook_name="git-autosync",
            event="spawn.finalized",
            outcome="success",
            success=True,
            duration_ms=12,
        ),
    )
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=StubIntervalTracker(),
        external_runner=StubExternalRunner(results={}),
        builtin_hooks={"git-autosync": builtin},
    )

    results = dispatcher.fire(_context())

    assert results[0].outcome == "success"
    assert builtin.executed is True


def test_dispatch_skips_builtin_when_requirements_are_missing(tmp_path: Path) -> None:
    hook = _hook("git-autosync", command=None, builtin="git-autosync")
    builtin = StubBuiltin(requirements_ok=False, requirements_error="git not found")
    runner = StubExternalRunner(results={})
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
        builtin_hooks={"git-autosync": builtin},
    )

    results = dispatcher.fire(_context())

    assert results[0].outcome == "skipped"
    assert results[0].skip_reason == "requirements"
    assert results[0].error == "git not found"
    assert builtin.executed is False
    assert runner.calls == []


def test_dispatch_catches_builtin_exception_and_continues_fail_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks = (
        _hook("git-autosync", command=None, builtin="git-autosync"),
        _hook("notify"),
    )
    builtin = StubBuiltin(
        requirements_ok=True,
        result=RuntimeError("builtin exploded"),
    )
    runner = StubExternalRunner(results={"notify": _result("notify", "spawn.finalized")})
    log = RecordingLogger()
    monkeypatch.setattr("meridian.lib.hooks.dispatch.logger", log)
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry(hooks),
        interval_tracker=StubIntervalTracker(),
        external_runner=runner,
        builtin_hooks={"git-autosync": builtin},
    )

    results = dispatcher.fire(_context())

    assert [result.outcome for result in results] == ["failure", "success"]
    assert results[0].error == "builtin exploded"
    assert runner.calls == [("notify", 60)]
    assert log.warning_calls[0][0] == "hook_execution_failed"
    assert log.warning_calls[0][1]["hook"] == "git-autosync"
    assert log.warning_calls[0][1]["hook_event"] == "spawn.finalized"
    assert log.warning_calls[0][1]["error"] == "builtin exploded"


def test_dispatch_logs_throttled_skip_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = _hook("notify", interval="10m")
    tracker = StubIntervalTracker(should_run_by_hook={"notify": False})
    log = RecordingLogger()
    monkeypatch.setattr("meridian.lib.hooks.dispatch.logger", log)
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=StubRegistry((hook,)),
        interval_tracker=tracker,
        external_runner=StubExternalRunner(
            results={"notify": _result("notify", "spawn.finalized")}
        ),
    )

    dispatcher.fire(_context())

    skipped = [
        kwargs
        for event, kwargs in log.info_calls
        if event == "hook_execution_skipped"
    ]
    assert skipped == [
        {
            "hook": "notify",
            "hook_event": "spawn.finalized",
            "reason": "throttled",
        }
    ]
