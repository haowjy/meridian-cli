from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def test_notify_json_all_none_exits_zero_without_project_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    for key in list(os.environ):
        if key.upper().startswith("MERIDIAN") or key == "TMUX_PANE":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setenv("MERIDIAN_NOTIFY_PUSH_BACKEND", "none")
    monkeypatch.setenv("MERIDIAN_NOTIFY_EMAIL_BACKEND", "none")
    monkeypatch.chdir(cwd)

    from meridian.cli.main import main

    with pytest.raises(SystemExit) as exc_info:
        main(["notify", "hello", "--json"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["label"]["text"] == "checkout"
    assert payload["results"] == [
        {
            "channel": "none",
            "error": None,
            "ok": False,
            "status": "none",
            "warning": None,
        }
    ]
    assert "every selected backend is none" in captured.err
