# Terminal Resize Corruption Investigation

## Scope
Investigated Meridian-mediated Codex terminal corruption originally reported around terminal resizing, then broadened after additional user evidence: corruption can also begin after leaving a session idle for a few minutes, without an obvious resize event. This was read-only: screenshot inspection, code-path tracing, commit history review, official Codex/OpenAI docs review, and one bounded PTY resize smoke probe.

## Updated trigger understanding
The bug is **not safely explainable as SIGWINCH-only** anymore.

User follow-up materially changes the hypothesis space:
- corruption can happen **without** resizing;
- sometimes it appears after the user walks away / leaves the session idle;
- recent examples were only **minutes old**, so this does **not** look like long-session accumulation alone.

So the real symptom to explain is broader:
> **Codex screen state diverges from what should be on-screen during Meridian-mediated sessions, sometimes around resize, but also sometimes after idle/refresh-style events.**

## Screenshot observations

### Screenshot 1: `messed up codex cli.png`
- Full-screen TUI layout looks desynchronized rather than text-garbled.
- The top title/header row remains visible (`session-bleed-isolation-phase7 -> are you sure this is still running?`).
- Large blank vertical regions appear between preserved UI bands.
- A lower action row (`Run /review on my current changes`) still renders, but the normal intermediate content area appears missing or not repainted.
- This looks like a partial/full-screen repaint failure, not corrupted model output.

### Screenshot 2: `messed up codex cli2.png`
- Same class of failure, but captured at a different UI state.
- The prompt/editor row at top remains visible, as do later status/footer rows (`Worked for 11m 15s`, `Implement {feature}`).
- The center of the screen again contains oversized blank regions and mis-proportioned spacing, as if the TUI buffer/layout recalculated inconsistently.
- Compared with screenshot 1, screenshot 2 strengthens the conclusion that this is a **viewport/repaint/state-divergence problem affecting multiple Codex TUI states**, not a one-off bad render of one widget.

## What Meridian does for Codex primary sessions

Current repo code does **not** launch Codex the same way a user gets from running plain `codex`.

### Direct Codex
- Plain CLI entrypoint is just `codex` (`https://github.com/openai/codex`).
- `codex resume --help` shows a native TUI command with optional `--remote` attach mode.

### Meridian-mediated Codex
Current repo `main` routes Codex primary sessions through a managed attach path:
1. Start `codex app-server --listen ws://127.0.0.1:PORT` (`src/meridian/lib/harness/projections/project_codex_streaming.py:105-153`).
2. Bootstrap/resume the thread over JSON-RPC via `CodexConnection` (`src/meridian/lib/harness/connections/codex_ws.py:261-374`).
3. Launch a second Codex TUI process as `codex resume SESSION_ID --remote ws://127.0.0.1:PORT` (`src/meridian/lib/harness/passthrough/codex.py:44-54,81-93`).
4. Run that TUI under Meridian's PTY wrapper when a real tty is available (`src/meridian/lib/launch/process/runner.py:126-137,169-193`; `src/meridian/lib/launch/process/pty_launcher.py:19-180`).

This routing was made explicit on **April 25, 2026**:
- `48a1085f` (April 23, 2026): introduced managed primary attach.
- `b099d2e6` (April 25, 2026): "Codex primary sessions always use managed app-server path".
- Integration test asserts fresh Codex primary uses managed attach, not black-box launch (`tests/integration/launch/test_launch_process.py:1584-1624`).

## Important mediation boundaries

### What Meridian *does* mediate
- A PTY sits between the user terminal and the attached Codex TUI (`pty_launcher.py`).
- Meridian installs a SIGWINCH handler and forwards winsize changes into the child PTY (`pty_launcher.py:56-73`).
- Meridian also runs a separate Codex app-server/backend process plus a JSON-RPC observer connection (`codex_ws.py`).

### What Meridian *does not* appear to do in this path
- Meridian is **not replaying terminal frames** back into the live TUI.
- Primary attach launches the TUI with `output_log_path=None` (`primary_attach.py:191`; `runner.py:184-193`), so this is not a terminal-output capture-and-replay pipeline.
- The backend event stream is written to `history.jsonl`, but not re-rendered into the foreground terminal by Meridian.

That matters because it lowers the odds of a "Meridian replayed a bad frame" theory and raises the odds of either:
- a Codex remote-TUI repaint/state bug; or
- a PTY/terminal interaction bug while live bytes are being passed through.

## Evidence about resize propagation

### Meridian PTY forwarding exists
Meridian explicitly forwards terminal size changes:
- Initial winsize copy: `pty_launcher.py:146-147`
- SIGWINCH handler installation: `pty_launcher.py:56-73`
- Resize sync on signal: `pty_launcher.py:64-66`

### Bounded smoke probe result
Ran a local tty probe against `meridian.lib.launch.process.pty_launcher._install_winsize_forwarding`.
Result:

```python
{'orig': (24, 80), 'initial_master': (24, 80), 'after_sigwinch_master': (41, 123)}
```

Interpretation: Meridian's PTY layer **does update child PTY winsize on SIGWINCH** in a simple probe. So the primary suspect is **not** "Meridian forgot to propagate terminal size at all."

## Updated root-cause ranking

### 1. Upstream Codex remote-attach / app-server TUI repaint-state bug — strongest evidence
Why:
- Meridian does **not** use plain/local Codex TUI behavior; it forces `codex app-server` + `codex resume --remote ...`.
- OpenAI's current docs say websocket app-server transport is **"experimental / unsupported"** (`codex-rs/app-server/README.md`, lines 261-272).
- OpenAI's February 4, 2026 engineering post says the historical TUI was native and the TUI-to-app-server refactor is a planned direction, i.e. this path is a newer integration surface, not the long-stable direct path (`https://openai.com/index/unlocking-the-codex-harness/`, lines 217-234).
- User explicitly reports the issue is much less common with normal/direct Codex, which matches the path difference.
- The new idle-trigger evidence fits a broader **remote-client repaint divergence** theory: the TUI may redraw incorrectly when processing async remote updates, focus/refresh events, or wake-from-idle terminal repaint conditions, not only on resize.

Assessment:
- Meridian is still implicated because it chooses this path.
- But the likely defect lives in the Codex remote attach / remote TUI codepath or in behavior unique to that path.

### 2. Meridian PTY mediation contributes an edge-case on repaint, idle refresh, or async write timing — plausible but weaker
Why:
- Meridian adds an extra PTY mediation layer around the remotely attached Codex TUI (`pty_launcher.py`).
- Even though winsize propagation works in the simple probe, a screen-state bug can still occur from:
  - alternate-screen transitions,
  - partial redraws after focus/idle refresh,
  - chunked PTY byte delivery during a redraw burst,
  - terminal wake/repaint moments where Codex expects slightly different TTY behavior.
- The new idle trigger makes this candidate a bit stronger than a pure resize-only theory.

Why still weaker than #1:
- There is no evidence Meridian is transforming frames or replaying UI state.
- Basic SIGWINCH/winsize forwarding works.
- The biggest architectural difference remains the use of Codex remote attach over app-server websocket.

### 3. Async app-server / remote event timing issue between Codex backend and Codex TUI — plausible and closely related to #1
Why:
- In Meridian, Codex is split into backend process + remote TUI client.
- Idle corruption could be triggered when the remote TUI receives late status/progress/turn events and repaints from async network-driven state rather than from the older local/native path.
- This is effectively an upstream remote-path synchronization hypothesis rather than a Meridian output replay issue.

### 4. Meridian output/session mediation beyond PTY (frame replay/capture) — unlikely
Why:
- Current code does not show live frame replay into the terminal in primary attach.
- `output_log_path=None` for the live attached TUI lowers the odds that Meridian is double-buffering and re-emitting the visible screen.

### 5. Pure terminal emulator behavior — possible but not primary
Why:
- Resize/focus/wake repaint bugs can be emulator-specific.
- But user says direct Codex in the same general environment usually behaves better, which points away from the emulator as sole cause.

## Broadened minimal reproduction / probe plan

### A. Re-test around *non-resize* triggers
Use the same terminal emulator and workspace for each run.

1. **Direct Codex baseline**
   - Run `codex`
   - Leave it idle for a few minutes
   - Switch focus away/back if that is part of normal usage
   - Note whether corruption appears without resizing

2. **Meridian-mediated Codex**
   - Repeat the same idle/focus/wait pattern
   - Then separately try aggressive resizes

This distinguishes:
- resize-only,
- idle/focus-only,
- and "any repaint pressure" behavior.

### B. Strongest A/B: native/local vs remote Codex without Meridian orchestration
Compare these two Codex modes directly:

1. **Native/local TUI:** `codex`
2. **Remote attach TUI:**
   - terminal A: `codex app-server --listen ws://127.0.0.1:PORT`
   - terminal B: `codex resume <SESSION_ID> --remote ws://127.0.0.1:PORT`

Then test both:
- idle for a few minutes,
- refocus terminal,
- resize window,
- optionally send a short follow-up prompt after idle.

If corruption reproduces in mode 2 but not mode 1, that strongly confirms an upstream Codex remote-attach repaint/state issue independent of Meridian's higher-level orchestration.

### C. Meridian-specific follow-up if needed
If the remote-attach path alone is **not** enough to reproduce:
1. Instrument/debug-run Meridian primary Codex attach.
2. Log SIGWINCH timing and observed winsizes around visible corruption.
3. Log whether corruption correlates with:
   - app-server event bursts,
   - turn completion,
   - idle focus return,
   - or terminal alternate-screen transitions.
4. Try `--no-alt-screen` in the attached Codex TUI path if feasible, to test whether alternate-screen repainting is the main trigger.

## Recommended next step before implementation

**Do not start with a Meridian source fix yet.**

Best next step:
1. Reproduce with a direct Codex remote-path A/B:
   - `codex` vs
   - `codex app-server` + `codex resume --remote`
2. Include **idle/walk-away/focus-return** testing, not just resizing.
3. If remote attach reproduces independently, file/upstream the bug against Codex as a remote-TUI repaint/state-divergence issue.
4. Only if the bug appears **only** when Meridian adds its PTY wrapper should Meridian implement extra diagnostics or a mitigation.

## Bottom line
Meridian is implicated because it currently routes Codex primary sessions through a **managed remote attach architecture** rather than plain direct Codex. The screenshots and the new idle-trigger detail both fit a **screen repaint/state-divergence problem**, not just a resize bug. The strongest evidence still points to the **Codex remote attach / app-server websocket path** as the primary root-cause area, with Meridian's PTY wrapper as a possible secondary contributor rather than the first thing to fix.

## Evidence references
- `src/meridian/lib/launch/process/runner.py:126-137,169-193,258-320`
- `src/meridian/lib/launch/process/pty_launcher.py:19-180`
- `src/meridian/lib/launch/process/primary_attach.py:140-191`
- `src/meridian/lib/harness/passthrough/codex.py:44-54,81-93`
- `src/meridian/lib/harness/projections/project_codex_streaming.py:105-153,156-198`
- `src/meridian/lib/harness/connections/codex_ws.py:245-374`
- `tests/integration/launch/test_launch_process.py:1584-1624`
- OpenAI Codex app-server README: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
- OpenAI engineering post (February 4, 2026): https://openai.com/index/unlocking-the-codex-harness/
