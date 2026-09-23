"""Synthetic Pi RPC peer: only stdio and files inside its test working directory."""

import json
import os
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("pi 0.87.1")
    sys.exit()
if "--help" in sys.argv:
    print(
        "--mode rpc --model --append-system-prompt --session --fork --session-dir "
        "--no-extensions --no-skills --no-context-files --no-prompt-templates "
        "-e --extension PI_CODING_AGENT_SESSION_DIR"
    )
    sys.exit()

scenario = os.environ.get("SCENARIO", "normal")
state = "A"
old_id = None
query_count = 0


def emit(value):
    print(json.dumps(value), flush=True)


def barrier(name):
    deadline = time.monotonic() + 10
    while not Path(name).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Test did not release " + name)
        time.sleep(0.005)


prefix = ""
if scenario == "blocked_input":
    prefix = sys.stdin.read(1)
    Path("input-started").touch()
    barrier("release-input")

for line in sys.stdin:
    command = json.loads(prefix + line)
    prefix = ""
    kind = command["type"]
    with Path("inbound.jsonl").open("a") as log:
        log.write(json.dumps(command) + "\n")
    response = {"type": "response", "id": command.get("id"), "command": kind, "success": True}
    if kind == "prompt":
        Path("prompt-recorded").touch()
    if kind == "abort":
        break
    if kind == "get_state":
        query_count += 1
        if scenario == "eof":
            os.close(1)
            continue  # Keep process alive: EOF must fail waiters before process.wait().
        if scenario == "parse":
            print("{bad json", flush=True)
            continue
        if scenario == "long_line":
            print("x" * (11 * 1024 * 1024), flush=True)
            continue
        if scenario in ("late", "switch_barrier") and query_count == 1:
            barrier("release-query")
        if scenario == "overflow":
            for n in range(400):
                emit({"type": "message_update", "n": n})
        emit({"type": "extension_error", "error": "synthetic observation only"})
        if old_id is not None:
            emit(
                {**response, "id": old_id, "data": {"sessionId": "STALE", "sessionFile": "/STALE"}}
            )
        emit(
            {**response, "id": "wrong-id", "data": {"sessionId": "WRONG", "sessionFile": "/WRONG"}}
        )
        if scenario == "wrong_only":
            continue
        response["data"] = {
            "sessionId": state,
            "sessionFile": str(Path(state + ".jsonl").absolute()),
        }
        if scenario == "wrong_command":
            response["command"] = "prompt"
        if scenario == "rejected":
            response["success"] = False
        if scenario == "truthy":
            response["success"] = 1
        if scenario == "bad_state":
            response["data"]["sessionFile"] = None
        emit(response)
        emit({**response, "data": {"sessionId": "DUPLICATE", "sessionFile": "/DUPLICATE"}})
        old_id = command["id"]
    elif kind == "switch_session":
        barrier("release-switch")
        if scenario == "cancelled_switch":
            response["data"] = {"cancelled": True}
        else:
            state = "B"
        emit(response)
    elif kind == "prompt":
        if scenario == "silent_prompt":
            continue
        if scenario == "pending_prompt":
            continue
        emit(response)
        if scenario == "ack_then_eof":
            break
        emit({"type": "agent_start"})
    elif kind == "steer":
        emit(response)
