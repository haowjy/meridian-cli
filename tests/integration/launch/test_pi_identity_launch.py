"""Managed Pi launches use assigned native keys, never newest-file discovery."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.state import session_store
from tests.support.executables import prepend_fake_executables
from tests.support.launch import build_launch_context, stub_bundle_request_and_resolve
from tests.support.pi_extensions import configure_pi_extension_projection

HELP = (
    "--mode rpc --model --append-system-prompt --session --session-id --fork "
    "--session-dir --no-extensions -e --extension PI_CODING_AGENT_SESSION_DIR"
)


@pytest.fixture
def pi_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_PI_BINARY", raising=False)
    monkeypatch.delenv("PI_CACHE_RETENTION", raising=False)
    configure_pi_extension_projection(monkeypatch, tmp_path)
    stub_bundle_request_and_resolve(monkeypatch, model="pi-test", harness=HarnessId.PI)
    prepend_fake_executables(monkeypatch, tmp_path, "pi")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mars.toml").write_text('[settings]\ntargets = [".pi"]\n')
    return root


def install_shim(root: Path, *, behavior: str = "ok") -> None:
    """Only external Pi is fake; bind, argv, journal, transport and exit are real."""
    scratch = root.parent
    shim = scratch / "fake-bin" / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "1.1.0"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo {shlex.quote(HELP)}; exit 0; fi\n'
        f"cp {shlex.quote(str(root / '.meridian' / 'sessions.jsonl'))} "
        f"{shlex.quote(str(scratch / 'binding-at-exec'))}\n"
        f'printf "%s\\n" "$@" > {shlex.quote(str(scratch / "argv"))}\n'
        'printf "%s\\n" "$PI_CODING_AGENT_SESSION_DIR" > '
        f"{shlex.quote(str(scratch / 'env-store'))}\n"
        "id=; source=; session=; rpc=\n"
        'while [ "$#" -gt 0 ]; do\n'
        ' case "$1" in\n'
        " --session-id) shift; id=$1 ;;\n"
        " --session-dir) shift; store=$1 ;;\n"
        " --fork) shift; source=$1 ;;\n"
        " --session) shift; session=$1 ;;\n"
        " --mode) shift; rpc=$1 ;;\n"
        " esac; shift\ndone\n"
        + ("exit 7\n" if behavior == "fail" else "")
        + 'mkdir -p "$store"\n'
        + ("header_id=replacement\n" if behavior == "mismatch" else "header_id=$id\n")
        + ("source=wrong-parent\n" if behavior == "wrong-parent" else "")
        + 'if [ -n "$id" ]; then\n'
        ' printf \'{"type":"session","id":"%s","parentSession":"%s"}\\n\' '
        '"$header_id" "$source" > "$store/1_$id.jsonl"\nfi\n'
        # Concurrent unrelated writer must not influence the bound key.
        'printf \'{"type":"session","id":"unrelated-newest"}\\n\''
        ' > "$store/9_unrelated-newest.jsonl"\n'
        + (
            'if [ -n "$session" ]; then printf \'{"type":"session","id":"replacement"}\\n\' '
            '> "$session"; fi\n'
            if behavior == "mismatch"
            else ""
        )
        + 'if [ "$rpc" != "rpc" ]; then exit 0; fi\n'
        "while IFS= read -r line; do\n"
        ' case "$line" in\n'
        ' *\'"type":"prompt"\'*)\n'
        " printf '%s\\n' '{\"type\":\"agent_start\"}' "
        '\'{"type":"agent_end","messages":[{"role":"assistant","stopReason":"stop",'
        '"content":[{"type":"text","text":"done"}]}]}\' '
        '\'{"type":"agent_settled","aborted":false}\' ;;\n'
        ' *\'"type":"abort"\'*) exit 0 ;;\n'
        " esac\ndone\n"
    )


def context(
    root: Path,
    *,
    primary: bool = True,
    spawn_id: str = "p42",
    session: SessionRequest | None = None,
    extra_args: tuple[str, ...] = (),
):
    request = SpawnRequest(
        harness="pi",
        model="pi-test",
        prompt="hello",
        session=session or SessionRequest(),
        extra_args=extra_args,
        launch_policy_snapshot=(
            LaunchPolicySnapshot(model="pi-test", harness="pi") if session else None
        ),
    )
    return build_launch_context(
        spawn_id=spawn_id,
        request=request,
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=(
                LaunchCompositionSurface.PRIMARY if primary else LaunchCompositionSurface.DIRECT
            ),
            runtime_root=str(root / ".meridian"),
            project_paths_project_root=str(root),
            project_paths_execution_cwd=str(root),
        ),
        harness_registry=HarnessRegistry.with_defaults(),
        dry_run=primary,
    )


def assert_prebound(root: Path, chat_id: str) -> tuple[str, Path]:
    argv = (root.parent / "argv").read_text().splitlines()
    native_id = argv[argv.index("--session-id") + 1]
    store = Path(argv[argv.index("--session-dir") + 1])
    record = session_store.chat_record(root / ".meridian", chat_id)
    assert record is not None
    assert record.harness_session_id == native_id
    assert record.native_store == str(store)
    assert (root.parent / "env-store").read_text().strip() == str(store)
    at_exec = [
        json.loads(line) for line in (root.parent / "binding-at-exec").read_text().splitlines()
    ]
    assert any(
        event.get("harness_session_id") == native_id and event.get("native_store") == str(store)
        for event in at_exec
    )
    return native_id, store


@pytest.mark.parametrize(
    ("primary", "configured_retention", "expected_retention"),
    [
        (True, None, "long"),
        (True, "short", "short"),
        (False, None, None),
    ],
)
def test_pi_cache_retention_default_is_primary_only(
    pi_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary: bool,
    configured_retention: str | None,
    expected_retention: str | None,
) -> None:
    if configured_retention is not None:
        monkeypatch.setenv("PI_CACHE_RETENTION", configured_retention)

    child_env = context(pi_runtime, primary=primary).binding.environment.final_env

    assert child_env.get("PI_CACHE_RETENTION") == expected_retention


@pytest.mark.parametrize("primary", [True, False])
@pytest.mark.parametrize(
    "flag",
    [
        "--session-id",
        "--session",
        "--fork",
        "-c",
        "--continue",
        "-r",
        "--resume",
        "--session-dir",
        "--no-session",
    ],
)
def test_managed_passthrough_refuses_identity_flags(
    pi_runtime: Path, primary: bool, flag: str
) -> None:
    install_shim(pi_runtime)
    with pytest.raises(ValueError, match="managed identity refuses"):
        context(pi_runtime, primary=primary, extra_args=(flag,))
    assert not (pi_runtime.parent / "argv").exists()


@pytest.mark.parametrize("primary", [True, False])
def test_collision_refuses_before_exec(
    pi_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary: bool,
) -> None:
    install_shim(pi_runtime)
    planned = context(pi_runtime, primary=primary).binding.spec.native_identity
    assert planned is not None and planned.native_store is not None
    store = Path(planned.native_store)
    store.mkdir(parents=True, exist_ok=True)
    (store / "unrelated-basename.jsonl").write_text('{"type":"session","id":"chosen-id"}\n')
    monkeypatch.setattr("meridian.lib.harness.pi_identity.uuid.uuid4", lambda: "chosen-id")
    with pytest.raises(ValueError, match="native_identity_collision"):
        context(pi_runtime, primary=primary)
    assert not (pi_runtime.parent / "argv").exists()


