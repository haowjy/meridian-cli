from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping

from meridian.env_registry import HOOK_PAYLOAD_ENV_KEYS
from meridian.lib.hooks.types import Hook, HookContext, HookResult, hooks_dispatch_enabled
from meridian.lib.launch.cli_shim import pin_launching_build, write_pinned_shim
from meridian.lib.platform import get_home_path
from meridian.lib.platform.process_scope.base import process_birth_epoch
from meridian.lib.platform.process_scope.fallback import terminate_tree_sync

_TAIL_BYTES = 1024
_TERM_GRACE_SECONDS = 2.0
_HOOK_PAYLOAD_ENV_NAMES = frozenset(HOOK_PAYLOAD_ENV_KEYS.values())


def _tail_text(data: bytes | None) -> str | None:
    if not data:
        return None
    return data[-_TAIL_BYTES:].decode("utf-8", errors="replace")


def _terminate_process_tree(process: subprocess.Popen[bytes], *, grace_secs: float) -> None:
    """Terminate a hook subprocess and descendants before draining pipes."""

    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is not None:
        return

    pid = getattr(process, "pid", None)
    if not isinstance(pid, int):
        process.terminate()
        return

    terminate_tree_sync(
        pid=pid,
        created_at_epoch=process_birth_epoch(pid),
        grace_secs=grace_secs,
        reason="hook_timeout",
        scope_id="hook_process",
    )


class ExternalHookRunner:
    """Run one external hook command with context transport and timeout handling."""

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        # The environment hooks run in, and whose MERIDIAN_HOOKS_ENABLED gates them.
        self._env = os.environ if env is None else env

    def run(self, hook: Hook, context: HookContext, *, timeout_secs: int) -> HookResult:

        if not hooks_dispatch_enabled(self._env):
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="skipped",
                success=True,
                skipped=True,
                skip_reason="hooks_disabled",
            )

        if timeout_secs <= 0:
            raise ValueError("timeout_secs must be > 0.")

        if not hook.command:
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="failure",
                success=False,
                error="Hook command is required for external execution.",
            )

        env = {key: value for key, value in self._env.items() if key not in _HOOK_PAYLOAD_ENV_NAMES}
        env.update(context.to_env())
        env.pop("_MERIDIAN_RUNTIME_DIR", None)
        # A hook runs in the launcher, not a harness sandbox, so no write roots apply.
        pin_launching_build(env, write_roots=())
        write_pinned_shim(env)
        start = time.monotonic()

        try:
            process = subprocess.Popen(
                hook.command,
                shell=True,
                # A machine event has no project; its rows run in the home directory.
                cwd=context.project_root or get_home_path(),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout, stderr = process.communicate(
                    input=context.to_json().encode("utf-8"),
                    timeout=timeout_secs,
                )
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process, grace_secs=_TERM_GRACE_SECONDS)
                # communicate() keeps what earlier calls read, so the last one has it all.
                try:
                    stdout, stderr = process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                # No exit code: the tree was killed, and reaping it elsewhere can leave 0.
                return HookResult(
                    hook_name=hook.name,
                    event=context.event_name,
                    outcome="timeout",
                    success=False,
                    error=f"Timed out after {timeout_secs}s.",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    stdout=_tail_text(stdout),
                    stderr=_tail_text(stderr),
                )
            duration_ms = int((time.monotonic() - start) * 1000)
            if process.returncode == 0:
                return HookResult(
                    hook_name=hook.name,
                    event=context.event_name,
                    outcome="success",
                    success=True,
                    exit_code=0,
                    duration_ms=duration_ms,
                    stdout=_tail_text(stdout),
                    stderr=_tail_text(stderr),
                )
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="failure",
                success=False,
                error=f"Exited with code {process.returncode}.",
                exit_code=process.returncode,
                duration_ms=duration_ms,
                stdout=_tail_text(stdout),
                stderr=_tail_text(stderr),
            )
        except OSError as exc:
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="failure",
                success=False,
                error=str(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
