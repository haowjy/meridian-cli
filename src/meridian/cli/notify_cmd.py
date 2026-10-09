"""CLI handler for ``meridian notify``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from cyclopts import Parameter

from meridian.cli.app_tree import notify_app
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.config.settings import load_config
from meridian.lib.notify.label import build_session_label
from meridian.lib.notify.notice import Notice
from meridian.lib.notify.service import SendReport, send


def _render_report(report: SendReport, *, json_mode: bool = False) -> None:
    from meridian.cli.main import current_output_sink, get_global_options

    sink = current_output_sink()
    for warning in report.warnings:
        sink.warning(warning)

    failures = tuple(result for result in report.results if result.status == "failed")
    for result in failures:
        message = f"{result.channel}: {result.error or 'delivery failed'}"
        if report.ok:
            sink.warning(message)
        else:
            sink.error(message)

    if json_mode or get_global_options().output.format == "json":
        sink.result(report.to_dict())
        return
    if report.all_none:
        sink.result("Notification delivery is disabled.")
        return
    sent = ", ".join(result.channel for result in report.results if result.ok)
    if sent:
        sink.result(f"Notification sent via {sent}.")


@notify_app.default
def cmd_notify(
    message: Annotated[str, Parameter(help="Message body to send.")],
    *,
    title: Annotated[
        str | None,
        Parameter(name="--title", help="Title appended to the session label."),
    ] = None,
    no_email: Annotated[
        bool,
        Parameter(name="--no-email", help="Send only to the push backend."),
    ] = False,
    priority: Annotated[
        int,
        Parameter(name="--priority", help="Notification priority from 1 to 5."),
    ] = 3,
    json_mode: Annotated[
        bool,
        Parameter(name="--json", help="Print the delivery report as JSON."),
    ] = False,
) -> None:
    """Send one manual notification."""
    from meridian.cli.main import current_output_sink, get_global_options

    sink = current_output_sink()
    if not 1 <= priority <= 5:
        sink.error("--priority must be between 1 and 5", exit_code=2)
        raise SystemExit(2)

    explicit_root = get_global_options().project_root
    project_root = resolve_project_root_resolution(
        Path(explicit_root) if explicit_root is not None else None
    ).project_root
    config = load_config(project_root, resolve_models=False).notify
    label = build_session_label()
    notice = Notice(
        title=label.titled(title),
        body=message,
        priority=priority,
        email=not no_email,
        kind="manual",
    )
    report = send(notice, config).with_label(label)
    _render_report(report, json_mode=json_mode)
    raise SystemExit(report.exit_code)


__all__ = ["cmd_notify"]
