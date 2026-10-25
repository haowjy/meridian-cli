"""CLI handler for ``meridian notify``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from cyclopts import Parameter

from meridian.cli.app_tree import notify_app
from meridian.cli.invocation import current_output_sink, get_global_options
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.notify.emit import RouteReport, emit
from meridian.lib.notify.label import build_session_label
from meridian.lib.notify.notice import SessionLabel, manual_notification


def _route_label(channel: str, transports: tuple[str, ...]) -> str:
    if not transports:
        return channel
    return f"{channel} ({', '.join(transports)})"


def _render_report(
    report: RouteReport, label: SessionLabel, *, json_mode: bool = False
) -> None:
    sink = current_output_sink()
    for warning in report.warnings:
        sink.warning(warning)
    if report.results and report.all_none:
        # A deliberate `none` is quiet for idle; only the person who asked is told.
        sink.warning("notification disabled: every selected backend is none")

    for result in report.results:
        if result.status == "failed":
            message = f"{result.route}: {result.error or 'delivery failed'}"
            if report.ok:
                sink.warning(message)
            else:
                sink.error(message)

    if json_mode or get_global_options().output.format == "json":
        sink.result({**report.to_dict(), "label": label.to_dict()})
        return
    if report.all_none:
        sink.result("Nothing delivered.")
        return
    sent = ", ".join(
        _route_label(result.route, result.transports)
        for result in report.results
        if result.ok
    )
    if sent:
        sink.result(f"Notification sent via {sent}.")


@notify_app.default
def cmd_notify(
    title: Annotated[str, Parameter(help="One line: what the human needs to decide or see.")],
    body: Annotated[
        str | None,
        Parameter(help="Optional detail; attach a file for longer text."),
    ] = None,
    *,
    no_email: Annotated[
        bool,
        Parameter(name="--no-email", help="Keep it off email backends."),
    ] = False,
    priority: Annotated[
        int,
        Parameter(name="--priority", help="Notification priority from 1 to 5."),
    ] = 3,
    attach: Annotated[
        tuple[Path, ...],
        Parameter(
            name="--attach",
            help="A file to send with it; repeat for more (ntfy sends one per message).",
        ),
    ] = (),
    json_mode: Annotated[
        bool,
        Parameter(name="--json", help="Print the delivery report as JSON."),
    ] = False,
) -> None:
    """Send one notification as the `notify.manual` hook event."""
    sink = current_output_sink()
    explicit_root = get_global_options().project_root
    project_root = resolve_project_root_resolution(
        Path(explicit_root) if explicit_root is not None else None
    ).project_root
    label = build_session_label()
    try:
        notification = manual_notification(
            title, body, label, priority=priority, email=not no_email, attachments=attach
        )
    except ValueError as exc:
        sink.error(str(exc), exit_code=2)
        raise SystemExit(2) from None
    report = emit("notify.manual", notification, project_root=project_root, env=os.environ)
    _render_report(report, label, json_mode=json_mode)
    raise SystemExit(report.exit_code)


__all__ = ["cmd_notify"]
