from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels.command import CommandChannel
from meridian.lib.notify.notice import Notice


class _FakeCommandTransport:
    def __init__(self, results: Sequence[subprocess.CompletedProcess[str]]) -> None:
        self._results = iter(results)
        self.calls: list[tuple[list[str], str, float]] = []

    def __call__(
        self,
        argv: list[str],
        stdin: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, stdin, timeout))
        return next(self._results)


_NOTICE = Notice(
    title="[a2] meridian-cli · F1b",
    body="command body",
    priority=4,
    email=True,
    kind="manual",
)


def test_command_sends_notice_json_on_stdin_and_text_on_argv() -> None:
    transport = _FakeCommandTransport(
        [subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")]
    )

    result = CommandChannel(transport=transport).send(
        _NOTICE,
        NotifyConfig(
            push_backend="command",
            email_backend="none",
            push_command="/usr/bin/helper --mode push",
        ),
    )

    assert result.ok is True
    assert len(transport.calls) == 1
    argv, stdin, timeout = transport.calls[0]
    assert argv == [
        "/usr/bin/helper",
        "--mode",
        "push",
        _NOTICE.title,
        _NOTICE.body,
    ]
    assert timeout == 10.0
    assert json.loads(stdin) == {
        "title": _NOTICE.title,
        "body": _NOTICE.body,
        "priority": 4,
        "email": True,
        "kind": "manual",
    }


def test_command_uses_email_command_for_email_backend() -> None:
    transport = _FakeCommandTransport(
        [subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")]
    )

    result = CommandChannel(transport=transport).send(
        _NOTICE,
        NotifyConfig(
            push_backend="none",
            email_backend="command",
            email_command="mailer --format text",
        ),
    )

    assert result.ok is True
    assert transport.calls[0][0][:3] == ["mailer", "--format", "text"]


def test_command_runs_push_and_email_commands_when_both_select_it() -> None:
    transport = _FakeCommandTransport(
        [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]
    )

    result = CommandChannel(transport=transport).send(
        _NOTICE,
        NotifyConfig(
            push_backend="command",
            email_backend="command",
            push_command="push-helper",
            email_command="email-helper",
        ),
    )

    assert result.ok is True
    assert [call[0][0] for call in transport.calls] == ["push-helper", "email-helper"]


def test_command_nonzero_exit_includes_stderr() -> None:
    transport = _FakeCommandTransport(
        [
            subprocess.CompletedProcess(
                args=[],
                returncode=7,
                stdout="",
                stderr="delivery broke\n",
            )
        ]
    )

    result = CommandChannel(transport=transport).send(
        _NOTICE,
        NotifyConfig(
            push_backend="command",
            email_backend="none",
            push_command="helper",
        ),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "status 7" in result.error
    assert "delivery broke" in result.error


def test_command_timeout_is_a_channel_failure() -> None:
    def timeout_transport(
        argv: list[str],
        stdin: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = stdin
        raise subprocess.TimeoutExpired(argv, timeout)

    result = CommandChannel(transport=timeout_transport).send(
        _NOTICE,
        NotifyConfig(
            push_backend="command",
            email_backend="none",
            push_command="helper",
        ),
    )

    assert result.status == "failed"
    assert result.error == "push_command timed out after 10 seconds"
