from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("email_backend", "expected_code"),
    [("none", 0), ("smtp", 1)],
)
def test_notify_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    email_backend: str,
    expected_code: int,
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
    monkeypatch.setenv("MERIDIAN_NOTIFY_EMAIL_BACKEND", email_backend)
    monkeypatch.setenv("MERIDIAN_NOTIFY_SMTP_USER", "sender@example.com")
    monkeypatch.setenv("MERIDIAN_NOTIFY_EMAIL_TO", "recipient@example.com")
    monkeypatch.chdir(cwd)

    from meridian.cli.main import main

    with pytest.raises(SystemExit) as exc_info:
        main(["notify", "hello", "--json"])

    assert exc_info.value.code == expected_code
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["exit_code"] == expected_code
    if email_backend == "none":
        assert "every selected backend is none" in captured.err
    else:
        # One route, whose error names the failed transport, reported once.
        assert [(row["channel"], row["status"]) for row in payload["results"]] == [
            ("notify", "failed")
        ]
        assert payload["results"][0]["error"].startswith("smtp: ")
        assert captured.err.count("smtp: ") == 1


def test_notify_refuses_a_missing_attachment_before_any_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key in list(os.environ):
        if key.upper().startswith("MERIDIAN"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    monkeypatch.chdir(tmp_path)
    (tmp_path / "meridian.toml").write_text(
        '[[hooks]]\nname = "desk"\nevent = "notify.manual"\ncommand = "touch ran"\n',
        encoding="utf-8",
    )

    from meridian.cli.main import main

    with pytest.raises(SystemExit) as exc_info:
        main(["notify", "Pick auth storage", "--attach", "missing.md"])

    assert exc_info.value.code == 2
    assert "attachment is not a readable file" in capsys.readouterr().err
    assert not (tmp_path / "ran").exists()


def test_notify_splits_a_multiline_title_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key in list(os.environ):
        if key.upper().startswith("MERIDIAN") or key == "TMUX_PANE":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    monkeypatch.setenv("MERIDIAN_NOTIFY_PUSH_BACKEND", "none")
    monkeypatch.setenv("MERIDIAN_NOTIFY_EMAIL_BACKEND", "none")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "meridian.toml").write_text(
        '[[hooks]]\nname = "capture"\nevent = "notify.manual"\n'
        'command = "cat > delivered.json"\n',
        encoding="utf-8",
    )

    from meridian.cli.main import main

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "notify",
                "\nDecision needed\nTradeoffs follow",
                "Explicit body",
                "--no-email",
                "--json",
            ]
        )

    assert exc_info.value.code == 0
    delivery = json.loads((tmp_path / "delivered.json").read_text(encoding="utf-8"))
    assert delivery["notification"]["title"] == "Decision needed"
    assert delivery["notification"]["body"].splitlines()[:2] == [
        "Tradeoffs follow",
        "Explicit body",
    ]
    report = json.loads(capsys.readouterr().out)
    assert sum(result["status"] == "sent" for result in report["results"]) == 1
