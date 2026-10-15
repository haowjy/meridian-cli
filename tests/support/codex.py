"""Faithful Codex rollout fixtures for native transcript readers."""

import json
from pathlib import Path


def write_codex_rollout(
    *,
    sessions_root: Path,
    project_root: Path,
    session_id: str,
    assistant_text: str | None = None,
    date_dirs: tuple[str, ...] = ("2026", "04"),
) -> Path:
    rollout_dir = sessions_root.joinpath(*date_dirs)
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout_path = rollout_dir / f"rollout-2026-04-22T00-00-00-{session_id}.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": project_root.as_posix()},
        }
    ]
    if assistant_text is not None:
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": assistant_text}],
                },
            }
        )
    rollout_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return rollout_path

