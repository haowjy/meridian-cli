from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def test_interval_hook_executes_once_when_two_processes_observe_it_due(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "state"
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    start_file = tmp_path / "start"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    hook_script = tmp_path / "hook.py"
    hook_script.write_text(
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(0.3)\n"
        "(Path(sys.argv[1]) / str(os.getppid())).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "from uuid import uuid4\n"
        "from meridian.lib.hooks.config import HooksConfig\n"
        "from meridian.lib.hooks.dispatch import HookDispatcher\n"
        "from meridian.lib.hooks.registry import HookRegistry\n"
        "from meridian.lib.hooks.types import Hook, HookContext\n"
        "project_root, runtime_root, ready_dir, start_file, hook_script, runs_dir = "
        "map(Path, sys.argv[1:])\n"
        "hook = Hook(name='shared-hook', event='spawn.finalized', source='project', "
        "command=subprocess.list2cmdline([sys.executable, str(hook_script), str(runs_dir)]), "
        "interval='1h')\n"
        "registry = HookRegistry(project_root, hooks_config=HooksConfig(hooks=(hook,)))\n"
        "(ready_dir / str(os.getpid())).write_text('ready', encoding='utf-8')\n"
        "deadline = time.monotonic() + 5\n"
        "while not start_file.exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise TimeoutError('parent did not release workers')\n"
        "    time.sleep(0.01)\n"
        "context = HookContext(event_name='spawn.finalized', event_id=uuid4(), "
        "timestamp='2026-04-20T00:00:00+00:00', project_root=str(project_root), "
        "runtime_root=str(runtime_root), spawn_id='p1', spawn_status='success')\n"
        "HookDispatcher(project_root, runtime_root, registry=registry).fire(context)\n",
        encoding="utf-8",
    )

    worker_args = [
        sys.executable,
        str(worker_script),
        str(project_root),
        str(runtime_root),
        str(ready_dir),
        str(start_file),
        str(hook_script),
        str(runs_dir),
    ]
    workers = [
        subprocess.Popen(worker_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    try:
        deadline = time.monotonic() + 5
        while len(list(ready_dir.iterdir())) < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError("workers did not become ready")
            time.sleep(0.01)
        start_file.write_text("start", encoding="utf-8")

        failures: list[str] = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            if worker.returncode != 0:
                failures.append(f"exit={worker.returncode}\nstdout={stdout}\nstderr={stderr}")
        assert failures == []
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()

    assert len(list(runs_dir.iterdir())) == 1
