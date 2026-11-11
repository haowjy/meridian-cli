# Invariants: how each is kept

The root `AGENTS.md` states each invariant and why users rely on it. This page says how
the code keeps it today, what checks it, and where it falls short. The mechanisms are
current choices; the invariant is what they serve.

## Overlay

**How.** A launch runs the real harness binary, and the session lands in the harness's
own store. Native identity is planned at launch and bound once
(`lib/state/native_binding.py`), never discovered afterwards; transcript reads are
read-only. The places Meridian does write into a native store are relocations
(`lib/harness/pi_relocate.py`, `opencode_relocate.py`) and Claude's cross-project copy
(`claude_sessions.py`, `ensure_claude_session_accessible`). Details:
[lib/harness/AGENTS.md](../src/meridian/lib/harness/AGENTS.md) and the KB's
`state/native-session-binding.md`.

**Checked by** per-harness identity and store tests in `tests/integration/harness/`
(`test_recorded_source_store.py`, `test_codex_owned_identity.py`,
`test_pi_native_identity.py`), and
`tests/smoke/test_overlay_resume.py` (opt-in, paid), which resumes a session without
Meridian.

**Gaps.** No doc lists which files Meridian may write inside a native store.

## Crash-only

**How.** Writes replace files atomically (`lib/platform/atomic.py`, `atomic_replace`).
JSONL appends repair a torn last line first (`lib/state/jsonl.py`). Spawn directories
are staged, then published. Lock files keep their inode. Recovery runs in the
background on a primary launch (`cli/main.py`, `_maybe_schedule_background_repairs`).
Details: [lib/state/AGENTS.md](../src/meridian/lib/state/AGENTS.md) and the KB's
`state/durability-and-locking.md`.

**Checked by** `tests/platform/test_atomic.py`,
`tests/integration/state/test_jsonl_tail_repair.py`, `test_lock_gc.py` and
`test_spawn_store_publication.py`.

`lib/platform/atomic.py` holds the one `atomic_write_text`. It keeps an existing
file's mode and gives a new one the umask default; state that may hold private
content passes `permissions=PRIVATE_FILE_MODE` (`0o600`) at the call.

**Gaps.** Read-only commands never run recovery. Nothing checks that a new
state-file call passes `PRIVATE_FILE_MODE`.

## Compatible upgrades

**How.** Old state is migrated on read: spawn rows (`lib/state/spawn/legacy.py`),
session events (`lib/state/session_fold.py` skips an unknown version with a warning),
work items (a newer version is shown and refused for mutation). The history index is
rebuilt, never migrated. Deprecated config and flags warn for one minor release. The
rules: [docs/releasing.md](../docs/releasing.md#compatibility-and-versioning); the
reasons: the KB's `state/compatibility.md`.

**Checked by** `tests/unit/state/test_spawn_legacy_upgrade.py`,
`tests/integration/state/test_work_store_safety.py`, and the older row shapes in
`tests/support/legacy_rows.py`.

**Gaps.** Nothing checks the session-format or history-index version paths, or any
deprecation warning, and no test reads state written by a real older build.

## Only what was authorized

**How.** A signal goes only to a process whose birth time matches the one recorded at
launch (`lib/state/liveness.py`, `signal_with_birth`). The launch gate holds a child
until its scope is recorded (`lib/platform/launch_gate.py`). A held launch lock can't
be claimed, so cancel signals the runner instead of starting or claiming the work
(`lib/core/spawn_service.py`). A sandboxed caller can't launch a less-confined child
(`lib/core/caller_boundary.py`). Details: `lib/core/.context/cancel.md`, the KB's
`launch/process-scope.md` and `permissions/sandbox-projection.md`.

**Checked by** `tests/platform/test_launch_gate.py`, `test_launch_gate_review.py` and
`test_posix_scope.py` and `test_process_scope_fallback.py`.

Every termination goes through `check_root_identity` (`process_scope/base.py`): a PID
is signalled only when its live birth time matches the recorded one. An unset birth, an
unreadable one or a different one is skipped (`birth_unknown`, `pid_reuse_detected`).
Callers that hold an unreaped child read its birth at the call (`process_birth_epoch`).
Rows that never recorded a birth, such as a legacy `worker_pid`, are therefore never
signalled.

**Gaps.** A POSIX scope whose root has exited still signals its process group, trusting
that live members keep the group ID from being reused. Nothing checks that a sandboxed
caller is refused.

## Private content stays put

**How.** `MERIDIAN_SECRET_*` variables never reach a child
(`env_registry.py`, `lib/launch/env.py`) or a guardrail script
(`lib/safety/guardrails.py`). Secret-looking arguments are redacted before a command is
stored (`redact_primary_command`), and telemetry passes through `RedactionPipeline`
(`lib/extensions/observability.py`). `--from` and `--continue` pass references and
report text, never another session's transcript (`lib/core/context_ref.py`). Turn text
reaches a notification only when `notify.include_messages` is set.

**Checked by** `tests/integration/harness/test_child_env_boundary.py`, the redaction
case in `test_pi_integration.py`, and `tests/unit/idle/`.

**Gaps.** Nothing checks guardrail stripping, `config show` redaction or
`RedactionPipeline`. Idle excerpts sit in plain text in the idle state file, a second
copy of turn text beside the harness's own.
