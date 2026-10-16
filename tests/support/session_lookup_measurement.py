"""Opt-in built-wheel probe for M18; runs only against private /tmp copies.

PYTHONPATH=<unpacked-wheel> uv run python -m tests.support.session_lookup_measurement
    <readonly-copy> <scratch-run> <operation>

Each invocation is a fresh process. Setup/cold-index build are outside the timer.
Count iter_jsonl_file (not its wrappers) once per physical filtered/full pass.
"""

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from tests.support import io_probe


def main() -> None:
    source, scratch = map(Path, sys.argv[1:3])
    operation = sys.argv[3]
    for path in (source, scratch):
        if not path.resolve().is_relative_to("/tmp"):
            raise ValueError("Private /tmp copies only")
    events = []
    io_probe.install(events.append)
    from meridian.lib.ops.legacy_native_import import MARKER, bind_late_legacy_sessions
    from meridian.lib.ops.session_archive import archive_history
    from meridian.lib.state import session_store as s
    from meridian.lib.state.history_index import HistoryIndex
    from tests.support.session_close_probe import close, prepare_close

    root = scratch / "home/projects" / source.name
    if not root.exists():
        shutil.copytree(source, root)
    os.environ["MERIDIAN_HOME"] = str(root.parent.parent)
    os.environ["_MERIDIAN_RUNTIME_DIR"] = str(root)
    project = scratch / "project"
    project.mkdir(exist_ok=True)
    (project / "meridian.toml").write_text(f'[project]\nid="{root.name}"\n')
    # Pure read workloads leave copied authority read-only; close mutates only its clone.
    if operation == "close":
        (root / "sessions.jsonl").chmod(0o600)
        key = prepare_close(project, root)
    else:
        key = ""
    marker = json.loads((root / MARKER).read_text())
    pending = set(marker["unbound"]["no_session_id"]) - set(marker.get("late_retries", []))
    # Select the most-aliased native key independently of the new native_ids seam.
    aliases = {}
    raw_rows = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
    for row in raw_rows:
        record = row.get("record", {}) if row.get("event") == "historical_import" else row
        if record.get("chat_id") and record.get("harness_session_id"):
            aliases.setdefault(record["harness_session_id"], set()).add(record["chat_id"])
    native = max(aliases, key=lambda key: (len(aliases[key]), key))
    selections = [
        s.SessionModelSelectionEvent.model_validate(row)
        for row in raw_rows if row.get("event") == "model_selection"
        and row.get("harness_session_id") == native
    ]
    selection = selections[-1]
    harness = selection.harness
    # Build and catch up both namespaces before timing, including close's fixture.
    HistoryIndex(root).spawns()
    if hasattr(s, "chats_rows"):
        from meridian.lib.launch.session_scope import warm_close_imports

        warm_close_imports().join()
    before = hashlib.sha256((root / "sessions.jsonl").read_bytes()).hexdigest()
    events.clear()
    started = time.perf_counter()
    cpu = time.process_time()
    if operation == "bind_late":
        result = bind_late_legacy_sessions(root)
    elif operation == "records":
        result = s.get_session_records(root, pending)
    elif operation == "record_model":
        result = s.record_model_selection(root, selection)
        assert result is False
    elif operation == "get_model":
        result = s.get_model_selection(root, harness, native)
        assert result is not None
    elif operation == "resolve":
        result = s.resolve_session_ref(root, native, harness=harness)
        assert result is not None
    elif operation == "archive":
        result = archive_history(root, destination=scratch / "archives", eligible=True)
    elif operation == "close":
        result = close(project, root, key)
    else:
        raise ValueError(operation)
    seconds = time.perf_counter() - started
    cpu = time.process_time() - cpu
    passes = [event for event in events if event["kind"] == "iter_jsonl_file"]
    holds = {}
    for event in events:
        if event["kind"] == "lock_hold":
            name = Path(event["path"]).name
            holds[name] = holds.get(name, 0) + event["hold_ns"] / 1e9
    after = hashlib.sha256((root / "sessions.jsonl").read_bytes()).hexdigest()
    assert before == after, "Measured operations must not change session authority"
    print(json.dumps(dict(
        operation=operation, corpus=source.name, seconds=seconds, cpu_seconds=cpu,
        filtered=sum(event["filtered"] for event in passes),
        unfiltered=sum(not event["filtered"] for event in passes), lock_seconds=holds,
        pending=len(pending), aliases=len(aliases[native]), native=native,
        result=str(result)[:500], module=s.__file__, errors=0,
    )), flush=True)


if __name__ == "__main__":
    main()
