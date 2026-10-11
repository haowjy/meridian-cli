#!/usr/bin/env python3
"""Execute concrete native hook payloads through the packaged Claude recorder.

Only the entry ID (assigned in argv) and native store need substitution. This
fixture knows native events and file damage, never test case names or outcomes.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ENTRY_SESSION_TOKEN = "__MERIDIAN_ENTRY_SESSION_ID__"
NATIVE_STORE_TOKEN = "__MERIDIAN_NATIVE_STORE__"


def main() -> None:
    config = json.loads(Path(os.environ["_MERIDIAN_CLAUDE_BOUNDARY_FIXTURE_CONFIG"]).read_text())
    entry = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else ""
    plugins = [Path(sys.argv[i + 1]) for i, arg in enumerate(sys.argv) if arg == "--plugin-dir"]
    recorder = next(
        plugin / "hooks/record.py" for plugin in plugins
        if json.loads((plugin / ".claude-plugin/plugin.json").read_text())["name"]
        == "meridian-session-boundary"
    )

    def expand(value: str) -> str:
        return value.replace(ENTRY_SESSION_TOKEN, entry).replace(
            NATIVE_STORE_TOKEN, config["native_store"],
        )

    for event in config["events"]:
        payload = {**event, "session_id": expand(event["session_id"]),
                   "transcript_path": expand(event["transcript_path"])}
        transcript = Path(payload["transcript_path"])
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"sessionId": payload["session_id"]}) + "\n")
        subprocess.run(
            [sys.executable, str(recorder)], input=json.dumps(payload), text=True, check=True,
        )

    mutation = config.get("transcript_mutation")
    if mutation is not None:
        path = Path(expand(mutation["path"]))
        if mutation["action"] == "delete":
            path.unlink()
        else:
            path.write_text(json.dumps({"sessionId": expand(mutation["header_session_id"])}) + "\n")
    mutation = config.get("receipt_mutation")
    if mutation is not None:
        path = Path(os.environ["_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH"])
        receipt = json.loads(path.read_text())
        if mutation["field"] == "launch_nonce":
            receipt["launch_nonce"] = "old-attempt"
        else:
            receipt["pid"] += 1
        path.write_text(json.dumps(receipt))


if __name__ == "__main__":
    main()
