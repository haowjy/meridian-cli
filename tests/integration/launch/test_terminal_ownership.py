from __future__ import annotations

import errno
import os
import select
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from meridian.lib.launch.constants import LAUNCHER_LOG_FILENAME
from meridian.lib.platform import IS_WINDOWS, pty


def _run_script_in_pty(script_path: Path, *args: Path) -> str:
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, str(script_path), *(str(arg) for arg in args)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    terminal_output = bytearray()
    while process.poll() is None:
        ready, _, _ = select.select([master_fd], [], [], 0.1)
        if ready:
            try:
                terminal_output.extend(os.read(master_fd, 4096))
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
    while True:
        try:
            chunk = os.read(master_fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        terminal_output.extend(chunk)
    os.close(master_fd)
    process.wait(timeout=5)

    assert process.returncode == 0, terminal_output.decode(errors="replace")
    return terminal_output.decode()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY contract")
def test_primary_tui_window_captures_launcher_threads_and_children(
    tmp_path: Path,
) -> None:
    diagnostics_path = tmp_path / LAUNCHER_LOG_FILENAME
    script_path = tmp_path / "exercise_window.py"
    script_path.write_text(
        textwrap.dedent(
            """
            import os
            import subprocess
            import sys
            import threading
            from pathlib import Path

            from meridian.lib.launch.process.runner import run_primary_process_with_capture
            from meridian.lib.launch.process.native_launcher import NativeProcessLauncher
            from meridian.lib.launch.process.terminal_ownership import (
                format_launcher_diagnostics_pointer,
            )

            diagnostics_path = Path(sys.argv[1])
            def on_child_started(_pid):
                thread = threading.Thread(
                    target=lambda: os.write(2, b"thread diagnostic\\n")
                )
                thread.start()
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os; "
                            "os.write(1, f'child stdin isatty={os.isatty(0)}\\\\n'.encode()); "
                            "os.write(1, b'child stdout\\\\n'); "
                            "os.write(2, b'child stderr\\\\n')"
                        ),
                    ],
                    check=True,
                )
                thread.join()

            result = run_primary_process_with_capture(
                (
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'TUI frame\\\\n')",
                ),
                Path.cwd(),
                dict(os.environ),
                None,
                NativeProcessLauncher(),
                on_child_started,
                diagnostics_path,
            )
            assert result.exit_code == 0
            assert result.pid is not None
            assert result.launcher_diagnostics_path == diagnostics_path.resolve()
            print("Session finished.")
            print("To continue with meridian: meridian --continue c1")
            print(format_launcher_diagnostics_pointer(result.launcher_diagnostics_path))
            """
        )
    )

    terminal_text = _run_script_in_pty(script_path, diagnostics_path)
    pointer = f"Launcher output during the TUI was written to {diagnostics_path.resolve()}"
    assert "TUI frame" in terminal_text
    assert "thread diagnostic" not in terminal_text
    assert "child stdout" not in terminal_text
    assert "child stderr" not in terminal_text
    assert terminal_text.count(pointer) == 1
    assert terminal_text.index("To continue with meridian") < terminal_text.index(pointer)

    diagnostics = diagnostics_path.read_text()
    assert diagnostics_path.name == LAUNCHER_LOG_FILENAME
    assert "thread diagnostic" in diagnostics
    assert "child stdin isatty=False" in diagnostics
    assert "child stdout" in diagnostics
    assert "child stderr" in diagnostics


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY contract")
def test_tui_window_reports_new_diagnostics_when_exception_escapes(
    tmp_path: Path,
) -> None:
    diagnostics_path = tmp_path / LAUNCHER_LOG_FILENAME
    script_path = tmp_path / "exercise_exception_pointer.py"
    script_path.write_text(
        textwrap.dedent(
            """
            import os
            import sys
            from pathlib import Path

            from meridian.lib.launch.process.terminal_ownership import tui_terminal_window

            diagnostics_path = Path(sys.argv[1])
            try:
                with tui_terminal_window(diagnostics_path):
                    os.write(2, b"new launcher diagnostic\\n")
                    raise RuntimeError("launch failed")
            except RuntimeError:
                print("exception caught")
            """
        )
    )

    terminal_text = _run_script_in_pty(script_path, diagnostics_path)

    pointer = f"Launcher output during the TUI was written to {diagnostics_path.resolve()}"
    assert terminal_text.count(pointer) == 1
    assert terminal_text.index(pointer) < terminal_text.index("exception caught")
    assert "new launcher diagnostic" not in terminal_text
    assert diagnostics_path.read_text(encoding="utf-8") == "new launcher diagnostic\n"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY contract")
@pytest.mark.parametrize("prior_diagnostics", (None, "stale launcher diagnostic\n"))
def test_tui_window_does_not_report_diagnostics_when_exception_adds_nothing(
    tmp_path: Path,
    prior_diagnostics: str | None,
) -> None:
    diagnostics_path = tmp_path / LAUNCHER_LOG_FILENAME
    script_path = tmp_path / "exercise_exception_without_diagnostics.py"
    script_path.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path
            import sys

            from meridian.lib.launch.process.terminal_ownership import tui_terminal_window

            diagnostics_path = Path(sys.argv[1])
            prior_diagnostics = {prior_diagnostics!r}
            if prior_diagnostics is not None:
                diagnostics_path.write_text(prior_diagnostics, encoding="utf-8")
            try:
                with tui_terminal_window(diagnostics_path):
                    raise RuntimeError("launch failed")
            except RuntimeError:
                print("exception caught")
            """
        )
    )

    terminal_text = _run_script_in_pty(script_path, diagnostics_path)

    assert "exception caught" in terminal_text
    assert "Launcher output during the TUI was written to" not in terminal_text
    assert diagnostics_path.read_text(encoding="utf-8") == (prior_diagnostics or "")


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY contract")
def test_pty_relay_owns_terminal_fds_after_window_closes(tmp_path: Path) -> None:
    diagnostics_path = tmp_path / LAUNCHER_LOG_FILENAME
    gate_path = tmp_path / "release-child"
    script_path = tmp_path / "exercise_relay_ownership.py"
    sink_paths = [tmp_path / f"reused-{index}.bin" for index in range(3)]
    script_path.write_text(
        textwrap.dedent(
            """
            import os
            import sys
            from pathlib import Path

            from meridian.lib.launch.process.pty_launcher import PtyProcessLauncher
            from meridian.lib.launch.process.terminal_ownership import tui_terminal_window

            diagnostics_path = Path(sys.argv[1])
            gate_path = Path(sys.argv[2])
            sink_paths = [Path(value) for value in sys.argv[3:]]
            child_code = '''
            import os
            import sys
            import time
            from pathlib import Path

            gate_path = Path(sys.argv[1])
            deadline = time.monotonic() + 5
            while not gate_path.exists():
                if time.monotonic() >= deadline:
                    raise SystemExit(3)
                time.sleep(0.01)
            os.write(1, b"late TUI frame\\\\n")
            '''

            with tui_terminal_window(diagnostics_path) as window:
                saved_fds = window.terminal_fds
                running = PtyProcessLauncher().start(
                    command=(sys.executable, "-c", child_code, str(gate_path)),
                    cwd=Path.cwd(),
                    env=dict(os.environ),
                    output_log_path=None,
                    terminal_fds=saved_fds,
                )

            sinks = [path.open("wb", buffering=0) for path in sink_paths]
            try:
                assert {sink.fileno() for sink in sinks} == {
                    saved_fds.stdin,
                    saved_fds.stdout,
                    saved_fds.stderr,
                }
                gate_path.touch()
                result = running.wait()
                assert result.exit_code == 0
            finally:
                for sink in sinks:
                    sink.close()
            assert all(path.read_bytes() == b"" for path in sink_paths)
            """
        )
    )

    terminal_text = _run_script_in_pty(
        script_path,
        diagnostics_path,
        gate_path,
        *sink_paths,
    )

    assert "late TUI frame" in terminal_text
