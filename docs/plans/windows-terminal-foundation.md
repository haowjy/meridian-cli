## Why

Windows spawn/coordination runs natively, but the primary interactive path still depends on WSL: the Windows launcher is console-inheritance fallback (not ConPTY), the POSIX PTY path uses blanket raw mode that breaks scrollback, and one launch-crash issue needs on-hardware verification.

## Goal

A real terminal transport story: ConPTY-backed primary launch on Windows, raw-mode handling that preserves terminal usability on POSIX, and the autoclose report settled on hardware.

## Summary

Planning draft. Scope, per issue:

- **Closes #15** — complete the Windows port past console-inheritance fallback: ConPTY transport behind the existing per-platform `ProcessLauncher` seam (the seam itself landed; #43 closed as done).
- **Closes #44** — replace blanket `tty.setraw(stdin_fd)` in `pty_launcher.py` with scroll-preserving handling.
- **Closes #68** — verify the primary-launch autoclose fix on real Windows (code-level cause fixed — `output_log_path` now forwarded — but marked unverified on hardware).

## Resulting Behavior

`meridian` primary launch works in a native Windows terminal without WSL; POSIX terminal scrollback survives a primary session.

## Changes

Needs Windows hardware/VM for verification — CI's windows-gate covers spawn/state layers, not interactive transport. Sequence #68's verification first (cheap), then #44, then #15.

## Work Item

issue-triage-sweep

## Verification

None yet — this is a planning draft (scoping commit only: `docs/plans/windows-terminal-foundation.md`). Implementation on this branch will carry its own tests per `tests/AGENTS.md` and a CHANGELOG entry.

## Knowledge Updates

Plan doc committed at `docs/plans/windows-terminal-foundation.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
