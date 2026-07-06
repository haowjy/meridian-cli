"""External hook execution primitives."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

from meridian.lib.hooks.types import Hook, HookContext, HookResult
from meridian.lib.launch.env_sanitize import (
    ChildEnvPolicy,
    build_meridian_subprocess_env,
)
from meridian.lib.platform.process_scope.fallback import terminate_tree_sync

_TAIL_BYTES = 1024
_TERM_GRACE_SECONDS = 2.0
_HOOKS_ENABLED_ENV = "MERIDIAN_HOOKS_ENABLED"


def hooks_dispatch_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether hook dispatch should run globally."""

    scope = os.environ if env is None else env
    value = scope.get(_HOOKS_ENABLED_ENV)
    if value is None:
        return True
    return value.strip().lower() != "false"


def _tail_text(data: bytes | None) -> str | None:
    if not data:
        return None
    return data[-_TAIL_BYTES:].decode("utf-8", errors="replace")


def _as_bytes(data: bytes | str | None) -> bytes:
    if data is None:
        return b""
    if isinstance(data, bytes):
        return data
    return data.encode("utf-8", errors="replace")


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
        created_at_epoch=0.0,
        grace_secs=grace_secs,
        reason="hook_timeout",
        scope_id="hook_process",
    )


class ExternalHookRunner:
    """Run one external hook command with context transport and timeout handling."""

    def __init__(
        self,
        project_root: Path,
        *,
        env_policy: ChildEnvPolicy | None = None,
    ) -> None:
        self._project_root = project_root.expanduser().resolve()
        self._env_policy = env_policy

    def run(self, hook: Hook, context: HookContext, *, timeout_secs: int) -> HookResult:
        """Execute one hook command and return structured result."""

        if not hooks_dispatch_enabled():
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

        if not hook.command and not hook.command_argv:
            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="failure",
                success=False,
                error="Hook command is required for external execution.",
            )

        env = build_meridian_subprocess_env(
            base_env=os.environ,
            env_overrides=context.to_env(),
            env_policy=self._env_policy,
        )
        env.pop("MERIDIAN_RUNTIME_DIR", None)
        start = time.monotonic()
        process: subprocess.Popen[bytes] | None = None

        try:
            if hook.command_argv:
                process = subprocess.Popen(
                    list(hook.command_argv),
                    shell=False,
                    cwd=self._project_root,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            elif hook.command is not None:
                process = subprocess.Popen(
                    hook.command,
                    shell=True,
                    cwd=self._project_root,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            else:
                raise RuntimeError("hook command resolution failed")
            stdout, stderr = process.communicate(
                input=context.to_json().encode("utf-8"),
                timeout=timeout_secs,
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
        except subprocess.TimeoutExpired as exc:
            stdout = _as_bytes(exc.stdout)
            stderr = _as_bytes(exc.stderr)

            if process is not None:
                _terminate_process_tree(process, grace_secs=_TERM_GRACE_SECONDS)
                try:
                    term_stdout, term_stderr = process.communicate(timeout=1.0)
                except subprocess.TimeoutExpired as kill_exc:
                    stdout += _as_bytes(kill_exc.stdout)
                    stderr += _as_bytes(kill_exc.stderr)
                    process.kill()
                    kill_stdout, kill_stderr = process.communicate()
                    stdout += kill_stdout
                    stderr += kill_stderr
                else:
                    stdout += term_stdout
                    stderr += term_stderr

            return HookResult(
                hook_name=hook.name,
                event=context.event_name,
                outcome="timeout",
                success=False,
                error=f"Timed out after {timeout_secs}s.",
                exit_code=None if process is None else process.returncode,
                duration_ms=int((time.monotonic() - start) * 1000),
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
