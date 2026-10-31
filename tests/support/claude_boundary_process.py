#!/usr/bin/env python3
"""Emit native lifecycle payloads through the packaged recorder in a real process."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ENTRY_SESSION_TOKEN = "__MERIDIAN_ENTRY_SESSION_ID__"


def main() -> None:
    config = json.loads(Path(os.environ["_MERIDIAN_CLAUDE_BOUNDARY_FIXTURE_CONFIG"]).read_text())
    entry = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else ""
    plugins = [Path(sys.argv[i + 1]) for i, arg in enumerate(sys.argv) if arg == "--plugin-dir"]
    recorder = next(
        plugin / "hooks/record.py"
        for plugin in plugins
        if json.loads((plugin / ".claude-plugin/plugin.json").read_text())["name"]
        == "meridian-session-boundary"
    )
    for event in config["events"]:
        payload = {
            **event,
            "session_id": event["session_id"].replace(ENTRY_SESSION_TOKEN, entry),
            "transcript_path": event["transcript_path"].replace(ENTRY_SESSION_TOKEN, entry),
        }
        transcript = Path(payload["transcript_path"])
        if event.get("write_transcript", True):
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(json.dumps({"sessionId": payload["session_id"]}) + "\n")
        subprocess.run(
            [sys.executable, str(recorder)],
            input=json.dumps(payload),
            text=True,
            check=True,
        )
    wait_file = config.get("wait_file")
    if wait_file:
        deadline = time.monotonic() + 10
        while not Path(wait_file).exists() and time.monotonic() < deadline:
            time.sleep(0.02)


if __name__ == "__main__":
    main()
