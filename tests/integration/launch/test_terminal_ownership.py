from __future__ import annotations

import errno
import os
import select
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from meridian.lib.platform import IS_WINDOWS, pty


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX PTY contract")
def test_primary_tui_window_captures_launcher_threads_and_children(
    tmp_path: Path,
) -> None:
    diagnostics_path = tmp_path / "stderr.log"
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
                            "os.write(1, b'child stdout\\\\n'); "
                            "os.write(2, b'child stderr\\\\n')"
                        ),
                    ],
                    check=True,
                )
                thread.join()

            exit_code, child_pid, captured_path = run_primary_process_with_capture(
                (
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'TUI frame\\\\n')",
                ),
                Path.cwd(),
                dict(os.environ),
                None,
                on_child_started,
                diagnostics_path,
            )
            assert exit_code == 0
            assert child_pid is not None
            assert captured_path == diagnostics_path.resolve()
            print("Session finished.")
            print("To continue with meridian: meridian --continue c1")
            print(format_launcher_diagnostics_pointer(captured_path))
            """
        )
    )

    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, str(script_path), str(diagnostics_path)],
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

    assert process.returncode == 0
    terminal_text = terminal_output.decode()
    pointer = f"Launcher output during the TUI was written to {diagnostics_path.resolve()}"
    assert "TUI frame" in terminal_text
    assert "thread diagnostic" not in terminal_text
    assert "child stdout" not in terminal_text
    assert "child stderr" not in terminal_text
    assert terminal_text.count(pointer) == 1
    assert terminal_text.index("To continue with meridian") < terminal_text.index(pointer)

    diagnostics = diagnostics_path.read_text()
    assert "thread diagnostic" in diagnostics
    assert "child stdout" in diagnostics
    assert "child stderr" in diagnostics
