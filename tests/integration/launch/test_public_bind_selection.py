"""Public bind reconciles every independently supplied selection before lookup."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import meridian.lib.launch.context as launch_context
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import RuntimeBindings, bind_launch_context
from meridian.lib.launch.request import LaunchRuntime, SessionRequest, SpawnRequest
from meridian.lib.ops.reference import UntrackedSourceUse


def _prepared(tmp_path: Path, session: SessionRequest):
    registry = get_default_harness_registry()
    request = SpawnRequest(prompt="", harness="claude", session=session)
    prepared = launch_context._build_direct_surface(
        request=request,
        project_root=tmp_path,
        reference_anchor=tmp_path,
        harness_registry=registry,
    )
    runtime = LaunchRuntime(
        runtime_root=(tmp_path / ".meridian").as_posix(),
        config_root=tmp_path.as_posix(),
        control_root=tmp_path.as_posix(),
    )
    return prepared, runtime, registry


def _bind(prepared, runtime, registry, *, bindings=None):
    return bind_launch_context(
        prepared=prepared,
        bindings=bindings or RuntimeBindings(spawn_id="bind-test"),
        runtime=runtime,
        project_root=Path(runtime.config_root),
        harness_registry=registry,
    )


def _stub_effects(monkeypatch):
    lookups: list[tuple[str | None, str | None, str]] = []
    materializations: list[bool] = []

    def resolve_source_use(runtime_root, operation, native_id, harness):
        lookups.append((native_id, native_id, operation))
        return UntrackedSourceUse(
            operation=operation,
            original_ref=native_id,
            native_id=native_id,
            harness=harness,
            lookup_scope=runtime_root,
        )

    def materialize(**kwargs):
        materializations.append(True)
        return SimpleNamespace()

    monkeypatch.setattr("meridian.lib.ops.reference.resolve_source_use", resolve_source_use)
    monkeypatch.setattr(launch_context, "_bind_launch_context_impl", materialize)
    return lookups, materializations


def test_public_bind_refuses_request_a_seed_b_before_lookup_or_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, runtime, registry = _prepared(
        tmp_path,
        SessionRequest(requested_harness_session_id="native-A", continue_source_ref="native-A"),
    )
    prepared = prepared.__class__(
        **{
            **prepared.__dict__,
            "runtime_seeds": launch_context.PreparedLaunchRuntimeSeeds(
                seed_harness_session_id="native-B"
            ),
        }
    )
    lookups, materializations = _stub_effects(monkeypatch)

    with pytest.raises(ValueError, match="source selection conflict"):
        _bind(prepared, runtime, registry)

    assert lookups == []
    assert materializations == []


@pytest.mark.parametrize("override", ["seed", "fork"])
def test_public_bind_refuses_fresh_caller_continuation_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    prepared, runtime, registry = _prepared(tmp_path, SessionRequest())
    bindings = RuntimeBindings(
        spawn_id="bind-test",
        forked_harness_session_id="native-B" if override == "fork" else None,
    )
    if override == "seed":
        prepared = prepared.__class__(
            **{
                **prepared.__dict__,
                "runtime_seeds": launch_context.PreparedLaunchRuntimeSeeds(
                    seed_harness_session_id="native-B"
                ),
            }
        )
    lookups, materializations = _stub_effects(monkeypatch)

    with pytest.raises(ValueError, match="source selection conflict"):
        _bind(prepared, runtime, registry, bindings=bindings)

    assert lookups == []
    assert materializations == []


def test_public_bind_refuses_caller_fork_override_for_fresh_and_fork_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, runtime, registry = _prepared(tmp_path, SessionRequest())
    lookups, materializations = _stub_effects(monkeypatch)

    with pytest.raises(ValueError, match="caller-supplied fork override"):
        _bind(
            prepared,
            runtime,
            registry,
            bindings=RuntimeBindings(spawn_id="bind-test", continue_fork_override=False),
        )

    assert lookups == []
    assert materializations == []


def test_public_bind_accepts_native_fork_source_without_claiming_a_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, runtime, registry = _prepared(
        tmp_path,
        SessionRequest(
            requested_harness_session_id="native-A",
            continue_source_ref="native-A",
            continue_fork=True,
            primary_session_mode="fork",
        ),
    )
    lookups, materializations = _stub_effects(monkeypatch)

    _bind(prepared, runtime, registry)

    assert lookups == [("native-A", "native-A", "fork")]
    assert materializations == [True]


def test_public_bind_performs_one_lookup_for_consistent_untracked_native_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, runtime, registry = _prepared(
        tmp_path,
        SessionRequest(requested_harness_session_id="native-A", continue_source_ref="native-A"),
    )
    lookups, materializations = _stub_effects(monkeypatch)

    _bind(prepared, runtime, registry)

    assert lookups == [("native-A", "native-A", "resume")]
    assert materializations == [True]


@pytest.mark.parametrize("resolved_ref", [None, "c18"], ids=["source-dropped", "alias-changed"])
def test_public_bind_rejects_changed_prepared_alias_before_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_ref: str | None,
) -> None:
    prepared, runtime, registry = _prepared(
        tmp_path,
        SessionRequest(requested_harness_session_id="native-A", continue_source_ref="c17"),
    )
    prepared = prepared.__class__(
        **{
            **prepared.__dict__,
            "request": prepared.request.model_copy(
                update={
                    "session": prepared.request.session.model_copy(
                        update={"continue_source_ref": resolved_ref}
                    )
                }
            ),
        }
    )
    lookups, materializations = _stub_effects(monkeypatch)

    with pytest.raises(ValueError, match="source selection conflict"):
        _bind(prepared, runtime, registry)

    assert lookups == []
    assert materializations == []


def test_public_bind_rejects_actual_adapter_harness_mismatch_before_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, runtime, registry = _prepared(
        tmp_path,
        SessionRequest(requested_harness_session_id="native-A", continue_source_ref="native-A"),
    )
    prepared = prepared.__class__(
        **{
            **prepared.__dict__,
            "harness": get_default_harness_registry().get_subprocess_harness("codex"),
        }
    )
    lookups, materializations = _stub_effects(monkeypatch)

    with pytest.raises(ValueError, match="source selection conflict"):
        _bind(prepared, runtime, registry)

    assert lookups == []
    assert materializations == []


def test_public_bind_accepts_genuine_fresh_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, runtime, registry = _prepared(tmp_path, SessionRequest())
    lookups, materializations = _stub_effects(monkeypatch)

    _bind(prepared, runtime, registry)

    assert lookups == []
    assert materializations == [True]
