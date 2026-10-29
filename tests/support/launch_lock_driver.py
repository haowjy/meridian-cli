"""Subprocess fixture: real CLI/runner/gate, fake catalog and harness boundary."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


def fake_harness() -> None:
    if "--version" in sys.argv:
        print("2.0.0")
        return
    root = Path(os.environ["FAKE_ROOT"])
    # Marker files name the spawn, and its launch from the second on: `p1`, then `p1.2`.
    spawn = os.environ["MERIDIAN_SPAWN_ID"]
    state = json.loads((root / "runtime" / "spawns" / spawn / "state.json").read_text())
    launch = (state.get("launch") or {}).get("n", 1)
    if launch > 1:
        spawn = f"{spawn}.{launch}"
    session = next(
        sys.argv[i + 1] for i, arg in enumerate(sys.argv) if arg in ("--session-id", "--resume")
    )
    from meridian.lib.harness.claude import project_slug

    store = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / project_slug(Path.cwd())
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{session}.jsonl").write_text(json.dumps({"sessionId": session}) + "\n")
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    (root / f"{spawn}.pid").write_text(str(os.getpid()))
    child = os.fork()
    if child == 0:
        # Codex's native writer stays in the wrapper's group after wrapper death.
        with (root / "writer.lock").open("w") as writer:
            if os.environ.get("FAKE_SHAPE") == "backend":
                try:
                    fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    (root / f"{spawn}.writer-busy").touch()
                    os._exit(2)
            (root / f"{spawn}.worker").write_text(str(os.getpid()))
            if os.environ.get("FAKE_DETACHED") == "1":
                detached = os.fork()
                if detached == 0:
                    os.setsid()
                    if os.fork():
                        os._exit(0)
                    (root / f"{spawn}.detached").write_text(str(os.getpid()))
                    while True:
                        time.sleep(1)
                os.waitpid(detached, 0)
            while not (root / f"{spawn}.exit").exists():
                time.sleep(0.02)
        os._exit(0)
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
    _, status = os.waitpid(child, 0)
    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        sys.exit(code)
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "OK", "session_id": session, "num_turns": 1,
    }), flush=True)


def main() -> None:
    if sys.argv[1:2] == ["harness"]:
        fake_harness()
        return
    import pytest

    from meridian.lib.core.types import HarnessId
    from meridian.lib.ops.spawn import execute
    from meridian.lib.streaming import control_socket
    from tests.support.launch import stub_bundle_request_and_resolve

    patch = pytest.MonkeyPatch()
    patch.setattr(control_socket, "_POSIX_SOCKET_ROOT", Path(os.environ["FAKE_SOCKET_ROOT"]))
    stub_bundle_request_and_resolve(patch, model="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    patch.setattr(execute, "_build_background_worker_command", lambda **kw: (
        sys.executable, __file__, "worker", "--spawn-id", str(kw["spawn_id"]),
        "--project-root", str(kw["project_paths"].project_root),
        "--runtime-root", str(kw["runtime_root"]),
    ))
    race = os.environ.get("FAKE_RACE")
    if race:
        from meridian.lib.ops.spawn import api

        original = api.begin_continue

        def scheduled_begin(**kwargs: Any) -> Any:
            root = Path(os.environ["FAKE_ROOT"])
            if race == "second":
                while not (root / "first-swept").exists():
                    time.sleep(0.01)
            try:
                return original(**kwargs)
            finally:
                # The loser is refused and the winner holds its lock until it has seen that.
                (root / f"{race}-swept").touch()
                while not (root / "second-swept").exists():
                    time.sleep(0.01)

        patch.setattr(api, "begin_continue", scheduled_begin)
    if sys.argv[1:2] == ["worker"]:
        if os.environ.get("FAKE_HOLD_WORKER") == "1":
            root = Path(os.environ["FAKE_ROOT"])
            spawn_id = next(
                sys.argv[i + 1] for i, arg in enumerate(sys.argv) if arg == "--spawn-id"
            )
            (root / f"{spawn_id}.worker-gate.pid").write_text(str(os.getpid()))
            while not (root / f"{spawn_id}.worker-release").exists():
                time.sleep(0.01)
        from meridian.lib.ops.spawn.execute_bg import _background_worker_main

        raise SystemExit(_background_worker_main(sys.argv[2:]))
    from meridian.cli.entrypoint import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
