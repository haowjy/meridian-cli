"""The detached process that fires inbox.received: ``python -m`` this module."""

from __future__ import annotations

import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from meridian.lib.hooks.config import load_machine_hooks_config_report
from meridian.lib.hooks.dispatch import HookDispatcher
from meridian.lib.hooks.registry import HookRegistry
from meridian.lib.hooks.types import HookContext
from meridian.lib.inbox import prune_if_due, write_hook_log
from meridian.lib.inbox.models import HookLog, HookLogRow


def fire_inbox_received(message_id: str, message_path: Path) -> HookLog:
    """Run the user config's inbox.received rows for one message and say what each did."""

    fired_at = datetime.now(UTC)
    rows: list[HookLogRow] = []
    errors: list[str] = []
    # This process's stderr is /dev/null, so whatever stops dispatch goes in the log.
    try:
        loaded = load_machine_hooks_config_report()
        errors.extend(loaded.errors)
        context = HookContext.from_roots(
            project_root=None,
            runtime_root=None,
            event_name="inbox.received",
            event_id=uuid4(),
            timestamp=fired_at.isoformat(),
            inbox_message_id=message_id,
            inbox_message_path=message_path,
        )
        dispatcher = HookDispatcher(None, None, registry=HookRegistry(loaded.config))
        for result in dispatcher.fire(context):
            rows.append(
                HookLogRow(
                    name=result.hook_name,
                    outcome=result.outcome,
                    exit_code=result.exit_code,
                    stdout_tail=result.stdout or "",
                    stderr_tail=result.stderr or "",
                )
            )
            if result.error:
                errors.append(f"hook {result.hook_name!r}: {result.error}")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return HookLog(fired_at=fired_at, rows=rows, errors=errors)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: machine_event MESSAGE_ID MESSAGE_PATH")
    message_id = sys.argv[1]
    log = fire_inbox_received(message_id, Path(sys.argv[2]))
    with suppress(Exception):
        write_hook_log(message_id, log)
    with suppress(Exception):
        prune_if_due(datetime.now(UTC))


if __name__ == "__main__":
    main()
