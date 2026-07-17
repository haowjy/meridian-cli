## Why

PR #320 bounded the post-connect event stream (BackendLivenessPolicy), but the pre-connect window — subprocess start through handshake — is still unbounded, retry attempts still erase first-attempt diagnostics, and two concrete launch-path defects remain (AF_UNIX socket path length, port-selection TOCTOU). #37's mid-turn Codex orphan case needs the preserved diagnostics to be diagnosable at all. And upstream of all of it, the launcher itself can hang forever *before reservation*: an implicit untimed `sys.stdin.read()` in prompt resolution (#441) wedges `meridian spawn` on any open-but-silent non-TTY stdin — no spawn ID, no artifacts, no trace.

## Goal

Every phase of a spawn launch — from CLI submission through handshake — is bounded and diagnosable: the launcher never blocks on implicit stdin, a startup watchdog covers the pre-connect phase, failed-attempt artifacts survive retries, and known launch-path defects are closed.

## Summary

Scope, per issue:

- **Closes #235** — add a startup-phase watchdog around `start_spawn`/`_start_connection` (the "2h18m wedged before any liveness signal" case); stop truncating first-attempt logs/artifacts on retry (`streaming_runner.py:1107-1112`).
- **Closes #168** — bound `control.sock` path length (hash/shorten under long runtime roots) in `spawn_manager.py`; absorbs closed #174.
- **Closes #201** — close the `_find_free_port` bind-close-reuse TOCTOU in the OpenCode launch path (bind-and-hold or retry-on-EADDRINUSE as the contract).
- **Closes #37** — make Codex runner mid-turn loss diagnosable: persist enough runner/backend evidence that an `orphan_run` reconciliation can say *why* (depends on #235's preserved diagnostics).
- **Closes #419** — cancellation-safe startup ownership: one ownership-transfer guard across adapter startup, dispatch, and manager registration (audit finding [31]; see the audit section below).
- **Closes #441** — never read stdin implicitly: prompt resolution (`cli/spawn.py:180-186`) falls through to an untimed `sys.stdin.read()` on non-TTY stdin even when a reference file (`-f`) makes an empty prompt valid, hanging submission pre-registration under harness/pipeline stdin. Fix by **deleting** the implicit-stdin fallback — stdin is read only via explicit `--prompt-file -`; reference/continue commands with no prompt source resolve to an empty prompt. Ride-alongs: regression test (fd 0 held open and silent → submission must not block; deterministic repro in the issue), help/error copy distinguishing `-f` (reference) from `--prompt-file` (prompt), and launcher-scoped telemetry across prompt resolution → bootstrap → composition → reservation → Popen (pre-registration observability currently ends exactly where these hangs live).

## Resulting Behavior

A spawn that cannot launch fails fast with intact first-attempt evidence instead of wedging silently or retrying over its own diagnostics.

## Changes

#235 is the keystone; #37 consumes it. #168/#201/#441 are independent, small, first-mergeable.
#419 shares #235's pre-connect window — implement the guard alongside the watchdog.
#441 sits upstream of every other member (pre-reservation, in the CLI launcher) — its fix is
**deletion of stdin inference**, not a timeout wrapper, and explicitly NOT a decomposition of
`cli/spawn.py` (that structural note belongs to #424 / the planned rewrite, out of lane scope).

## Audit finding (thermo-nuclear audit #389): cancellation-safe startup ownership

The audit found (finding [31], narrowed by both verification passes) a cancellation-safety
gap at the launch path's ownership-transfer seams:

- Every adapter `start()` guards cleanup with `except Exception:`, which does not catch
  `asyncio.CancelledError` (`connections/codex_ws.py:453`, `claude_ws.py:168`,
  `opencode_http.py:312`, `pi_rpc.py:213`, `cursor_subprocess.py:143`). Claude, Codex,
  OpenCode, and Pi all await after storing the subprocess handle and before `start()`
  returns, so cancellation can bypass their cleanup. **Cursor differs**: no await between
  `create_subprocess_exec()` returning and `start()` returning; asyncio's transport handles
  cancellation inside the exec itself.
- A **universal** window sits above the adapters: in `SpawnManager.start_spawn()`,
  cancellation during `control_server.start()` (`spawn_manager.py:218-229`) bypasses the
  `except Exception:` cleanup while the connection is not yet in `_sessions` — manager
  shutdown can never find it. A runtime probe reproduced exactly this
  (`connection_stopped: False, registered_sessions: 0`).

Scoping (why this is narrower than the finder claimed): managed backends (Codex/OpenCode)
record a `spawn_owned` scope sidecar synchronously after exec (`managed_backend.py:104-119`),
the reaper kills recorded orphan scopes (`reaper.py:594-609`), and Linux applies
`PR_SET_PDEATHSIG` (`platform/detached_process.py:54-78`) — and the primary runner path
never cancels `start()` at all (signals are arbitrated after it returns,
`streaming_runner.py:600-619`). The exposure is external task cancellation (server-mode
disconnect, loop teardown) hitting the stdio children and the manager registration window.

Remedy: one ownership-transfer guard covering every seam — adapter startup, dispatch, and
manager registration through `_sessions` insertion — that catches `BaseException`,
best-effort kills, shields the reap, and re-raises. A single `base.py` helper replacing the
five adapter blocks is insufficient on its own (the manager window remains); conversely,
extending disk scope recording to stdio children closes their residue by construction,
consistent with crash-only recovery. This is the same pre-connect window #235's watchdog
bounds — the guard is what makes the watchdog's cancellation path leak-free.

## Work Item

issue-triage-sweep

## Verification

- Full bar green at every landing: `ruff` clean, `pyright` 0 errors,
  `pytest-llm` 1595 passed / 2 skipped (final).
- Pre-fix-failing regression tests per member: #441 fd-0-open-and-silent
  subprocess test; #168 pathological-root socket-path bound; #201
  deterministic port-claim race; #419 the audit probe
  (cancel during `control_server.start()` → connection must be stopped,
  was `{'connection_stopped': False, 'registered_sessions': 0}`); #235
  startup-timeout + attempt-preservation tests; #37 marker/lifecycle/
  orphan-evidence tests mirroring the p1996 incident shape.
- Runtime probe pass (disposable roots, fake harnesses): #441 repro
  completes in 1.7s (was: indefinite wedge); 222-char runtime root
  spawns + injects fine (socket 64 bytes under `/tmp/meridian-<uid>/`);
  wedged startup fails in ~1.2s with `startup phase timeout` and no
  leftover processes; `attempt-1..3/` evidence intact after retries;
  foreground/background success paths unchanged.
- Adversarial review gate: correctness review (3 blocking + 1 should-fix
  findings, all fixed and re-verified), maintainability review, and a
  focused fix-verification pass.

## Knowledge Updates

Plan doc committed at `docs/plans/spawn-launch-hardening.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
- p5424 (explorer) — pre-connect corridor map
- p5425/p5426/p5427 (coders) — wave 1: #441, #168, #201
- p5432 + p5437 (coders) — wave 2: #235+#419 (+ finisher)
- p5441 (coder) — wave 3: #37
- p5446/p5447/p5448 (reviewers + prober) — G1 gate
- p5452, p5457 (coders) — G1 fixes; p5458/p5459 — fix-verify + thermo re-run
