"""External-command notification channel."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from meridian.lib.config.settings import NotifyConfig
from meridian.lib.notify.channels.base import SendResult
from meridian.lib.notify.notice import Notice

_COMMAND_TIMEOUT_SECONDS = 10.0

type CommandTransport = Callable[
    [list[str], str, float], subprocess.CompletedProcess[str]
]


def run_command(
    argv: list[str],
    stdin: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run one configured command behind the injectable process boundary."""
    return subprocess.run(
        argv,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _selected_commands(notice: Notice, cfg: NotifyConfig) -> list[tuple[str, str | None]]:
    commands: list[tuple[str, str | None]] = []
    if cfg.push_backend.strip().lower() == "command":
        commands.append(("push_command", cfg.push_command))
    if notice.email and cfg.email_backend.strip().lower() == "command":
        commands.append(("email_command", cfg.email_command))
    return commands


def _notice_json(notice: Notice) -> str:
    return json.dumps(
        {
            "title": notice.title,
            "body": notice.body,
            "priority": notice.priority,
            "email": notice.email,
            "kind": notice.kind,
        },
        ensure_ascii=False,
    )


@dataclass(frozen=True)
class CommandChannel:
    """Deliver notices to configured programs without invoking a shell."""

    name = "command"
    transport: CommandTransport = run_command

    def send(self, notice: Notice, cfg: NotifyConfig) -> SendResult:
        selected = _selected_commands(notice, cfg)
        if not selected:
            return SendResult(
                channel=self.name,
                status="failed",
                error="command backend is not selected",
            )

        prepared: list[tuple[str, list[str]]] = []
        for field, raw_command in selected:
            if raw_command is None or not raw_command.strip():
                return SendResult(
                    channel=self.name,
                    status="failed",
                    error=f"{field} is unset",
                )
            try:
                argv = shlex.split(raw_command)
            except ValueError as exc:
                return SendResult(
                    channel=self.name,
                    status="failed",
                    error=f"invalid {field}: {exc}",
                )
            if not argv:
                return SendResult(
                    channel=self.name,
                    status="failed",
                    error=f"{field} is empty",
                )
            prepared.append((field, [*argv, notice.title, notice.body]))

        payload = _notice_json(notice)
        errors: list[str] = []
        for field, argv in prepared:
            try:
                completed = self.transport(argv, payload, _COMMAND_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                errors.append(f"{field} timed out after 10 seconds")
                continue
            except Exception as exc:  # external process boundary
                errors.append(f"{field} failed: {exc}")
                continue

            if completed.returncode != 0:
                detail = completed.stderr.strip()
                message = f"{field} exited with status {completed.returncode}"
                errors.append(f"{message}: {detail}" if detail else message)

        if errors:
            return SendResult(channel=self.name, status="failed", error="; ".join(errors))
        return SendResult(channel=self.name, status="sent")


CHANNEL = CommandChannel()

__all__ = ["CHANNEL", "CommandChannel", "CommandTransport", "run_command"]
