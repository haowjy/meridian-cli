"""Inspection of complete runner-writer fixtures (removed with the writer in PR3)."""

import json
from pathlib import Path


def written_events(path: Path) -> list[dict[str, object]]:
    return [
        event
        for line in path.read_text().splitlines()
        if (event := json.loads(line)).get("record") != "meridian.transcript"
    ]
