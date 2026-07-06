# Changelog

Caveman style. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/). Versions `0.0.6` through `0.0.25` in git history only — changelog fell stale, resumed at `[Unreleased]`.

## [Unreleased]

### Security
- Harness and hook child processes now default to sanitized coordinator env: secret-like variables are dropped unless harness-declared, listed in `[spawn].env_passthrough`, or set via `[env]` / `spawn --env`. Opt back into full inheritance with `[spawn].inherit_full_env = true`.
- Child-env sanitization now keeps OS/network/TLS baseline vars (proxy, cert bundles, Windows process env), harness-declared auth alternatives (`ANTHROPIC_AUTH_TOKEN`, provider base URLs), and Pi/Node runtime prefixes while still dropping undeclared `*_KEY` / `*_TOKEN` / `*_SECRET`.
- Pi and OpenCode child launches now declare exact provider credential env vars (e.g. `DEEPSEEK_API_KEY`, `ANTHROPIC_AUTH_TOKEN`) so sanitized spawn env no longer strips load-bearing auth.
- Child-env allowlist no longer uses broad `NPM_CONFIG_`, `NODE_`, or `COREPACK_` prefixes; only exact npm/node/corepack runtime vars pass, with inline credentials scrubbed from `NPM_CONFIG_REGISTRY`.
- `[spawn].inherit_full_env` and `env_passthrough` now apply to external hook subprocesses, restoring legacy full-env behavior when opted in.
- Spawn depth limits read persisted `meridian_depth` from spawn records (O(1)) so pruned ancestry cannot undercount depth via forged `MERIDIAN_DEPTH`.
- External hooks accept argv-array `command = ["/path/to/script", "arg"]` (`shell=False`); string `command` still works but logs a deprecation warning on load.
- Spawn depth limits derive from coordinator spawn ancestry when `MERIDIAN_SPAWN_ID` resolves in the spawn store (`max(env, records)`), closing env-forged depth bypass.

### Changed
- Per-harness coordinator env passthrough declarations moved from `launch/harness_env_passthrough.py` onto `HarnessContract.coordinator_env_passthrough`; launch and connections read grants from the adapter contract.
- Record-backed spawn depth resolution moved from `core/depth.py` to `state/depth_resolution.py`; ops and launch apply `max(env, records)` at their boundaries so core stays free of spawn-store imports.
- Hook docs now describe `command` as `str | array[str]` with POSIX and Windows argv-array examples.

## [0.3.24] - 2026-07-04

### Changed
- Spawn guidance now documents `--skills` flag and deemphasizes `-m` (agent profiles already set the right model).

## [0.3.23] - 2026-07-03

### Changed
- Bump mars-agents to 0.10.2, which rewrites foreign MCP tokens in map-form tool declarations during lift/import.

### Fixed
- Exact continue replay now centralizes launch-contract replay for primary and spawn continuations, uses in-memory `None` for harness-default model replay, rejects agent opt-out as a launch-identity mutation, and suppresses ambient work/task inheritance when the source had none.

## [0.3.23-rc.1] - 2026-07-03

### Added
- `meridian qi claude-md-fix [path] [--dry-run]` replaces the standalone script for creating `CLAUDE.md` mirrors (`@AGENTS.md`) for every `AGENTS.md` under a tree, skipping generated harness directories and reporting conflicts for non-mirror files.

### Fixed
- Creation/bootstrap commands (`meridian init`, `meridian`, `spawn create`, `work start`, `config init`/`set`, `workspace init`) now auto-initialize an uninitialized cwd instead of failing the project-root guard; read and existing-state commands remain strict.
- `--continue` now preserves the recorded work/task directory and cache-shaping launch contract for both primary sessions and spawned subagents. Same-session continue rejects work/task-dir overrides, and launch-policy snapshot replay now treats an empty recorded model as the valid “no managed model override; let the harness default” contract instead of failing with “Launch policy snapshot is missing model.”

## [0.3.22] - 2026-07-02

## [0.3.21] - 2026-07-01

### Added
- Added a guard test asserting the `codex app-server` (streaming) command only ever emits app-server-accepted flags — bare flags valid on `codex exec` but not app-server (like the `--search` regression) now fail CI instead of crashing a live streaming spawn.

## [0.3.20] - 2026-06-25

### Fixed
- Codex agents that grant the `web` capability (e.g. `web-researcher`) now actually get web search: the codex harness enables codex's native Responses `web_search` tool via `-c tools.web_search=true` (works for both the `exec` and `app-server` transports; the `exec`-only `--search` flag crashed the streaming path). Previously the Mars `tools.allowed: [web_search]` grant was inert for codex — the agent loaded but could make no web-search calls.

## [0.3.19] - 2026-06-25

### Removed
- Removed dead `RuntimePaths.chat_policy_path()` (`chats/<id>/policy.json`) — never written or read; launch-policy snapshots persist in spawn state (`spawns/<pid>/state.json`).

## [0.3.18] - 2026-06-25

### Fixed
- `--from` now inherits the **same work** as the source reference. Previously the inherited work was carried only as a non-binding hint and never materialized, so the new session did not join the source's work; both primary launch and `spawn` now materialize it (`ensure_explicit_work_item`) exactly like an explicit `--work`. Explicit `--work` still takes precedence.
- `--from` with a filesystem path now fails fast with an actionable error (it expects a spawn/chat reference) instead of an opaque reference-resolution failure.
- Primary `meridian --continue` is now idempotent: it persists a `LaunchPolicySnapshot` and replays it on resume (parity with `spawn --continue`), so the agent/model/skills/system-prompt no longer silently drift to the current config/catalog default. Snapshots are recovered for all accepted reference forms (primary chat, tracked spawn-session chat, harness session id), passthrough args are rejected on `--continue`, and `extra_args` are replayed from the snapshot. Sessions without a snapshot fall back to the previous behavior.
- OpenCode spawn startup failures now surface an actionable diagnostic (captured stderr + an `XDG_DATA_HOME`/data-dir hint) instead of a bare error, and a spawn that fails before any session exists no longer prints a `meridian session log` transcript command that can never work.

### Changed
- Bump mars-agents to 0.10.1: inbound import fidelity, per-tool MCP tool bridge, and convention-based discovery for skills/agents (PR haowjy/mars-agents#119), plus OpenCode skills no longer emitting per-skill `model-invocable` dropped-field warning noise (haowjy/mars-agents#123). The `build launch-bundle` schema stayed at v3, so meridian-cli's bundle-consumption contract is unchanged; validated end-to-end against discovery/sync/catalog surfaces (real agent resolve: tools/skills/warnings parse cleanly; full test suite + pyright green).

## [0.3.17] - 2026-06-22

### Changed
- Bump mars-agents to 0.9.0: convention-based tool name projection with source-verified harness tool names (exec_command/apply_patch/spawn_agent for Codex, StrReplace/Shell/Task for Cursor, view/browser/fetch for OpenCode, find for Pi).

## [0.3.16] - 2026-06-22

### Fixed
- A successful `meridian spawn` no longer emits two pieces of misleading noise. (1) The git-autosync divergence check pre-resolves `@{upstream}`; a clone with no upstream (detached HEAD / no tracking branch) is now a benign debug skip (`git_autosync_divergence_check_skipped`, `skip_reason="no_upstream"`) instead of a `[warning] git_autosync_divergence_check_failed ... no such branch: 'HEAD...'`. Genuine divergence failures still warn; the happy path is unchanged. (2) The Codex websocket reader treats a normal close (`ConnectionClosedOK`, or `ConnectionClosedError` with close code 1000) as clean shutdown, and the liveness cleanup awaits/retrieves the child task on cancellation — eliminating the GC-time `Task exception was never retrieved` / `ConnectionClosedError` traceback. Abnormal closes still surface.

## [0.3.15] - 2026-06-22

### Fixed
- Internal (tests): the `fake_reaper_liveness` reaper-test helper now also patches the nested read-projection liveness binding (`meridian.lib.ops.spawn.query.is_process_alive`), not just the reaper binding. The nested read path (`MERIDIAN_DEPTH=1`) consulted a real-OS PID liveness check the fake didn't cover, making `test_read_projection_does_not_reap_recorded_scope_but_explicit_reconcile_does` flaky on Windows when the fixture PID happened to be live.

## [0.3.14] - 2026-06-22

### Changed
- Internal: untangled the overloaded `report_output_path` field. The spawn report/`system-prompt.md` paths are no longer threaded through the launch/bind/adapter layers — they are derived once in `bind_launch_context` from the spawn log dir (`spawn_id`). `ResolvedLaunchSpec.report_output_path` is now genuinely codex-only (the subprocess `-o` target, set at bind). No behavior change — report.md still lands at `spawns/<id>/report.md` and codex still emits `-o`.

## [0.3.13] - 2026-06-21

### Added
- Cursor spawns record their harness session id (minted up front via `cursor agent create-chat`), so `meridian session log` works for cursor spawns and cursor sessions are resumable (`--resume`).
- Inactivity watchdog: a cursor spawn with no stream activity for 300s is stopped and finalized from its last assistant message instead of hanging.

### Fixed
- Cursor spawns no longer hang indefinitely when `cursor agent` finishes its work but never emits a terminal `result` event nor exits (previously: silent 80-minute idle hang until manual cancel).
- Cursor report extraction falls back to the last assistant message when no `result` event is present.
- `cursor agent create-chat` is bounded by a 30s timeout and degrades gracefully, so chat-id minting cannot stall spawn startup.

## [0.3.12] - 2026-06-21

### Fixed
- Tool-level commands no longer require a Meridian project — `meridian doctor`, the content linters (`kg check`/`graph`, `qi`/`qi check`/`list`, `mermaid check`), config readers (`config show`/`get`), and `ext list`/`commands` now run from any cwd instead of hard-failing with "No Meridian project found". Project-scoped commands (`spawn`, `work`, `telemetry`, …) still require one.
- `python -m meridian` now routes through the same entrypoint as the `meridian` console script — gains the Windows UTF-8 stdout reconfigure, so glyph output (e.g. `mermaid check`) no longer crashes under cp1252.

## [0.3.11] - 2026-06-21

### Removed
- `meridian chat` feature — entire chat backend, CLI command, frontend, and tests deleted.
- CWD walk-up / marker-based project-root discovery (`.mars/`, `meridian.toml`, `.meridian/id`, `.git`, etc.).

### Added
- Inheritable `MERIDIAN_TASK_DIR` launch surface: per-spawn `scope.json`, inherited task-dir precedence tier, and `meridian task-dir` (query/set/clear).
- `meridian spawn subagents` lists subagents the current agent may spawn (mars-delegated inventory).
- `--from` context refs now include `task_cwd` so receiving sessions know the prior spawn's working directory.
- `-a @explorer` syntax: leading `@` is stripped from agent names for convenience.
- Spawn contract warns agents not to use `Agent()` tool when meridian agents are available.

### Changed
- `meridian spawn agents` renamed to `meridian spawn subagents`; now agent-relative (respects current spawn's declared subagent allow-list) and delegates inventory to mars instead of parsing frontmatter.
- Project root resolution is explicit only: `-C` / `--directory` → `MERIDIAN_PROJECT_DIR` → literal CWD. `ProjectRootSource` narrowed to `explicit | env | cwd`.
- Bumped mars-agents to 0.8.12.

### Fixed
- Named work items no longer leak into the project repo. Active work-scope resolution only honors `[context.work]`; the context-blind `<runtime_root>/work/<slug>` fallback (which wrote `<repo>/.meridian/work/<slug>` under repo-local runtimes) is removed. Without project context the active work dir is now absent rather than fabricated.
- Spawn prompting guidance now points at `meridian mars models prompting <agent-or-model>`.
- Claude primary finalization now repairs TUI trampoline session IDs to the durable transcript ID when prompt history proves the successor.
- Deleted chat command invocations now fail through normal unknown-command routing instead of stale startup metadata.

## [0.3.10] - 2026-06-18

### Added
- `meridian work path <relpath>` materializes paths under the active work scope (ambient or named); `prompts/` and `handoffs/` bucket constants drive switch-warning artifact counts. Now listed in agent-mode `work -h`.
- Agent-mode `meridian spawn -h` now carries the full `meridian-spawn` skill methodology, so the skill can be retired; always-on contract gained the drain invariant; leaf help gained boundaries on `spawn inject`/`spawn files`/`--prompt-var`.
- Agent-mode `meridian spawn -h` is progressively disclosed via `--advanced`: lean default (~85 lines) with core loop, pre-launch decisions, and core commands/flags; `--advanced` reveals full methodology, all commands, and an `Advanced` flag panel. Agent-mode only.

### Fixed
- A same-process `meridian work start`/`work switch` is no longer shadowed by the launch-bound `MERIDIAN_ACTIVE_WORK_DIR`: when `work_id` comes from session attachment, the scope root is recomputed from the work item, so `work path`/artifacts land in the switched scope (child-spawn ambient/named binding preserved).
- `--mode human` (and the TTY downshift) now actually affect emitted output and runtime bootstrap inside a managed session; previously `emit()`/bootstrap keyed on an env-only check that ignored the mode flags and TTY.
- Agent-mode `meridian spawn --bg` prints the readable submit note (spawn id + the `meridian spawn wait` reminder) instead of a raw JSON field dump; `--json`/`--format json` still emit structured output.
- Bind-time context refresh rebinds the `work:` line on composed/recomposed requests too (background handoff, continue/fork), keeping it in parity with the child's `MERIDIAN_ACTIVE_WORK_DIR`; reprojection preserves the native adhoc-agent payload (agent/profile body) for Claude/Codex.
- `resolve_active_work_scope_dir` threads `explicit_chat_id` through `ResolvedContext.from_environment` instead of temporarily mutating `MERIDIAN_CHAT_ID` (reentrant-safe scope resolution for leave-scope warnings).
- Child spawn env resolves ambient `MERIDIAN_ACTIVE_WORK_DIR` from the child's spawn id at the shared `ChildEnvContext` seam (not inherited parent ambient).
- Child spawn bind aligns `MERIDIAN_ACTIVE_WORK_DIR` and injected context `work:` line to the child's ambient `spawns/p<N>/work` dir; dir-without-id propagates across env/JSON surfaces.
- Bind-time context refresh keeps the injected `work:` line and bound `MERIDIAN_ACTIVE_WORK_DIR` in parity across the composed `prompt` and non-empty `user_turn_content` channels; empty user turns stay unset.
- Prompt-file idiom across agent-help surfaces: run `meridian work path prompts/<name>.md`, write there, pass the printed path literally to `--prompt-file` (disposable prompts under `prompts/`, not scope top level).
- Agent-mode root help: portable quick-start example, spawn subcommand hint drops bogus `fork`, `context` uses imperative voice.
- Missing context roots are skipped before sandbox projection (a missing bind-mount source aborted codex's whole bwrap exec namespace while the spawn still reported `succeeded`).
- A spawn launcher killed mid-prep is now recovered: the durable `queued` row is reserved before prep so the reaper sees it; lifecycle side effects are deferred until prep succeeds.
- Agent-mode detection recognizes the primary launched agent (keys on `MERIDIAN_SPAWN_ID` managed-session membership, not `MERIDIAN_DEPTH > 0`).
- Pytest now restores and fails on repo-local Git config mutations, preventing test runs from leaving checkout-scoped identity changes behind.

### Changed
- Test suite aggressively pruned low-value unit/contract/integration coverage: mock-heavy CLI and adapter wiring, help/output presentation contracts, trivial field/default checks, and private-helper tests were removed while retaining signal, process-lifecycle, state-reconciliation, parsing/security, and smoke-like integration coverage.
- Test suite pruned low-value unit coverage for version/help boilerplate and default-property checks while retaining the parsing edge cases and agent-help behavior contracts.
- **Agent/human render mode** is one resolved value: `--mode agent|human` replaces the `--agent`/`--human` mode flags (the `-a`/`--agent` profile flag is unchanged), a single `resolve_render_mode()` (precedence flag > `MERIDIAN_SPAWN_ID` > TTY downshift) replaces three divergent detection paths, and the result is stored once on `GlobalOptions` so output, bootstrap, and help all read it. Dead `ExtensionCommandSpec.agent_default_format` and `help_profile` metadata removed; output-format defaults resolve through catalog `default_output_mode`.
- Help is one mode-aware system: all command-group truth lives in a single `CommandGroupSpec` registry (`cli/command_groups.py`) that feeds root help, curated agent help, app lookup, and command registration; group + leaf help render via `print_curated_group_help` with no Cyclopts `App`-singleton mutation or `main.py` leaf-epilogue mutation. `context` is a real group; `doctor` help flows the same path.
- `WorkScope` is a pure value object (kind/identifier/root + durability + artifact counting); the raw-`Path` overload that fabricated a fake scope is deleted and leave-scope warning/label wording lives in ops, not the state layer.
- The harness spawn-usage contract phrasing is adapter-owned (`RunPromptPolicy.spawn_usage_contract_variants`); `build_guidance_blocks` composes a caller-supplied contract with no `HarnessId` conditional, so a new harness sets its phrasing in its own adapter.
- Active work scope is an explicit `WorkScope` object (kind `work_item`/`ambient_spawn`, identifier, root, durable/ephemeral); `work path`, leave-scope warnings, and artifact counting route through it instead of raw paths + `prompts/`/`handoffs/` string heuristics and `MERIDIAN_CHAT_ID` env impersonation.
- Spawn reservation is a typed seam: `SpawnReservation` is the single lifecycle creation input (`start(reservation)`; `reserve()` is the events-off alias), with field mapping in one write site; replaces the overloaded `announce=False` flag and the duplicate full-signature `reserve_spawn_row`/`announce_started` alias; same happy-path/kill-mid-prep/graceful-failure guarantees.
- Bind-time context-env refresh re-renders the typed `context-env` block by name and re-projects (no header/footer marker surgery; `replace_context_block` deleted), carried as one `ComposedLaunchContent` (the mirror `ContextEnvRefreshPlan` is gone); `ChildEnvContext` is a projection of `ResolvedContext`, not a parallel resolver.
- `ResolvedContext.from_environment` falls back to per-spawn ambient artifact dir (`spawns/p<N>/work`) when no named work is attached; ambient never sets `work_id`.
- Agent-mode `session`/`work`/`context -h`: task-voice long help, de-duplicated session ref-forms (refs in long_help only).
- Lean agent `spawn -h` playbook: no core-loop duplication (contract owns that); teaches prompt-file, context, parallel drain.
- Spawn contract example path is `<prompt>.md`, not `/tmp/<task>.md`.
- Agent group help wraps multi-line command/parameter descriptions with aligned continuation indent.
- Agent-mode root help (`meridian -h`) reworked as a skill entry point: curated commands/options, multi-line table rendering, pointer block to group help.
- The agent inventory (`# Meridian Agents`) is gated on spawn capability: leaf agents (no `subagents`) no longer receive it.
- The spawn contract is a first-class composition block (`spawn_contract_prompt`), applied exactly once on continue/fork instead of appended to the inventory string.
- `LaunchPolicySnapshot` persists the agent profile as one nested `agent_profile` blob, so every field survives continue/fork replay.
- `meridian.spawn.execute` reserves the spawn row up front via `_reserve_then_prepare`; a single `resolve_spawn_work_id` replaces the divergent work_id precedence.
- Agent-mode `--help` is curated: group help shows only agent-relevant subcommands, drops the duplicated enumeration, renders usage examples once. Human help unchanged; hidden subcommands stay invokable.
- Agent root help drops the operator-facing `telemetry` command (still in human help, still invokable).
- Root and group/leaf `--help` render from one shared source (`cli/help_content.py`); root/group blocks no longer leak into nested help via cyclopts epilogue inheritance.

## [0.3.9] - 2026-06-13

### Fixed
- `-a ""` is explicit **no agent** via `agent_opt_out` on launch requests: skips `primary.agent`, profile body, native `--agents` payload, and fork/continue agent inheritance. `RuntimeOverrides` keeps normal `None` = unset semantics.

### Changed
- Bump `mars-agents` to `0.8.7`.

## [0.3.8] - 2026-06-12

### Fixed
- OpenCode `session log` now reads completed transcripts from `opencode.db` before legacy JSON or spawn-history fallback.
- OpenCode DB compactions now render as segment handoffs, not assistant replies.
- Empty OpenCode DB transcripts now fall back to legacy JSON or spawn history.
- Default OpenCode `session log` output now shows completed `task` tool result previews instead of making finished tasks look unfinished.
- OpenCode child task sessions no longer complete, fail, clear signals for, or supply reports for their parent spawn.
- Launch event scoping now falls back cleanly for duck-typed test connections without weakening Codex/OpenCode primary-scope filtering.

## [0.3.7] - 2026-06-12

### Changed
- Fan-out agent config moves from `[settings.meridian.agent_copy].fanout_agents` to peer table `[settings.meridian.fanout].agents`; docs and repo `mars.toml` updated. Legacy `fanout_agents` is deprecated (migration warning, ignored).
- Bump `mars-agents` to `0.8.5`, which parses `[settings.meridian.fanout]` and dual-lists fan-out agents in the launch inventory.

## [0.3.6] - 2026-06-12

### Added
- `max` reasoning effort level (`low` < `medium` < `high` < `xhigh` < `max`), matching Claude Code effort scale.

### Fixed
- `-a ""` (explicit empty agent name) now skips the configured default agent profile instead of falling through to it.
- Claude `xhigh` effort mapping passes through correctly (`"xhigh"` → `"xhigh"`) instead of remapping to `"max"`.

## [0.3.5] - 2026-06-11

### Fixed
- Foreground Codex primary attach no longer kills the app-server when the TUI displaces the observer stream after attach.

## [0.3.4] - 2026-06-11

### Added
- `[env]` config section in `meridian.toml` / `~/.meridian/config.toml`: flat key-value string map for environment variables passed to all spawned harness processes. Matches Claude Code's `settings.json` `"env"` shape.
- `--env KEY=VALUE` repeatable CLI flag on `meridian spawn` for per-spawn environment variable overrides. CLI values override config defaults.
- `config show` displays `env.*` keys; secret-like keys ending in `_TOKEN`, `_KEY`, or `_SECRET` are redacted as `[redacted]`.

### Changed
- `MERIDIAN_*` guard in child environment construction is now case-insensitive, preventing Windows env-var case bypass.

### Fixed
- `spawn --continue` rejects `--env` as a policy-changing override; the original spawn's CLI env is preserved via `LaunchPolicySnapshot` replay.
- Streaming serve and dry-run paths now pass env through `plan_overrides` (previously skipped).
- Empty string env values are now allowed through `--env` and `[env]` TOML values; non-string TOML env values are rejected with a clear error.

## [0.3.3] - 2026-06-10

### Fixed
- Pi projections now pass reasoning effort with `--thinking <level>` instead of appending `:<level>` to model IDs, fixing fully-qualified provider/model launches such as `deepseek`. Passthrough `--model` overrides suppress the managed `--thinking` flag so user model selection wins cleanly.

## [0.3.2] - 2026-06-10

### Added
- Resident "done" model for Codex and OpenCode (`ResidentDrainCoordinator`): a spawn that ends its turn while tracked work (a `--bg` child spawn tree) is still running stays resident instead of finalizing at the first turn-end event, eliminating early-finalize / orphaned-child. When no tracked work is outstanding it finalizes immediately (unchanged latency); otherwise it keeps the backend alive (`set_awaiting_done`) and finalizes when the descendant tree drains or a monotonic deadline backstop expires. On deadline expiry the parent finalizes `timed_out` and reaps its active descendant tree through the canonical cancel pipeline (`SignalCanceller.cancel` → `terminate_tree_sync(runner_pid)`), terminating each descendant's full runner tree (worker + backends) so a child mid backend-launch-retry cannot relaunch and survive as an orphan; each reaped child finalizes `cancelled`. Needs zero model cooperation for correctness. Configurable via `timeouts.resident_deadline_seconds` (default 3300s) and `timeouts.resident_poll_seconds`.
- `meridian spawn done` and `meridian spawn rearm` signals: `done` finalizes a resident spawn `succeeded` immediately (even with tracked work outstanding); `rearm` opts a spawn into explicit residency with a fresh deadline and periodic poll-message injection (~270s). Both are file-based signals consumed by the resident drain loop, requiring no live connection to the spawn.
- `timed_out` spawn status: a terminal failure-class status distinct from `failed`, recorded when a resident spawn's deadline backstop expires (vs an ordinary error). Accepted across the spawn-status state machine, status/stats output, and JSON.
- `ResidentBackendControl.begin_followup_turn(message)` seam (Codex `turn/start`, OpenCode `prompt_async`): starts a fresh turn on an already-resident, idle backend. The primitive for resident resume-on-child-completion and poll-message injection. (Pi has no resident backend; its idle nudge uses the dedicated `SendPiDoneNudge` callback described below.)
- Pi idle done-nudge with spawn-child-vs-process differentiation: when a Pi managed backend goes idle with outstanding work, Meridian nudges it to write `meridian spawn done` only when the remaining work is genuinely non-spawn (managed-bash background jobs / Pi-internal subspawns). If a real Meridian spawn child is still outstanding the parent stays waiting silently — it is resume-woken when the child finishes, so a nudge would nag a parent that is legitimately blocked. Classification fails closed across all three child-existence signals (authoritative active spawn rows, tracked-but-rowless spawn-shaped ids during the child-launch race, and disk-watcher candidate child dirs before `state.json` materializes), so no real child triggers a premature nudge. Pi-only (Codex/OpenCode have no Meridian-tracked non-spawn processes); advisory and routed through the serialized inject seam.

### Changed
- Managed harness backends launch through one flat `launch_managed_backend()` helper (Codex/OpenCode/primary attach), replacing the duplicated per-harness launch blocks. The parent-death/containment outcome is recorded on the process-scope snapshot.
- `BackendLivenessPolicy` owns managed-backend alive decisions (turn-aware silence suppression, process corroboration, stream-stall kill) for Codex and OpenCode connections.
- `SpawnDrainLoop` dispatches Pi vs resident drain explicitly (`use_resident_drain` selected once); Pi keeps `PiDrainCoordinator` with byte-identical behavior, marked `# TODO(phase-4)` to converge into the resident model when the Pi inference machinery is removed.

### Removed
- `backend_lifecycle.json` sidecar and `state/backend_lifecycle.py` — a write-once-never-read second source of truth that drifted vs `process_scopes.json`; the parent-death/containment datum now lives on `ProcessScopeSnapshot`.

### Fixed
- Resident terminal finalization is owned by the coordinator, not raw stream frames: a resident-deadline `timed_out` outcome wins over a late durable-report-completion (no longer mis-finalized `succeeded`), and only coordinator-authored terminal outcomes (resident `timed_out`) suppress launch retries — ordinary single-turn close-without-terminal-frame and Pi drain failures stay retry-eligible. Deadline reap is exception-safe: the parent still finalizes `timed_out` even if descendant teardown raises.
- Orphaned harness backends: detached Codex/OpenCode backends link to the worker lifetime (POSIX parent-death where available, Windows Job Object) and dead-worker reaping finalizes child spawns while cleaning recorded backend scopes.
- `detached_process` parent-death linkage is honest about containment: it reports whether linkage was actually installed (Linux `prctl`; other POSIX degrade to new-session-only and log) instead of silently claiming all non-Windows launches are linked.
- Managed-backend liveness confirms backend death before turn/request silence suppression, so a stale active-turn flag can no longer mask a dead backend.
- POSIX process-scope cleanup no longer sends signals to a recorded process group unless the scope root is the group leader; non-isolated children degrade to PID-tree cleanup instead of risking termination of the parent agent/test-runner session.
- POSIX signal forwarding now falls back to signalling only the child process when a launch inherited the parent process group, avoiding parent-session termination for native-inherit OpenCode/Codex TUI paths.
- Pipe-captured subprocess launches plus Claude/Pi observer backends now start in isolated POSIX sessions so later scope cleanup targets their own process group, not the launching Meridian process group.
- Spawn reads (`list`/status/stats/non-destructive wait, doctor discovery) no longer terminate processes as a side effect; dead-runner recovery and scope termination fire only from the explicit reconciliation path.
- Spawn pre-init failures are typed: expected validation/domain failures return a clean failed output, while unexpected exceptions are logged with a traceback and a distinct error signature instead of being silently bucketed as `pre_init_failed`.
- Pi child-session references resolve the shared Pi session directory via the owner primary (not the child's own `chat_id`) without inheriting the parent transcript; `session export --include-spawns` now includes child spawn reports; `spawn stats --session` filters by the exact session again (was filtering by owner).
- Managed primary attach now fails and tears down the TUI/backend when the observed event stream dies, and OpenCode keepalive chunks no longer reset progress liveness.
- Child spawn session logs no longer bind to the parent chat when the child harness session id is not recorded; child rows now point at their own chat and OpenCode records its fresh session id at connect time.
- OpenCode and Codex streaming now share event-stream liveness detection, so frozen managed backends fail within the liveness window instead of wedging with no events.
- Backend scope cleanup now runs through `SpawnManager` teardown for harnesses with a `ProcessScopeSnapshot` (OpenCode, Codex), removing the duplicate streaming-finalization cleanup path.
- Managed-primary process termination is PID-reuse-safe: launcher/backend/TUI processes are now killed only when their recorded exact birth time matches the live process (within 1s, the same tolerance as the process-scope adapters), instead of the old "created after the spawn started" guard that could signal an unrelated process that reused the PID. Per-PID birth epochs are persisted in primary metadata at launch and validated before every signal; an unverifiable birth fails closed (no kill) and is left to scope cleanup / reaper retry.
- Codex backend can no longer survive `stop()`: a reader task that failed before teardown previously propagated out of cleanup and skipped process termination, leaving an orphaned app-server. Codex teardown now always reaches process/scope termination, and `SpawnManager.stop_spawn` runs an idempotent recorded-scope backstop after every `connection.stop()`, so any transport whose stop throws still gets its orphans reaped.
- Cancelled managed primaries reconcile to the shared cancel terminal state: a dead-launcher managed primary with a cancel intent (or durable completion) no longer mis-reconciles to `failed/orphan_*`; managed-primary reconciliation now honors the same cancel/durable-completion precedence as the generic path for both finalizing and orphan-primary cases.
- OpenCode managed-primary attach gates on backend readiness (`/global/health`) with right-sized startup timeouts and a diagnosable failure cause, and live managed-primary session resolution now matches across chat-id and spawn-id paths.
- OpenCode readiness probes are bounded per-attempt instead of by the whole startup budget. `opencode serve` accepts the TCP connection before its HTTP handler is ready, so the first probe can hang; the old code let one hung GET consume the entire 60s readiness window, so under concurrent launch managed primaries fell back to the black-box TUI and spawns failed after ~240s of retries. Each readiness probe now has a short cap so a hung request is abandoned and retried, and aiohttp owns connection teardown (a stuck connection is not reused). A zero-length cap can no longer disable the timeout. The non-idempotent `POST /session` create is unchanged (it is not replayed on timeout). Verified: 6 concurrent OpenCode spawns now succeed in 5–9s where they previously all failed.

## [0.3.1] - 2026-06-07

### Added
- Primary launches now accept `--task-dir`, matching spawn's explicit task-directory override and rejecting `--continue --task-dir` in favor of `--fork --task-dir`.

### Changed
- Bump `mars-agents` to 0.8.3.

### Fixed
- Meridian CLI processes now default `MERIDIAN_MANAGED=1`, while preserving explicit outer overrides.

## [0.3.0] - 2026-06-06

### Changed
- Require `mars-agents==0.8.1` (was `0.7.15`): provides the v3 launch-bundle schema, harness-aware inventory, and the `settings.meridian.agent_copy` config key this release consumes. The accepted bundle-schema window is **v3 only** — mars 0.8.0's experimental v4 `launch_actions` field was reverted in 0.8.1 and Meridian never consumed it.
- Default `[spawn] deny_headless_harnesses = ["claude"]` (was empty): headless `meridian spawn` of Claude agents is denied out of the box, steering Claude→Claude delegation to the native Agent tool. Override with `deny_headless_harnesses = []`. Anthropic disables headless Claude runs on 2026-06-15.
- Primary startup warns when `claude` is absent from the effective `deny_headless_harnesses` (i.e. the default was overridden), flagging the 2026-06-15 headless-Claude breakage.
- Launch prompt helpers split across prompt builders, prompt context producers, text utilities, and skill resolution; legacy prompt composer APIs removed.
- Agent inventory: mars `prompt_surface.inventory_prompt` only — harness-aware spawn commands, native sections, model metadata. Meridian consumes verbatim; `LaunchPolicySnapshot.bundle_inventory_prompt` persists inventory for resume replay.

### Removed
- Python fallback agent inventory renderer (`build_agent_inventory_prompt` and mirror format helpers in `prompt_context.py`).
- Dead mars 0.7.x launch-bundle compat in the bundle adapter: the `content` skill-body fallback and the unreachable v2 flat-string skills branch (mars is pinned to 0.8.1 / v3 bundles).
- Dead bundle-adapter prompt-surface system_instruction/supplemental_documents parsing and v2 skills_metadata fallback (only inventory_prompt and v3 skills are consumed).

### Fixed
- Codex drain loop no longer treats subagent `turn/completed` as session termination; only the main thread's completion ends the spawn, and report extraction ignores other threads (#317).
- Spawn infers logical `task_cwd` from the caller's working directory when it lies outside the project tree, so cross-repo worktree spawns no longer require explicit `--task-dir` (#318).
- `meridian config set <key> '[]'` now accepts an explicit empty list for `str_list` options (e.g. `spawn.deny_headless_harnesses`), matching the documented "set to `[]`" escape hatch; lists whose items are all empty strings are still rejected.
- Distinct-task-dir launch instruction now states explicitly that the shell cwd is the project root (not the task dir) and tells the agent to `cd` into the absolute `MERIDIAN_TASK_DIR` or use absolute paths, so weaker models no longer run cwd-relative against the wrong directory and silently no-op (#318 follow-up).

## [0.2.32] - 2026-06-05

### Changed
- `deny_headless_harnesses` error now gives context-aware remediation: same-harness callers are directed to their native `Agent()` tool; cross-harness callers are told to use `-m` or `--harness`.

## [0.2.31] - 2026-06-05

### Fixed
- Claude aborted-streaming result during explicit cancel stays `cancelled`, not `succeeded`.
- Background spawn cancel stays `cancelled` when runner writes synthetic `# Spawn failed` report.
- Codex websocket close/error report during explicit cancel stays `cancelled`, not `succeeded`.
- Codex `thread/*`, `turn/*`, `item/*`, `account/*`, `mcpServer/*`, and `remoteControl/*` event envelopes no longer count as durable completion.
- Codex pre-command progress text during explicit cancel no longer counts as durable completion.
- OpenCode `message.*`, `server.*`, and `session.*` event envelopes no longer count as durable completion.

## [0.2.30] - 2026-06-04

### Changed
- Bump `mars-agents` to 0.7.15 — Cursor sandbox/approval classified as Approximate (was Dropped).

## [0.2.29] - 2026-06-04

### Added
- Cursor harness projection: `--sandbox enabled/disabled` flag from mars sandbox mode. `read-only` → enabled, `workspace-write`/`danger-full-access` → disabled.

## [0.2.28] - 2026-06-04

### Added
- Document `[spawn].deny_headless_harnesses` for disabling harness-backed `meridian spawn -a` routes.

## [0.2.27] - 2026-06-04

### Fixed
- Spawn cancel now records durable cancel intent, stops runners before scope cleanup, suppresses retries, and converges stale cancelled runs to `cancelled`.
- Process scope release tracking now uses concrete release identities, so repeated `backend` attempts clean independently.
- Spawn cancel scope fallback now reuses canonical process cleanup, preserving live session-owned scopes during cancel.
- Pi cancel classification now stays `cancelled` when cancellation has explicit intent.
- Generated `# Spawn failed` reports no longer count as durable completion during cancel/reaper convergence.

## [0.2.26] - 2026-06-02

### Changed
- Bump bundled `mars-agents` to 0.7.14 — fixes upgrade-hint Git remote normalization for canonical source identities.

## [0.2.25] - 2026-06-02

### Changed
- Bump bundled `mars-agents` to 0.7.13 — `mars sync` reports when compatible locked package upgrades are available.

## [0.2.24] - 2026-06-02

### Added
- Claude launches deny built-in subagents (`Agent(Explore)`, `Agent(Plan)`, `Agent(General-purpose)`) by default.

### Changed
- Bump bundled `mars-agents` to 0.7.12 — `[settings.agent_copy]` selective native agent emission under `MERIDIAN_MANAGED` / `agent_emission = "never"`.
- `# Meridian Agents` prompt block now frames listed agents as `meridian spawn` targets with system-temp prompt-file handoff.
- Pi `meridian-spawn-watch` correlates `/spawn` and completion notifications through managed-bash `originating_bash_id`, deleting per-child/per-candidate spawn-directory watchers.

### Fixed
- Claude generic `Agent` grant now follows Mars `[settings.agent_copy]`: no Claude agent copy means Meridian keeps `meridian spawn` allowed but blocks Claude-native delegation by default.

## [0.2.23] - 2026-06-01

### Changed
- Codex fresh-session bootstrap turn now says `Meridian started`.

## [0.2.22] - 2026-06-01

### Fixed
- Release builds pin hatchling 1.27.0 and verify wheel/sdist metadata stays PyPI-publisher compatible.

## [0.2.21] - 2026-06-01

## [0.2.20] - 2026-05-31

## [0.2.19] - 2026-05-31

### Added
- Claude adapter denies built-in subagents (`Agent(Explore)`, `Agent(Plan)`, `Agent(general-purpose)`) by default via `--disallowedTools` injection. Opt out with `[claude] allow_builtin_agents = true` in `meridian.toml`.

### Fixed
- Headless harness deny errors now name the denied harness without Claude-specific remediation.
- Claude delegation guidance now appears only for primary launches, not headless spawns.
- Claude delegation guidance probe now ignores optional `.claude/agents` filesystem errors.
- `[spawn].deny_headless_harnesses` now loads from TOML instead of being ignored.
- Codex primary `--from`/`-f`/`-p` now forwards primary user-turn content into the attached TUI; empty launches keep Codex's bootstrap-only idle default.
- Primary `meridian --from REF` now inherits source work item when no `--work` or ambient work is active.
- Root primary launches no longer treat spaced `--prompt-file PATH` values as unknown commands during startup classification.

## [0.2.18] - 2026-05-31

### Changed
- `AGENTS.md`: clarify split between `MERIDIAN_TASK_DIR` and inherited `MERIDIAN_PROJECT_DIR`; use `--task-dir` for source checkout targeting (not `-C`, which retargets the entire project/control root).
- `mars.toml`: use canonical `haowjy/meridian-prompter` dependency URL.

### Fixed
- `meridian -C <dir> mars ...` now scopes mars passthrough to `<dir>` instead of inherited `MERIDIAN_PROJECT_DIR`.
- Release packaging now verifies every Pi extension source entrypoint has a matching built `dist/extensions/<name>/index.js` in both wheel and sdist artifacts, and fails on stale extra extension bundles.
- Pi disk wakeups no longer lose pre-signaled changes before the drain loop subscribes.
- Pi child directories with late `state.json` writes now block quiescence until resolved, then wake the drain loop through bounded polling.
- Pi disk-only child work now re-arms child-wave timeout while parent is idle.
- Pi drain loop now processes ready harness events before timeout completions, avoiding dropped terminal-adjacent events.

## [0.2.17] - 2026-05-30

### Fixed
- Pi micro-drain accepts terminal only after disk quiescence recheck — stale disk state cancels micro-drain with `quiescence_micro_drain_cancelled` instead of completing early.
- Pi drain loop wakes on disk-state changes — `PiDiskWatcher.wait_for_change()` signals drain to re-evaluate quiescence without waiting for harness events.
- Pi spawns hanging permanently at quiescence_micro_drain_started — replaced fragile sleep(0)+task.done() heuristic with bounded 50ms asyncio.wait_for timeout. Also fixed O(N) disk watcher creating indefinite awatch tasks for standalone spawn directories (3000+ watchers in production).
- Pi extension artifacts (managed-bash, meridian-spawn-watch) missing from PyPI wheel — `release.yml` ran `uv build` without building extensions first. Every Pi spawn failed with `Missing Pi extension artifact`. Added Node.js setup, extension build, and wheel contents verification to the release workflow.

### Changed
- PiDiskWatcher no longer creates per-directory inotify watchers for child or candidate spawns. Children are discovered once at startup (O(N) scan) and polled on demand via `force_rescan()`. New children detected reactively when their directory appears under `spawns/`. Eliminates 3000+ `awatch` tasks that created event-loop pressure.

## [0.2.16] - 2026-05-30

## [0.2.15] - 2026-05-29

### Fixed
- Background-repairs thread import deadlock on Python 3.13+ — `meridian-background-repairs` raced with main-thread harness bootstrap to import `connections.base`, triggering `_ModuleLock` deadlock ~60% of the time. Eagerly import `reaper` before spawning the thread.
- Pi `session log` now resolves pi harness session files — `PiAdapter.resolve_session_file` searches `meridian-pi/sessions/` for `*_{session_id}.jsonl` patterns, including spawn-scoped subdirectories. Previously returned "not found" even when the file existed.
- Pi managed-bash foreground hint debounced to 5s minimum interval — prevents self-conversation loop where hint messages trigger continuous model turns without user input. Observed with deepseek-v4-flash producing 47 assistant turns from 1 user message.

## [0.2.14] - 2026-05-29

### Changed
- Bump mars-agents to v0.7.7 — fixes duplicate items in `mars list`/`why`/`doctor`, overlay model override now pivots harness on provider mismatch instead of hard-failing

### Fixed
- Stop appending "Meridian requires mars >= X.Y.Z" to every mars launch-bundle error. Version hint now only appears for genuinely version-related errors (missing binary, schema mismatch, unrecognized command).
- Fix 9 pyright warnings in settings.py, transcript.py, report.py — proper type narrowing for parsed JSON dicts/lists.

## [0.2.13] - 2026-05-28
- bump mars to v0.7.6 to fix some model resolutions bugs

## [0.2.12] - 2026-05-28

## [0.2.11] - 2026-05-28

### Changed
- Stop auto-propagating `MERIDIAN_RUNTIME_DIR` to child processes; `MERIDIAN_PROJECT_DIR` is the single inherited project anchor. Runtime root derives from `.meridian/id` → user home (read paths fall back to repo-local `.meridian/`).
- `-C` / `--directory` ignores explicit `MERIDIAN_RUNTIME_DIR` override; nested Meridian processes ignore it too. Power-user override remains for top-level invocations.
- Pi extension state paths prefer `MERIDIAN_PI_STATE_DIR` (always set at Pi launch from resolved runtime root); TypeScript `pi_state_paths.ts` no longer reads `MERIDIAN_RUNTIME_DIR`.
- Pi `/ps` and `/spawn` selectable task panels now use a full-screen covering overlay, avoiding redraw bleed-through from underlying Pi spinners/status while preserving whole-viewport UX.
- Pi managed bash now keeps separate stdout/stderr log files behind the combined log, so `/ps` stream filters and tool details use the same source-of-truth logs.
- Pi `/ps` bash row inspection now supports `s` to switch combined/stdout/stderr log streams.
- Pi `/ps` and `/spawn` row inspection now opens a centered log overlay with follow, scroll, top/bottom, and refresh controls instead of replacing the whole panel.
- Pi `/ps` and `/spawn` now share a full-screen selectable UI with row preview, colored key hints, `j/k` navigation, `enter` action, `c` clear, and `r` refresh.
- Pi `/ps` now sorts running bash tasks before completed tasks.
- Spawn task-directory resolution now prioritizes explicit `task_dir`, then explicit/ambient work-item `task_dir`, then authority root fallback.
- Launch directory model now keeps subprocess cwd at authority root while task references/instructions anchor to logical task dir.
- Codex and Pi managed connection launches now always bind subprocess/thread cwd to project control root, even when `MERIDIAN_TASK_DIR` differs.
- Env normalization now trims and validates `MERIDIAN_PROJECT_ROOT`/`MERIDIAN_TASK_DIR` alongside existing Meridian context directory vars.
- `meridian work` lifecycle ops are now metadata-only for `task_dir`; done/delete/reopen/rename/update-done no longer mutate task-dir filesystem targets.
- `spawn --continue` now rejects `--task-dir` with fork guidance, while `spawn --fork --task-dir` is allowed.
- `meridian doctor` now reports lingering `worktree-temp/` runtime directories as legacy informational cleanup.
- Spawn launch guidance now teaches split-root behavior (`MERIDIAN_PROJECT_ROOT` + `MERIDIAN_TASK_DIR`) for both primary and child launches; removed injected "cd task dir" instruction text.
- `meridian work`/`meridian work show` text output now surfaces `task_dir` and hides legacy worktree status from default text UI.

### Added
- Global `-C` / `--directory` flag to override project root resolution from CWD. Precedence: `-C` > `MERIDIAN_PROJECT_DIR` > CWD walk-up.
- Pi tracked background bash sends a one-shot follow-up ping after the configured task-ping interval, so stale long-running work can be killed, waited, or detached instead of silently blocking quiescence.
- Pi `/ps:clear` and `/spawn:clear` hide finished task rows from the current Pi session panels.
- Pi harness launch reads `[harness.pi]` from the launch config snapshot (`SpawnParams.pi_harness_profile`) instead of ambient project-root discovery during `resolve_launch_spec()`.
- `timeouts.pi_child_wave_timeout_seconds` and `timeouts.pi_task_ping_interval_seconds` in `meridian.toml` / env (`MERIDIAN_PI_CHILD_WAVE_TIMEOUT_SECONDS`, `MERIDIAN_PI_TASK_PING_INTERVAL_SECONDS`).
- Spawn create payload now carries explicit `task_dir` intent for launch resolution.
- Work item status now persists top-level `task_dir`, with lazy migration from legacy `worktree.path` on read.
- Launch bind env now exports `MERIDIAN_PROJECT_ROOT` and `MERIDIAN_TASK_DIR` to child sessions.
- Added `meridian work task-dir` to print/set/clear active work-item task directory.
- Added `--task-dir` on `meridian work start` and `meridian spawn` create/fork flows.

### Fixed
- Global `-C` / `--directory` now drives `session log`, `config show`, and other auto-wired ops (not only bootstrap); explicit directory sets `MERIDIAN_DIRECTORY_EXPLICIT` so stale `MERIDIAN_RUNTIME_DIR` cannot override the target project.
- External hook subprocess env no longer inherits stale parent `MERIDIAN_RUNTIME_DIR`; hook JSON context remains authoritative.
- `streaming-serve` reuses prepared runtime authority instead of re-resolving via `build_runtime()`.
- Pi launch no longer maps `MERIDIAN_PI_STATE_DIR` to per-spawn session dirs; session scoping stays on `PI_CODING_AGENT_SESSION_DIR` only.
- `meridian session log <pi-spawn>` now renders Pi RPC `message_end` transcript events, including prompt text, assistant text, tool calls/results, and custom follow-up pings.
- Pi full-screen task panels no longer request a render from `invalidate()`, avoiding self-sustaining `/ps` flicker.
- Pi `$` foreground bash no longer writes to Pi's closed foreground output stream after `/ps:b` backgrounds a still-chatty command.
- Pi `$` foreground bash again shows the `/ps` / `/ps:b` hint and supports `/ps:b` / `/ps:background` promotion into managed background tracking.
- OpenCode report extraction no longer returns raw `session.idle` envelopes when assistant text arrives via `message.part.updated` or `opencode.db` session storage.
- Pi auth/prompt failures now persist `report.md` with `# Spawn failed` (`source=pi_failure`) instead of `# Auto-extracted Report`.
- Streaming Pi launches scope `PI_CODING_AGENT_SESSION_DIR` per spawn only for **spawned** RPC sessions; primary sessions keep the configured session root.
- `meridian mars` passthrough now injects `--root` from `MERIDIAN_PROJECT_DIR` when set, so nested mars inside a spawn uses the launcher's project instead of cwd.
- `spawn --task-dir` / `spawn --fork --task-dir` now fail early when the override path is missing or not a directory.
- `work start --task-dir` now validates the path before creating/reopening/mutating work items.

### Removed
- Removed `meridian work worktree`, `work set-worktree`, and `work clear-worktree` command surfaces.
- Removed `--worktree`/`--no-worktree`/`--repo` flags from `meridian spawn` and `meridian work start`.
- Removed managed-worktree orchestration modules and temporary-worktree store from runtime paths.
- Removed obsolete worktree ensure/lifecycle unit suites and replaced lifecycle coverage with task-dir focused tests.
- Pruned trace-through unit tests around `spawn_service` / `spawn_api.build_create_payload` fakes whose only signal was field pass-through; durable split-root and goal-preview contracts now use focused model/service tests, with smoke guides for runtime verification.

## [0.2.10] - 2026-05-27

## [0.2.9] - 2026-05-27

### Added
- Added `meridian spawn status` as a `spawn show` companion with shared flags and report-body-off default.

### Changed
- `spawn show`/`spawn status` text output now defaults to a moderate status tier, with full internal diagnostics behind `--verbose`.
- Auto-extracted spawn reports now persist with `# Report` heading instead of `# Auto-extracted Report`.
- `session log` now defaults to a clean markdown-like transcript view (clean role headers, XML/tool chrome stripped, tool actions collapsed) with raw debug formatting behind `--raw`.
- `session log` now keeps full raw entry/message content in model output and applies truncation only at render time (`--no-truncate` expands cleaned tool output inline).
- Default `session log --tail` windows now emit `Previous:` navigation when earlier entries exist.

## [0.2.8] - 2026-05-27

### Added
- Spawn rows now persist `launch_policy_snapshot`; older `state.json` files without the field still read cleanly.
- Launch preparation now snapshots resolved agent description/body, skill paths, and policy provenance fields into the persisted launch-policy snapshot.
- Launch preparation now snapshots resolved loaded skill payloads into the persisted launch-policy snapshot so replay keeps skill content/name/path stable.

### Changed
- Bump bundled `mars-agents` to 0.7.3 and document static `models list` vs live `models list --live` semantics.
- `spawn --continue` now replays the source launch policy snapshot and rejects policy-changing overrides.
- Spawn launch-policy compilation now reuses persisted snapshots when present instead of re-running live launch-bundle/config/profile/skill resolution.
- Primary Claude launches now project profile-resolved `approval:auto` to `--permission-mode acceptEdits` without requiring a CLI `--approval` override.

## [0.2.7] - 2026-05-25

### Added
- Launch projection artifacts now write `inline-file-reference-bytes.json` with total bytes for inline-routed file refs plus largest file contributors.

### Changed
- Spawn/launch composition now emits a soft warning when 5+ file refs route inline: warns about context drain and recommends folder refs or design/context artifacts.

## [0.2.6] - 2026-05-25

### Added
- `meridian session log` segment-local window navigation: `--from N --limit M`, `--before N --limit M`, `--around N --context M`.
- `meridian session log --global` for explicit global interaction-entry windows with `--from/--before/--around`.
- `meridian session log --segment current|previous|N` with numeric segment indexes absolute from transcript start.
- `meridian session log --full` for explicit full selected-segment output.
- `meridian session log --no-truncate` to opt into full message/entry content rendering.
- `meridian session search` scope flags: `--work WORK_ID`, `--workspace`, `--global`.
- Deterministic session-search `Open:` commands with explicit segment-local anchors (`--segment N --around ... --context ...`).

### Changed
- `meridian session log REF` now defaults to a safe recent window: last 5 transcript entries/chunks from the selected segment, shown oldest→newest.
- `meridian session log` now reserves segment-local entry `0` as a prologue slot for every segment: segment `0` uses extractable system prompt content when available; later segments use extractable compaction handoff content when available; otherwise slot `0` renders a reserved placeholder.
- Segment boundaries now come only from explicit harness/provider compaction markers, so boundary-without-summary still creates a new segment with a reserved entry `0` setup slot.
- Session transcript parsing now consumes explicitly marked post-boundary summary messages into segment entry `0` and no longer duplicates those summaries as normal interaction entries.
- Prologue/handoff extraction is now parser-aware and conservative; broad generic key scraping was removed from segment-setup extraction.
- Segment-local interaction entries now start at `1`; `--full` includes slot `0`, while default/tail views keep their recent interaction windows without consuming one slot on entry `0`.
- `meridian session log --from/--before/--around` now applies segment-local ordinals (`0..N`) by default on the selected segment, including `--from 0 --limit 1` for explicit prologue/handoff reads.
- `meridian session log --global` now uses one unique-index stream across all segment entries (including each segment's reserved entry `0` slot), so `--global --from 0 --limit 1` opens the first global item and later segment handoff slots are directly addressable.
- `meridian session log` window output now emits explicit cross-segment boundary hints (`Previous segment:` / `Next segment:`) when a selected segment window touches segment start/end.
- Session-log output now preview-truncates oversized entry/message content by default and emits deterministic reopen guidance.
- Session-log navigation/counting now runs on interaction entries (tool-result-closed chunks) instead of raw parsed message rows.
- Bare `meridian session log REF --tail` still defaults to 5, now in entry/chunk units.
- Session-search corpus now defaults to current project when REF is omitted.
- Session-search now indexes real segment entry-`0` setup content (prologue/handoff) but excludes reserved placeholder slot text, and emits deterministic setup-open commands (`--segment N --from 0 --limit 1`).
- Work-session association logic is now shared through `ops/work_sessions.py` instead of being duplicated in dashboard/search paths.
- Session transcript loading now flows through one canonical parsed-transcript boundary shared by `session log`, `session search`, and `session export`.
- Workspace search scope now includes roots based on runtime/session evidence, not ad-hoc repo marker checks.

### Removed
- Legacy `meridian session log` compatibility flags `--compaction/-c`, `--last/-n`, and `--offset`.

## [0.2.5] - 2026-05-25

### Changed
- Spawn output now reports distinct task directories as implementation targets without authority-root noise.

## [0.2.4] - 2026-05-24

### Fixed
- Child spawns with distinct task cwd now always receive task-cwd `cd` guidance, and Codex managed-primary launches/bootstrap threads from the task cwd when present.
- Managed worktree ensure now writes missing `mars.local.toml` with `targets = []` so worktree-local Mars sync does not materialize harness targets.
- External task cwd projection now grants the task cwd itself for active projection harnesses.

## [0.2.3] - 2026-05-24

### Added
- `meridian work worktree` command family:
  - `meridian work worktree [<work-id>]` shows worktree status for active/explicit work.
  - `--ensure` creates/recovers managed worktrees.
  - no active work + `--ensure` now creates a tracked temporary managed worktree.
  - `--repo <path-or-alias>` selects target repo (path or `[workspace.<name>]` alias).

### Changed
- Preflight and check scripts now invoke lint/type/test tools through the `dev` extra, so fresh worktrees resolve Pyright as a dev dependency instead of a project script.
- `meridian spawn --worktree` now ensures the selected work item's managed worktree before launch and supports `--repo` target selection.
- `meridian work start --worktree` now routes through shared ensure logic and supports `--repo` target selection.
- Managed worktree metadata now persists `repo_path` and `worktree_name` fields for canonical-path enforcement and repo-scoped recovery.
- Managed worktree paths now always use `<repo>.worktrees/<worktree-name>`; config-driven managed bases removed from lifecycle flows.
- `shared-workspace` guidance in docs now points to `meridian work worktree --ensure` instead of manual `git worktree add`.

### Fixed
- Worktree metadata path separators now normalize at the state boundary, protecting Windows-written managed paths from caller drift.
- Temporary worktree provisioning failures now clear the pending record; pending still means interrupted create.

## [0.2.2] - 2026-05-23

### Fixed
- Claude tool projection now preserves explicit allow rules on allow-default profiles, so scoped Bash grants like `bash(meridian spawn *)` become `--allowedTools` instead of only projecting deny rules.
- Bumped `mars-agents` to `0.7.0`.
- Configuration docs now show Mars-owned routing/agent overlays in `mars.toml`, not legacy Meridian `[agents]` config.
- Model catalog command docs now use `meridian mars models ...`.
- Added shared worktree ensure policy for work items (canonical-path enforcement, manual assignment safeguards, pending recovery + reprovision path) and wired it into `meridian work worktree <work-id> --ensure`.
- Existing managed worktree metadata now wins over cwd/`--repo` inference during recovery/reprovision.
- Temporary managed worktrees now write pending records before `git worktree add` and report/recover interrupted creation.
- Added focused unit coverage for worktree ensure decisions (manual-missing guard, canonical drift guard, pending recovery + reprovision, dry-run no-write behavior, workspace alias target resolution).

## [0.2.1] - 2026-05-23

### Added
- Cursor subprocess harness integration: new `HarnessId.CURSOR`, `CursorAdapter`, `project_cursor_spec_to_cli_args`, and `CURSOR_EXTRACTOR`.
- Cursor model alias in `mars.toml`: `composer` (`cursor/composer-2.5`).
- Cursor harness unit tests for subprocess projection and stream-json extraction.
- `validate_prompt_size` guard in `CursorSubprocessConnection.start()`, matching other harness connections.
- `cursor` harness shortcut added to `HARNESS_SHORTCUT_NAMES` — `meridian cursor spawn ...` now routes to the cursor harness.
- Windows smoke steps for cursor subprocess boundary: missing binary and non-JSON protocol failure, using PowerShell and `.bat` fake binaries.

### Changed
- Cursor harness launch now trusts Mars `routing.harness_model` for effort routing; `project_cursor` passes `--model` through directly and no longer resolves effort slugs in Meridian.
- Launch constants now include `BASE_COMMAND_CURSOR_SUBPROCESS` for `cursor agent --print --output-format stream-json --trust`.
- `ResolvedLaunchSpec` now carries `task_cwd` so harness projections can consume explicit task working directory when needed.
- Projection drift guards now account for `task_cwd` across all harness projection modules.
- `CursorAdapter._CONSUMED_FIELDS`: removed `continue_harness_session_id` and `continue_fork` (moved to `_EXPLICITLY_IGNORED_FIELDS` — these fields are cleared before reaching the adapter when resume/fork are unsupported).

### Removed
- Meridian-side Cursor effort slug threading: removed `candidate_slugs` through launch bundle parsing, model selection context, spawn params, launch specs, adapter ignored-field accounting, and projection delegated-field accounting.

### Fixed
- Removed invalid `MERIDIAN_CURSOR_BINARY` env-var reference from `docs/configuration.md`.
- Added explanatory comment to `mid_turn_injection="queue"` in `CursorSubprocessConnection`: Literal type has no `"none"`; Cursor is single-turn so injection is unused in practice.
- All harness shortcut help text now includes `cursor` alongside `claude`, `codex`, and `opencode`.

## [0.2.0] - 2026-05-22

### Fixed
- Meridian now accepts Mars launch-bundle schema v2 and requires mars-agents >= 0.6.0.

## [0.1.14-rc.5] - 2026-05-22

### Added
- `meridian work set-worktree <work-item> <path>` and `meridian work clear-worktree <work-item>` for explicit worktree-path assignment/clearing on work items.

### Changed
- `release-on-merge.yml` now supports `release:minor` and `release:major` PR labels with `major > minor > patch` precedence.
- `CLAUDE.md` replaced with `@AGENTS.md` reference (no longer a symlink).
- Spawn now separates authority root from task cwd: authority stays project/config/catalog/KB root; task cwd drives file work and relative `-f` anchor.
- Relative `-f` paths now resolve only from task cwd/reference anchor; missing paths now hard-error with given path, anchor, and resolved path.
- `-f @path` support removed; `kb:path` now handles KB references with confinement checks (empty/absolute/escape rejected).
- `--work` is now a hard task-cwd selection boundary, and stale configured worktree paths now hard-error unless `--no-worktree` is set.
- Dry-run and spawn output now include authority/task cwd selection metadata (`authority_root`, `task_cwd`, `task_cwd_source`, `task_cwd_work_item`, `reference_anchor`).
- Spawn execute now consumes prepared authority/task-cwd contract from `SpawnRequest`, so launch cwd and reference-anchor behavior stays identical between dry-run and real launch.
- Task cwd outside authority root is now added to harness workspace projection for active projection harnesses; unsupported harnesses still fall back to authority-root execution + task-cwd instruction.
- Task-cwd fallback instruction now injects only when launch process cwd differs from logical task cwd (Claude/fallback paths), reducing prompt noise on managed task-cwd harnesses.
- Auto task-cwd selection now consults active work items only; archived work items no longer drive ambient/explicit worktree cwd selection.
- Explicit/ambient work lookup now reports authority-root fallback source as `explicit-work-authority-root` / `ambient-work-authority-root` while preserving the consulted work item id.
- Managed-attach fallback to black-box now launches from the selected execution cwd (`launch_child_cwd`) instead of always forcing authority root.
- `meridian work clear-worktree` now clears both `worktree.path` and `worktree.branch`.
- Worktree metadata now separates manual path assignment from Meridian-managed ownership; done/delete/rename cleanup skips manual paths and shared managed paths.
- Reopen/restore notices for missing managed worktrees now instruct users to restore/clear the assignment or use `--no-worktree` instead of claiming silent project-root fallback.

## [0.1.14-rc.4] - 2026-05-21

### Added
- `ResolvedLaunchPolicy.matched_policy_rule` preserves exact mars provenance string for audit and dry-run surfaces.

### Changed
- `uv run pytest-llm` defaults to xdist `-n auto` parallel mode. Suite runs in ~75s instead of ~210s. Force serial with `pytest-llm -p no:xdist` when order matters.
- Meridian's AgentProfile shrunk to identity + content; mars parses profile routing/tools/execution-policy and projects through the launch bundle.
- PRIMARY launch policy now resolves model/harness/execution policy through `mars build launch-bundle` adapter (same routing authority as spawn-prepare), not local compiler resolution.
- PRIMARY launches now preserve bundle-selected harness runnable model IDs (for example `openai/gpt-5.5`) through final harness launch params.
- Missing runnable-path warnings now cover bundle-driven reroutes on PRIMARY routing surfaces.
- Launch-policy unit tests no longer assert CHAT/PRIMARY local compiler routing; stale phase-1 guards were deleted from `tests/unit/launch/test_policies.py`.
- Model listing in `src/meridian/lib/ops/catalog.py` no longer applies Meridian-side visibility heuristics; it now consumes Mars model visibility output directly.
- Primary launch synthetic prompt gating in `src/meridian/lib/launch/plan.py` now uses explicit harness only; no model-pattern harness inference path.
- `AliasEntry.harness` now raises when Mars did not provide harness instead of guessing from model ID patterns.
- Mars now resolves agent overlays; Meridian deleted `AgentOverlayConfig` and now consumes launch-bundle routing/execution-policy directly.
- Launch-context and Exhibit-A integration tests now fake the launch-bundle seam when asserting Meridian-owned behavior (env projection, deny-set composition, telemetry, task-cwd persistence), while bundle subprocess/error contracts remain in `tests/unit/launch/test_bundle_adapter.py` + smoke.
- Smoke docs updated for post-phase-2 routing and launch checks: `tests/smoke/spawn-dry-run.md`, `tests/smoke/workspace.md`, and new `tests/smoke/spawn-continue-fork.md`.
- Legacy `[agents]` migration error-path assertion now checks platform-native config path rendering, so Windows validates copy/paste-safe diagnostics.
- Launch integration clusters now stub `launch-bundle` at the seam (`tests/support/launch.py`) across launch-process and launch-resolution tests, keeping runner/session/fork/managed-path assertions on real Meridian logic while removing live mars harness dependency.

### Removed
- 56 tests under `tests/integration/ops/` that pinned SpawnCreateInput field forwarding, prepared-payload serialization, or qi_ops micro-coverage. Canonical behavior + edge-classification cases retained.
- 66 tests under tests/integration/chat/ and tests/integration/cli/ that pinned argv shape (type-checked by pyright+cyclopts) or were per-mode cross-product duplicates. Smoke guides cover the per-mode behavior.
- AgentProfile.model, .harness, .tools, .mcp_tools, .sandbox, .effort, .approval, .autocompact, .autocompact_pct, .model_policies.
- `catalog/agent.py` `_parse_tools()`, `_parse_model_policies()`, `ModelPolicyRule` class.
- `meridian.toml` `[primary].model`, `[primary].harness` fields. Mars owns model and harness routing; shadow surface gone.
- `MERIDIAN_HARNESS` config env var. Use `-m` / `--harness` CLI flag or mars-side default.
- Obsolete chat CLI policy snapshot integration tests removed (`tests/integration/chat/test_chat_cli_policy.py`) now that launch policy no longer routes through chat/local compiler paths.
- `src/meridian/lib/catalog/model_policy.py` and all imports of its visibility/pattern fallback helpers.
- `AgentOverlayConfig`, `RuntimeOverrides.from_agent_overlay_routing()`, `RuntimeOverrides.from_agent_overlay_policy()`, and `compiler._overlay_policy_rules()`.
- `[agents]` block in `meridian.toml`; move config to `[agents.<name>]` in `mars.toml` / `mars.local.toml` (legacy section now raises migration error).
- 39 pin-the-implementation tests from launch/CLI output and spawn-continue forwarding clusters; coverage moved to lean parser/contract tests plus smoke guides.
- 77 tests under `tests/integration/state/` and `tests/unit/state/` that pinned implementation details or were error-mode cross-product duplicates. Canonical invariant coverage retained.

### Fixed
- Opencode report extraction now reads the assistant message from history.jsonl payload.properties.info.parts instead of looking for a literal "assistant"-type event that the wrapped format never emits.

## [0.1.14-rc.3] - 2026-05-21

## [0.1.14-rc.2] - 2026-05-21

### Changed
- Bump `mars-agents` to `0.4.8rc5` for `build launch-bundle` (with new top-level `agent_body` field) and centralized cache-root routing (`MARS_CACHE_DIR`).
- Spawn-prepare launch policy now routes model/harness/execution-policy through `mars build launch-bundle` adapter (`launch/bundle_adapter.py`) instead of local compiler resolution.
- Spawn-prepare policy tests now fake the bundle adapter boundary; spawn routing tests no longer fake `CatalogSession.resolve_model`.
- `launch/compiler.py` marked deprecated for spawn-prepare path; kept for primary/chat local resolver surfaces in phase 1.

### Fixed
- Spawn now passes mars runnable harness model ID (for example `openai/gpt-5.5`) to OpenCode even when `opencode` is the default-selected harness.
- `test_launch_policy_terminal_surface_mode_defaults_to_pty_mediated` now links all three harness targets (`.claude`, `.codex`, `.opencode`) so mars 0.4.8rc3 routes per-provider; previously the `.claude`-only fixture collapsed every model to claude under the new resolver.
- Spawn-prepare now applies agent-overlay model/harness on the first mars launch-bundle call, so overlay routing can rescue stale profile routes without replay.
- Spawn-prepare execution-policy provenance now follows Meridian post-bundle precedence from real spawn surfaces (CLI/env/overlay/profile), so overlay and env winners report correct sources.
- Spawn-prepare routing now demotes lower-precedence harness overrides when a higher-precedence model is set, preserves same-tier config model+harness forwarding, and rewrites model/harness provenance locally for env/overlay/config winners.
- Mars launch-bundle `config` provenance now maps to Meridian config-default provenance.

### Removed
- Meridian project config `[defaults]` routing keys `model` and `harness`. Use `mars.toml [settings].default_model` / `default_harness`.

## [0.1.14-rc.1] - 2026-05-20

### Added
- `docs/harness-integration.md` — end-to-end guide for adding a new harness to Meridian, using Pi as the worked example. Covers probing, adapter/projection/extraction/semantics, native vs RPC launch split, session parity, model/catalog/Mars integration, verification checklist, and Pi-specific gap tracking.
- Pi Phase 1/2 subprocess harness integration: new `HarnessId.PI`, `meridian pi` harness shortcut, `PiAdapter`, subprocess projection, extractor, and bundle registration.
- Pi harness unit tests for projection, extraction, registry/contract wiring, semantics, and CLI shortcut parsing.
- Pi runtime TypeScript extensions: `managed-bash` (`bash` override + `bash_bg_list/read/wait/kill`) and `meridian-lifecycle` (tracked child + notification/quiescence events) plus prebuild script `npm run build:extensions` for stable extension artifacts under `src/meridian/pi_runtime/dist/extensions/`.

### Changed
- Nested spawn reads now apply read-only stale detection after reconciliation when `MERIDIAN_DEPTH>0`: stale rows surface as synthetic terminal (`runner_exit_*`, `stale_nested_read`, `stale_nested_read_no_pid`) without writing reconciler orphan states to disk.
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; stderr remains debug only; primary TUI no longer shows raw lifecycle JSON; no lifecycle JSON leaks to TUI or stderr.
- Primary Pi session identity now comes from flat `PI_CODING_AGENT_SESSION_DIR/*.jsonl` scanning with first-line JSON parse, cwd/time-window disambiguation, and expected-id suppression; `primary_meta.json` now records `harness_session_discovery` plus `harness_session_discovery_detail` so continue/fork errors can distinguish never-created vs discovery-failed vs legacy empty-id cases.
- Pi harness now launches installed `pi` directly for both primary native TUI and spawned RPC (`MERIDIAN_PI_BINARY` override first, otherwise `pi` on `PATH`); no `meridian-pi` wrapper process in launch projection.
- Pi runtime compatibility checks now run in Python prelaunch/connection startup with fail-fast install/update guidance before process launch.
- Primary Pi metadata sidecar now records resolved runtime selection from Python adapter prelaunch instead of wrapper-emitted runtime metadata.
- `src/meridian/pi_runtime/` now contains only Meridian extension build artifacts/workflows (`npm run build:extensions`), not Pi runtime wrapper packaging.
- Reaper legacy-worker cleanup warning now says it cleaned up a stale spawn and identifies the affected spawn explicitly.
- Pi runtime resolution now prefers `MERIDIAN_PI_BINARY`, then installed `pi` on `PATH`; no bundled/dev runtime fallback path remains.
- Wrapper-era runtime fallback env flags (`MERIDIAN_PI_ALLOW_BUNDLED_FALLBACK`, `MERIDIAN_PI_BUNDLED_AUTH_CONFIRMED`, `MERIDIAN_PI_NODE_BIN`) no longer influence Pi launch policy.
- Pi wrapper no longer forces `PI_CODING_AGENT_DIR` by default; managed session isolation now uses `PI_CODING_AGENT_SESSION_DIR` and `--session-dir` under `~/.meridian/meridian-pi/sessions` (or configured user home).
- Pi streaming launches now scope `PI_CODING_AGENT_SESSION_DIR` to `<session-root>/<spawn-id>/` so extractor fallback ignores stale sibling launch sessions.
- Pi extractor session-id fallback now prefers `PI_CODING_AGENT_SESSION_DIR` and defaults to `get_user_home()/meridian-pi/sessions`.
- Pi harness switched to RPC-only spawned launch (`pi --mode rpc`) with managed extension projection (`--no-extensions` + Meridian-owned `-e` paths), spawned/primary session-role env wiring, and quiescence-aware drain behavior that keeps spawned sessions alive across tracked child work then auto-stops on quiescence.
- Harness semantics now classify Pi terminal/activity/signal events (`agent_end`, `message_update`, tool execution start/update) with stopReason=`error` failure mapping.
- Permission flag projection now explicitly emits no CLI permission flags for Pi (extension-hook permission model).
- Pi runtime compatibility probes require both `--version` and `--help` support for Meridian-required launch surface (`--mode rpc`, `--session-dir`/`PI_CODING_AGENT_SESSION_DIR`, `--no-extensions`, `-e`/`--extension`) before launch.
- Pi runtime resolution now fails fast when the selected override/PATH binary cannot execute or is incompatible, with install/update guidance.
- Pi RPC spawn path now sends prompt immediately after session creation (no preprompt wait) and hard-fails first-event startup on bounded timeout or runtime error.
- Pi runtime selection metadata now persists as `pi_runtime_meta.json` from Python prelaunch for both primary and spawned launches; runtime failures surface as prelaunch/connection resolver errors.
- Chat snapshots now persist effective harness-specific model; chat and primary-launch surfaces now gate with primary-launch capability.
- Pi stop seam now uses typed `StopResult` plus progress events so quiescent-stop escalation is visible in stream/reporting.
- Pi primary launch now routes through native Pi TUI process wrapping (no Pi RPC input loop, no `--mode rpc` for primary), while spawned Pi sessions remain RPC-managed with quiescence cleanup semantics.
- Spawned Pi semantic completion now records success at event-ordered quiescence with micro-drain only (no fixed idle grace) and unblocks `spawn wait` before cleanup.
- Pi cleanup now runs asynchronously after semantic completion; cleanup status/phases (`cleanup_running|cleanup_completed|cleanup_escalated|cleanup_failed`) are emitted separately from terminal spawn status.
- Pending continuation tracking now keeps queued/delivered notifications pending until correlated follow-up terminal completion, with timeout failure `pi_notification_timeout:id=...:phase=...:elapsed=...:timeout=...`.
- Tracked child waves now trigger one aggregate auto-resume with per-child status summary, not one resume per child.
- Pi child-wave timeout now separate from notification timeout; timed-out tracked children are canceled before resume.
- Pi launch/policy/integration tests now assert primary-native Pi command projection and runner routing, while keeping spawned RPC/quiescence assertions intact.
- Primary native Pi now projects lifecycle extension only (`-e meridian-lifecycle`) with explicit passthrough rejection for `--no-extensions`; spawned RPC keeps managed-bash+lifecycle projection and lifecycle role-gates quiescence-ready/cancel-on-deadline to spawned sessions only.
- Primary native Pi now also rejects passthrough `-e/--extension`; lifecycle-only extension policy cannot be overridden from `extra_args`.
- Primary native Pi black-box launches now use resolved runtime child cwd consistently for launch execution, metadata, and session-file discovery matching.
- Primary native Pi now records best-effort lifecycle diagnostics from `stderr.log` into `history.jsonl` when stderr is separately capturable; PTY/merged-stderr launches degrade cleanly without diagnostic ingestion.
- Primary native Pi discovery metadata now labels `--no-session` runs as `never_created`/`ephemeral_session` when no new session id appears, and treats retained continue/resume session ids as authoritative (`ok`) when no new file is discovered.

### Fixed
- Windows: Pi session JSONL fixture paths now use `json.dumps` for proper backslash escaping; cwd case-fold matching is case-insensitive.
- Windows: Node ESM extension loading uses `file://` URLs via `Path.as_uri()` instead of raw Windows paths (`D:` was parsed as URL protocol).
- Windows: tracked Pi child cleanup now has cross-platform fallback (was POSIX-only `os.killpg`).
- Windows: Pi runtime resolver probe errors now correctly classify execution failures vs compatibility failures.
- CI: Pi extension build now works across pnpm 10 (local) and pnpm 11 (CI/corepack) with `pnpm-workspace.yaml` `allowBuilds` + `package.json` `pnpm.onlyBuiltDependencies`.
- CI: committed `pnpm-lock.yaml` for reproducible `--frozen-lockfile` installs; added missing `typebox` dependency.
- CI: Node.js bumped to 24 for `node:sqlite` support required by pnpm 11 via corepack.
- Flaky `test_pi_connection_launches_in_task_cwd_when_provided` — shim now waits for prompt input before exiting instead of racing the connection lifecycle.

### Removed
- `meridian-pi` console entrypoint and wrapper implementation (`src/meridian/cli/pi_entrypoint.py`) plus wrapper-only unit tests.
- Bundled Pi runtime wrapper launch path artifacts (`compile_runner.mjs`, `runner.mjs`, and `scripts/build-meridian-pi-runtime.sh`).
- Slow/implementation-pinning tests: `test_launch_process_pi_primary.py`, `test_managed_bash_extension.py`.

## [0.1.13] - 2026-05-19
### Changed
- Release flow matches Mars shape: main workflow creates RC-by-default release commits/tags; tag-triggered `release.yml` is sole PyPI publish identity.

### Fixed
- Reaper no longer finalizes a spawn as `orphan_run` while its runner process is alive. Per-attempt `process_exit_code`/`exited_at` bookkeeping is attempt-level, not spawn-level — a live runner mid retry-backoff (or running post-attempt guardrails) owns finalization. Reaper now skips the post-exit branch when `runner_pid_alive`.
- Spawn exit schema split: attempt exit fields renamed to `last_attempt_exit_code`/`last_attempt_exited_at`, runner terminal intent persisted as `runner_exit_*`, and reaper now reconciles from runner intent. Dead runner without persisted runner intent now orphan-fails instead of inferring success from stale attempt/report artifacts.

## [0.1.13-rc.2] - 2026-05-17

### Changed
- Routine reaper cleanup logs move to debug; only anomaly diagnostics remain user-visible warnings.

## [0.1.12] - 2026-05-17

### Fixed
- Reaper reconciles active spawns with recorded process exit metadata after a short finalization grace, instead of waiting for the 120s heartbeat window. `spawn wait` no longer hangs on dead post-exit rows until stale-heartbeat cleanup.

## [0.1.11] - 2026-05-17

### Fixed
- Unnamed duplicate builtin hooks now synthesize stable identity names, so multiple `git-autosync` remotes no longer override each other.
- Git subprocess seams strip inherited repo-scoped `GIT_*` environment before invoking git. Worktree operations and `git-autosync` no longer risk retargeting commands into the parent checkout when launched from a git hook or git-managed process.

## [0.1.10] - 2026-05-16

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- OpenCode session logs read transcript messages from `opencode.db` when `session_diff` is empty/non-transcript.

## [0.1.9] - 2026-05-16

### Added
- `meridian spawn --metadata` and `meridian spawn wait --metadata` — inline detailed spawn accounting while preserving report body and transcript pointer.
- Spawn output smoke guide and boundary tests for report-first create/wait behavior.
- `scripts/manually-release.sh` — emergency/backfill release helper. Runs shared preflight, blocks empty `[Unreleased]`, updates version/changelog, commits, tags, and can push.
- `--fork-fresh [REF]` on both `meridian` (primary) and `meridian spawn` surfaces. It forks transcript lineage while allowing identity overrides (`-m`, `-a`, `--skills` on spawn). Bare `--fork-fresh` now defaults to `$MERIDIAN_SPAWN_ID` inside Meridian-managed sessions.
- `meridian --from [REF]` starts a fresh primary session with prior spawn or chat/session context as user-turn reference material. Bare `--from` defaults to `$MERIDIAN_SPAWN_ID` inside Meridian-managed sessions.
- Bare `--from` on `meridian spawn` defaults to `$MERIDIAN_SPAWN_ID` inside Meridian-managed sessions. Same argv normalization as `--fork` / `--fork-fresh`.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Foreground `meridian spawn` and single-spawn `spawn wait` default to report-first compact output: status, report body, transcript command. Agent-mode defaults match compact text; explicit JSON carries report/transcript fields.
- `spawn wait` includes report body by default; use `--no-report` to suppress it.
- `--fork` is now identity-preserving on both primary and spawn CLIs. It rejects identity-shaping overrides with: `--fork preserves launch identity. Use --fork-fresh to change agent, model, or skills.`
- Bare `--fork` now defaults to `$MERIDIAN_SPAWN_ID` inside Meridian-managed sessions. Outside a managed session it fails with: `Cannot infer --fork target: not inside a Meridian-managed session. Pass --fork REF explicitly.`
- CLI argv normalization now rewrites bare/equals forms for `--fork` and `--fork-fresh` before bootstrap and Cyclopts parsing, so forms like `--fork`, `--fork=`, and `--fork --bg` parse consistently.
- Fork conflict validation (mutual exclusion, identity lock, `--from` conflicts) centralized in `argv_normalization.validate_fork_mode()`. Both `spawn.py` and `primary_launch.py` use shared validator instead of duplicated inline checks.
- Bootstrap sentinel awareness replaced with generic `SYNTHETIC_VALUE_TOKENS` set — decouples bootstrap from fork feature encoding.
- `resolve_fork_ref` → `resolve_optional_ref(flag_name=)` with flag-specific inference errors for `--fork`, `--fork-fresh`, and `--from`.
- `--from` + `--continue` now rejected with: `Cannot combine --from with --continue.`

## [0.1.8] - 2026-05-16

### Added
- Candidate-aware harness routing: `--harness opencode --model gpt-5.5` accepted when Mars reports OpenCode as a runnable candidate.
- `RunnablePath` type and `AliasEntry.harness_candidates`/`runnable_paths` fields carry Mars multi-path data through catalog resolution.
- `select_harness_model_id()` picks per-harness model ID (e.g. `openai/gpt-5.5`) from runnable paths at the bind boundary.
- Model-policy `override: {harness: opencode}` validated against candidates and selects harness-specific model ID.
- Empty `override: {}` accepted as intentional no-op in model-policy rules.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Explicit harness selection is a force — model/harness compatibility checks removed; only unavailable primary-launch adapters are rejected.
- Launch policy no longer re-validates final model/harness pairs after harness materialization.
- `ModelSelectionContext.harness_model_id` carries per-harness model string; applied only at harness command boundary, not in telemetry/display/persistence.
- Consolidated Mars candidate/runnable-path parsing: `parse_harness_candidates`/`parse_runnable_paths` replace duplicated inner functions in `models.py`.

## [0.1.7] - 2026-05-15

### Added
- `scripts/manually-release.sh` — emergency/backfill release helper. Runs shared preflight, blocks empty `[Unreleased]`, updates version/changelog, commits, tags, and can push.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Release workflow: PR merges release only with a `release:*` label. CI creates the patch release commit and `vX.Y.Z` tag, then directly runs PyPI publish. Missing labels, `release:skip`, or direct `main` pushes skip auto-release. Tag pushes remain a manual/backfill publish path.
- Workflow docs: document label-gated auto-release and manual tag backfill behavior.
- Agent profile catalog: silently ignore invalid generated profile metadata instead of blocking spawn. Mars owns package validation.
- Agent profile catalog: accept missing `model-policies[].override` for fallback rules and ignore unknown override keys inside otherwise usable rules.
- Repository docs and package examples point at `haowjy/*` repos instead of stale `meridian-flow/*` owners.

## [0.1.6] - 2026-05-15

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Git autosync: merge instead of rebase for remote integration. On conflict: `merge --abort` preserves local state (local-wins), writes conflict metadata JSON, appends notice to AGENTS.md managed section.
- Git autosync: default `conflict_policy` changed from `leave` to `abort`.
- Git autosync: `_FETCH_TIMEOUT_SECS` separated from `_REMOTE_TIMEOUT_SECS` (60s vs 10s).
- Git autosync: divergence detection logs warning on failure instead of silent `(0,0)`, with fallback.

### Added
- `meridian sync conflict list` — terse summary of unresolved autosync conflicts.
- `meridian sync conflict show <id>` — detailed conflict info with merge resolution commands.
- `meridian sync conflict resolve <id>` — mark resolved, strip AGENTS.md notice block, idempotent.
- `meridian context` — sync status section with conflict counts and relative timestamps.
- Git autosync: default ignores for `.git` and `.meridian/autosync/` in `.git/info/exclude`.
- Git autosync: exclude stash/unstash around merge to prevent dirty-tree failures.
- Git autosync: per-conflict metadata at `<sync-root>/.meridian/autosync/conflicts/<id>.json`.
- Git autosync: sync-level state at `<sync-root>/.meridian/autosync/state.json`.
- Git autosync: structured logging per sync cycle with file change stats.
- Git autosync: AGENTS.md conflict notice inside `<!-- autosync-notices -->` managed section.

## [0.1.5] - 2026-05-15

## [0.1.4] - 2026-05-14

### Removed
- Legacy `models:` field on agent profiles — parser, compiler, policy resolver, prompt builder. Use `model-policies:` instead. Profiles with `models:` now fail-closed at parse time.
- `AgentModelEntry` type and all legacy model-override resolution paths in compiler and policies.
- Duplicate `_effective_model_policies()` in policies.py — centralized in compiler.py.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Config overlay `model-policies` accepts empty `override: {}` for fallback-candidate rules (parity with profile parsing).

## [0.1.3] - 2026-05-14
### Fixed
- Release workflow honors the highest stable semver tag, including historical/manual releases, when computing the next version.


## [0.0.50] - 2026-05-14

## [0.0.34] - 2026-05-13

## [0.1.2] - 2026-05-13
### Added
- `model-invocable` frontmatter field on agent profiles. `false` omits the agent from the model-facing `# Meridian Agents` inventory. Missing defaults to visible; non-boolean values warn and stay visible.
- Native-skill harness suppression: harnesses declaring `supports_native_skills=True` no longer duplicate skill prompt documents into supplemental_documents. Claude skill delivery preserved via `--append-system-prompt-file`.
- `meridian spawn --worktree`/`--no-worktree` flag auto-provisions an isolated git worktree per spawn with rollback on failure.
- `.github/workflows/release-on-merge.yml` — merge-to-main auto-release driven by PR labels (`release:patch`, `release:minor`, `release:major`, `release:skip`). CI reads label, bumps version, promotes changelog, commits and tags. Anchored to current main; idempotent across reruns.
- `.github/PULL_REQUEST_TEMPLATE.md` — release label instructions for humans reviewing agent PRs.
- `work switch` reports worktree path, branch, existence, and pending state in output. Recovers interrupted worktree creation on switch.
- `work rename --worktree` flag moves git worktree directory and renames branch atomically with rollback on failure.
- `scripts/prune-worktrees.ps1` — Windows PowerShell mirror of prune-worktrees.sh.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Runtime catalog reads no longer warn for package authoring metadata mistakes; Mars owns profile/skill validation diagnostics.
- `.github/workflows/meridian-ci.yml` extended with non-blocking PR label and changelog validation.
- Release workflow accumulates bump labels from all unreleased merge commits between last tag and HEAD, preventing race conditions when multiple PRs merge before release runs.
- Release workflow requires stable-tag provenance before skipping; ignores untrusted foreign tag collisions.
- `prune-worktrees.sh` reordered: git worktree remove → git branch -d → meridian work done. Work item state only changes after both git operations succeed.
- `AGENTS.md`, `DEVELOPMENT.md`, `docs/releasing.md` rewritten for merge-to-main auto-release workflow.
- `.githooks/pre-push` updated for new release workflow.
- `shared-workspace` skill removed from all agents except `product-lead` — worktrees eliminate the shared workspace problem.

### Removed
- `scripts/release.sh` — manual release script replaced by CI auto-release.

## [0.1.0] - 2026-05-12
### Fixed
- Process reaper escape — spawned processes (Vite, Codex) no longer persist as orphans after session exit. Sync cleanup paths now use containment-aware dispatch (`terminate_scope_sync()`) instead of PID-tree fallback that missed reparented processes. Session-exit triggers cleanup of session-owned scopes. Dead `reclaim_stale_session_scopes()` deleted.

### Added
- Three-word project IDs (`adjective-noun-noun`, e.g. `bright-falcon-harbor`). New projects get word IDs; existing UUID projects keep working. Bundled word lists — no external dependency.
- `meridian migrate` command converts UUID project IDs to three-word format. Blocks when active spawns exist; moves context and runtime dirs atomically.
- Context directories default to user-level storage (`~/.meridian/context/{project}/work`, `~/.meridian/context/{project}/kb`). Project-local `.meridian/` stays lean — identity and config only.
- `git-autosync` hook supports local-only mode (`path` option, no `remote`). Auto-initializes git repo and commits at lifecycle events without network ops.
- `WorkItem` gains `worktree_path` (absolute path string or None) and `worktree_pending` (bool) fields. Persisted in `__status.json`. Legacy files without these fields read as `None`/`False` — no migration needed.
- `update_work_item()` accepts `worktree_path` and `worktree_pending` so the ops layer can record worktree state without re-reading the item.
- `WorkConfig` model (`default_worktree: bool`, `worktree_base: str | None`) surfaced as `MeridianConfig.work`. Populated from `[work]` TOML section.
- `[work]` config section now accepts `default_worktree` (bool) and `worktree_base` (str). Existing `[work.artifacts]` unaffected.
- Durable launch-boundary observability for background spawns. `launch-boundary.jsonl` per spawn records parent-side launch attempt/spawned/failed events and worker-side boot/takeover/failure events across the startup boundary. Reaper reads this artifact to distinguish pre-takeover startup failures from mid-run orphans.
- `launch_boundary_no_takeover` reconciliation error. Background spawns whose `launch-boundary.jsonl` shows launch events but no worker takeover reconcile under this code instead of `orphan_run` or `missing_runner_pid`. Pinpoints the startup-phase failure window.
- Centralized session reference resolution. `resolve_session_reference()` now recovers missing harness session IDs from durable state (session store, spawn row, primary meta) and harness adapter detection. Recovery provenance is explicit: SESSION_STORE, SPAWN_ROW, PRIMARY_META, DETECTED_UNVERIFIED. `session log`, `--continue`, `--fork`, and `--from` now share the same resolution path instead of divergent fallbacks.
- New `reference_recovery.py` module — read-only helper for harness session ID recovery. Keeps transcript-source policy separate from reference resolution.
- `LaunchResult` and `PrimaryLaunchOutput` carry `continue_chat_id` separately from `continue_ref`. Quit message now shows `meridian --continue <chat-id>` instead of UUID, making session resumption human-friendly.
- `ResolvedSessionReference` gains `effective_harness_session_id` (any recorded or recovered ID) and `authoritative_harness_session_id` (excludes DETECTED_UNVERIFIED). Continue/fork paths require authoritative recovery; `session log` may use detected IDs for transcript verification.
- Spawn state v2: per-spawn `state.json` replaces monolithic `spawns.jsonl` event log. Primary launch drops from ~12s to <1s on large histories.
- Project-scoped agent runtime overrides via `[agents.<name>]` in config files — override model, harness, effort, approval, sandbox, autocompact per agent without editing generated profiles.
- Canonical launch-parameter compiler (`src/meridian/lib/launch/compiler.py`) as single authority for launch resolution with typed provenance.
- Three-state model-policy overlay semantics: inherit, suppress, or replace profile model-policies.
- Agent overlay rendering in `meridian config show`.
- Compiler provenance helpers for `meridian spawn --dry-run` output.
- Per-harness token usage extraction. Claude, Codex, and OpenCode each have dedicated extractors that parse their native event shapes (camelCase modelUsage, thread/tokenUsage/updated, session.idle) instead of relying on a generic snake_case scanner.
- Suite-wide git env scrubbing — all tests run with hermetic `GIT_*` isolation. Shared `tests/support/git.py` helper for git subprocess env. Bootstrap service seam contract test.
- Stronger CLI surface contract assertions in smoke tests (`test_config.py`, `test_context.py`).

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- `source = "git"` without `remote` in context config is now valid (local-only tracking) instead of logging a warning and falling back.
- **Execution policy carrier refactor.** `ResolvedExecutionPolicy` promoted to Pydantic `BaseModel` carrier passed opaquely through `SpawnRequest`, `LaunchRequest`, `ChatPolicySnapshot`, `CompilerRequest`, and `CompilerResult`. Adding a new execution-policy field now touches 2 files (carrier type + compiler resolution) instead of ~7 pass-through layers. 27 flat policy fields collapsed to 5 carrier fields; ~150 lines net reduction.
- `ChatPolicySnapshot` version bumped to 2. Old v1 snapshots auto-migrate via `model_validator(mode="before")` — no manual migration needed.
- `SpawnRequest` and `LaunchRequest` accept legacy flat-field JSON via migration validators for backward-compatible deserialization of persisted background spawns.
- `CompilerRequest` profile defaults consolidated from 5 `profile_policy_*` fields to one `profile_policy_defaults: ResolvedExecutionPolicy` carrier.
- `CompilerResult` policy output consolidated from 6 flat fields to one `execution_policy: ResolvedExecutionPolicy` carrier. `compiler_result_to_dry_run_dict()` reads from carrier; dry-run output unchanged.
- Spawned Codex agents auto-reject runtime HITL permission requests via `PermissionBroker(auto_reject_runtime_requests=True)`. Primary sessions unaffected (`auto_reject_runtime_requests=False`).
- Harness adapters declare `requires_initial_prompt` capability flag. Codex adapter sets it `True`; suppresses synthetic prompt for harnesses that provide their own.
- **BREAKING**: Removed all legacy state auto-migration from hot paths. Spawn v1→v2 migration, session counter seeding from legacy events, and legacy spawn output fallbacks deleted. Upgrading from pre-0.1.0 requires `rm -rf ~/.meridian`.
- Spawn store assumes v2 format only — no automatic format detection or conversion.
- Default `spawn wait` yield intervals raised to **50 minutes** (3000s) for all harnesses, up from 15 min for Claude/Codex and 4 min for OpenCode. Long-running spawns no longer lose prompt-cache warmth mid-flight.
- `resolve_policies()` now delegates to the compiler internally through a backward-compatible wrapper.
- Policy-field resolution now uses one generalized N-tier precedence path.
- Spawn help examples now teach `--prompt-file ... --bg` as delegation default. Inline `-p` help points to prompt files for real handoffs.
- `meridian chat` launch now allowed in nested execution (`MERIDIAN_DEPTH > 0`). Agents can delegate headless chat servers (`--headless --port 0`) from within spawns. Root-only restriction lifted for the launch subcommand only.
- `meridian chat` management subcommands (`list`, `show`, `kill`, `open`, `events`) now allowed in nested execution. Agents can read and manage chat sessions without being at depth 0.
- Project `meridian.toml` sets `[defaults] max_depth = 4`, raising the nesting ceiling for workflows that fan-out through multiple spawn tiers.

### Fixed
- Claude primary (interactive) sessions now get `--session-id` seeded, so `meridian session log` can resolve their transcripts. Previously only non-interactive spawns were seeded.
- Work-store write flows now initialize project UUIDs before resolving `{project}` context paths, so create/list/archive/reopen use configured work/archive directories instead of falling back to `.meridian/work` on first use.
- Nested spawns no longer reacquire stale persisted current-work state. `include_persisted_work_fallback` flag gates persisted fallback; `BLOCKED_CHILD_ENV_VARS` blocks stale `MERIDIAN_ACTIVE_WORK_*` from parent env. Chat backend launch explicitly opts in.
- Spawn ID locking race: `ensure_v2_format()` renamed lock file mid-contention, splitting locks across inodes → duplicate spawn IDs. Fixed by keeping stable lock path, then removed migration entirely.
- Pre-push hook leaked `GIT_DIR` env var into preflight subprocess, causing test git operations to target the real worktree.
- structlog `capture_logs()` tests failing in full suite due to `cache_logger_on_first_use` caching module-level loggers.
- Chat normalization now maps current Claude, Codex, and OpenCode live event shapes into canonical chat events. Assistant text, reasoning, tool lifecycle, and turn completion now reach `meridian chat` clients instead of dropping output or leaving chats stuck `active`.
- Profile `approval` and `sandbox` silently dropped during policy resolution. `model_policy_scope()` projection replaces hand-curated field subset — all non-routing fields now flow through the precedence ladder automatically. Agents with `approval: auto` now correctly receive `--permission-mode acceptEdits`.
- Spawn finalization ownership (R4-R6): streaming runner now owns finalization from function entry via sentinel locals + outer try/finally. Eliminates gap where early setup exceptions escaped without terminal events. `launch_prepared_spawn()` extracted as shared helper for both foreground/background paths — structural ownership replaces `pre_launch_complete` boolean flag. Surface-level panic backstops added as last-resort around entire post-row sections.
- TokenUsage expanded with cache_read_input_tokens, cache_creation_input_tokens, reasoning_tokens, cost_is_estimate.
- Catalog-based cost estimation for harnesses that don't expose pricing (Codex). Reads mars models cache pricing (cost_input, cost_output, cost_cache_read, cost_reasoning) and applies per-token formula. Falls back to 10% heuristic when cache pricing is missing.
- CLI spawn show displays cached/reasoning tokens and prefixes estimated costs with `~`.
- Concurrent authoritative finalizers no longer corrupt spawn metrics — losing writers are rejected at the store level (#153).
- Authoritative spawn finalization now correctly replaces reconciler terminals (#152).
- Spawn execution handoff split into prepare/run/cleanup phases. Post-run session teardown errors now log as secondary cleanup failures instead of rewriting runner terminal state (#154).
- OpenCode session continuation creates empty session instead of resuming. Root cause: `POST /session` ignores `sessionID` payload and always creates a new empty session. Fix: verify existing session via `GET /session/{id}` before POST; if found, return it directly. Attach then connects to the existing session with full history.

### Removed
- `_spawn_request_overrides()` from `context.py` — rebuilt `RuntimeOverrides` from flat `SpawnRequest` fields.
- `_compiler_execution_policy_overrides()` from `policies.py` — repacked compiler result fields into overrides.
- `_supported_policy_scope()` from `policies.py` — field-scoping helper replaced by carrier-level scoping.
- `ResolvedLaunchPolicy` compatibility properties (`resolved_routing`, `resolved_execution_policy`, `resolved_overrides`).
- `RuntimeOverrides.from_launch_request()` — bridged flat `LaunchRequest` fields to overrides.
- `src/meridian/lib/state/spawn/migration.py` — v1→v2 auto-migration
- `src/meridian/lib/state/spawn/legacy_events.py` — v1 event models and reducer
- Legacy session counter seeding from `sessions.jsonl` scan
- Dead `read_spawn_events()` fallback in history module

## [0.0.49] - 2026-05-04
### Added
- Descriptor-driven startup pipeline. CommandDescriptor catalog classifies all CLI commands at parse time — startup class, state requirements, telemetry mode, output format. Thin entrypoint handles `--help`/`--version` in ~16ms without importing the full CLI tree (11.6× faster).
- Bootstrap service split. `resolve_*` functions are pure reads; `ensure_*` functions may mutate. Explicit `prepare_for_{project,runtime}_{read,write}` facade.
- Unified telemetry bootstrap API. `TelemetryPlan`/`TelemetryHandle`/`install()` replaces BufferingSink→upgrade pattern.
- Background telemetry retention maintenance with marker-file cooldown and non-blocking flock.
- Prepared context threading. Spawn ops receive `RuntimeReadContext`/`WriteContext` instead of self-bootstrapping.
- CLI output protocol. `to_cli_output()` dispatch replaces isinstance cascade in main.py.
- Declarative help profiles selected at app-build time from catalog metadata.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- `meridian doctor` (no flags) is now cheap and per-project only — no automatic background global scans. Background per-project repairs (stale locks, orphan runs) still run silently on primary launches.
- `meridian doctor --global` is the explicit, opt-in path for expensive cross-project maintenance. Requires root process (rejects in nested execution).
- `meridian chat` (all subcommands) rejects with clear error in nested execution (`MERIDIAN_DEPTH > 0`). Chat is root-only.
- Git autosync hook lock permission errors now gracefully skip the sync cycle instead of crashing. Returns `skip_reason=lock_permission_error`.
- Implicit user config read (`~/.meridian/config.toml`) degrades gracefully when inaccessible — treated as absent with structured warning in nested mode.

### Removed
- Proactive doctor-cache subsystem. No more `doctor-cache.json`, background global scans, or startup nag warnings. Run `meridian doctor` explicitly.
- `BufferingSink` and CLI telemetry upgrade pattern — replaced by unified install API.
- `_resolve_command_path()` and manual command routing in main.py — replaced by catalog classification.

### Fixed
- Sandbox permission gaps: 5 code paths that wrote to unprojected `~/.meridian/` root in sandboxed agents now gate/wrap correctly. No new sandbox projection needed.
- O(N²) spawn scanning in telemetry retention cleanup.
- Startup token scanner: unknown flags no longer greedily consume next positional token.
- Spawn event reducer hardened against missing fields in malformed JSONL.


## [0.0.48] - 2026-05-03
### Added
- `meridian session export` command. Emits stitched full-session markdown transcripts; optional child spawn report appendix.
- Diagnostic guard test for launch warning suppression. Catches catalog/config warnings leaking through spawn prompt assembly.
- Telemetry v1 contract surface: 8-field envelope, event registry, process router, noop/stderr sinks, and startup sink selection.
- Telemetry v1 instrumentation: HTTP/WS lifecycle, command dispatch, dev frontend, MCP invocations, work transitions, debug tracer, stream backpressure, usage events (command invoked, model selected, spawn launched).
- `meridian telemetry tail`, `query`, `status` CLI commands. Domain/correlation filters, truncation-tolerant reader, crash-safe segment discovery.
- `--global` flag on telemetry tail/query/status. Aggregates across all project telemetry dirs plus legacy user-level segments.
- `BufferingSink` for CLI early-event capture. Buffers events before project root resolution, in-place upgrade to project-local sink.
- Per-project telemetry storage under `<project_runtime_root>/telemetry/`. Compound segment naming: `<logical_owner>.<pid>-<seq>.jsonl`.
- Spawn-store-based retention. Read-only reconciled liveness (heartbeat + runner PID + store status) replaces raw PID liveness checks.
- Legacy telemetry migration UX. Old `~/.meridian/telemetry/` segments read-only, visible via `--global`, age out via retention.
- `meridian work root` command — prints the work items container path. Escape hatch for the root that's no longer shown in agent prompts.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Bumped mars-agents 0.2.5→0.2.6. Skill schema: `invocation: explicit|implicit` replaced by `model-invocable` + `user-invocable` booleans. Old fields are hard errors.
- CLAUDE.md: release docs clarified — `meridian mars version` for prompt packages, `scripts/release.sh` for mars-agents and meridian-cli.
- Env vars renamed: `MERIDIAN_WORK_DIR` → `MERIDIAN_ACTIVE_WORK_DIR`, `MERIDIAN_WORK_ID` → `MERIDIAN_ACTIVE_WORK_ID`. Agents confused the context root (`$MERIDIAN_CONTEXT_WORK_DIR`) with the active item dir when both said "WORK_DIR."
- Context prompt injection no longer shows work root path. Shows `$MERIDIAN_ACTIVE_WORK_DIR` when a work item is active, explicit "(no active work item)" when none. Prevents agents from writing to the container.
- Chat backend cold spawn acquisition injects child env context (`MERIDIAN_SPAWN_ID`, `MERIDIAN_PROJECT_DIR`, `MERIDIAN_RUNTIME_DIR`, `MERIDIAN_HARNESS`). Web-launched spawns now get the same environment as CLI spawns.
- CLI telemetry sink uses inherited `MERIDIAN_SPAWN_ID` as logical owner when present. Spawn-invoked CLI commands write to the spawn's segment, not a separate `cli.*` segment.
- mars.toml targets: added `.codex` alongside `.claude` and `.opencode`.
- Primary agent profile renamed `product-manager` → `product-lead`.
- `docs/commands.md`: added telemetry CLI reference (tail, query, status, --global, filtering flags, legacy segments, rootless processes).
- Workspace help text updated for named-root merge model (`meridian.toml` + `meridian.local.toml`).

### Fixed
- Telemetry retention orphan detection checks `owner is None` (truly unrecognizable segment) instead of `not live`. Spawn-owned segments for active spawns no longer falsely deleted.
- Segment owner parsing rejects non-numeric PID/seq components via `isdigit()` guard. Prevents misidentifying filenames with hex or negative values.
- Project root resolution falls back to legacy `.agents/skills/` when `.mars/` absent. Existing projects that haven't migrated to mars still resolve correctly.

### Added
- `meridian bootstrap` primary launch command. Loads typed skill/package bootstrap docs from `.mars`, injects after skill prompts, forwards launch flags, and still runs without docs.
- Skill variant runtime selection. 4-step exact-match specificity ladder: model-token+harness > canonical-id+harness > harness > base. Base frontmatter authoritative; variants replace body only.
- Workspace system redesign. Named `[workspace.<name>]` entries in `meridian.toml` (committed, shared) and `meridian.local.toml` (gitignored, per-machine overrides) replace unnamed `[[context-roots]]` in `workspace.local.toml`. Two-tier merge by name — local overrides committed paths. `meridian workspace migrate` converts legacy config. Legacy fallback with deprecation warnings. Doctor and config-show updated for new format.
- Unified dev frontend (`meridian chat --dev`). Portless auto-detection, `--tailscale`/`--funnel` sharing, `--portless-force` route takeover. `LaunchResult` dataclass bundles session + display metadata. Policy layer resolves tailscale DNS names into `PortlessExposure.allowed_hosts`. HOST/PORT scrubbed from raw Vite child env to prevent accidental network exposure.

### Fixed
- Launch/spawn diagnostics boundary captures `meridian.lib` warnings before stderr. Agent inventory no longer needs quiet catalog scans.
- Skill prompt loading preserves base `SKILL.md` frontmatter. Variant skills now replace only body while keeping base metadata and selected variant path.
- Vite host validation in portless tailscale/funnel mode. Portless HTTPS does not bypass Vite's Host header check — tailscale hostnames were blocked with 403. Policy layer now resolves the tailscale DNS name and passes it through `VITE_DEV_ALLOWED_HOSTS`.
- Portless error classification. All immediate non-zero exits were treated as route-occupied collisions. Now captures stderr via tempfile and matches known collision indicators; generic failures surface actual stderr output.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Mars compiled store migrated from `.agents/` to `.mars/`. `meridian mars sync` passthrough uses managed env. Remaining `.agents` path references cleaned up in resolve.py and test fixtures.
- Mars model identity now separates harness affinity from model ID.
- OpenCode harness drops `opencode-` model prefix routing. Use `--harness opencode` to force harness selection; raw `provider/model` IDs pass through unchanged. Default OpenCode visibility narrowed to `gemini*` only.
- mars.toml targets: removed deprecated `.agents`, added `.opencode` alongside `.claude`.
- Bumped `mars-agents` 0.2.2 → 0.2.3. Agent artifacts suppressed in managed targets under `MERIDIAN_MANAGED=1`; `AgentSurfacePolicy` enum replaces bare bool.
- Makefile: portless-based `dev`/`backend`/`frontend` targets replaced with `chat`/`chat-dev`/`build-frontend`.
- Chat backend structural refactors: `server.py` now transport-only; `ChatRuntime` owns lifecycle, dispatch, close postwork, and recovery. `BackendAcquisitionFactory` + `PipelineLookup` break bootstrap cycle. Normalizers moved from `harness/normalizers/` to `chat/normalization/` (D8 superseded). TUI passthrough extracted to `harness/passthrough/` with `TuiPassthrough` protocol.
- Dead code cleanup: removed `cli/format_helpers.py` shim, dead CLI helpers, unused `ReferenceFile` aliases, `load_reference_files()`, speculative `CreateChatRequest` model/harness fields, stale re-exports, unused function params, unreachable recovery logic.
- Chat backend test suite expanded from 1289 to 1329 tests. New coverage: recovery edge cases (truncated JSONL, corrupt index, idempotency), concurrency races (parallel create, dispatch fencing, close+prompt), WebSocket fanout (reconnection, multi-client, ack framing), HITL flow (approve, answer_input, stale generation), CLI passthrough registry.

### Fixed
- `MERIDIAN_HARNESS` env var no longer overrides child spawn profile harness selection. Child spawns respect their own profile's harness preference.
- Model policy override application and launch policy selection gates fixed for edge cases.
- `spawn wait` yield interval now reads parent harness (`MERIDIAN_HARNESS` env) instead of scanning child spawn rows. Keeps the *caller's* prompt cache alive, not the children's.
- Claude default `wait_yield_seconds` bumped from 270s to 900s — matches Codex, well within Claude Code Max 1-hour cache TTL.
- Workspace projection now includes meridian context paths (work, kb, archive, extras) in harness sandbox permissions. OpenCode/Claude/Codex spawns can access work item artifacts and knowledge base.
- `meridian work` / `meridian work list` no longer crash when a work item exists in both active and archive directories. Warns instead of failing, dashboard stays usable.
- Chat recovery no longer emits duplicate `runtime.error` on repeated restarts of the same abandoned chat. SQLite index now consistent with JSONL after recovery.

### Added
- Model-policy system for agent profiles. Agent profiles now declare `model-policies` with mode parsing, structured fanout display, and per-mode harness preferences. `meridian mars models list` groups by mode. Runtime model-policy matching selects models by harness availability. `ModelSelectionContext` threads through spawn preparation for resolve-once identity. Dry-run surfaces routing provenance. Unrouteable fanout fallback models skipped instead of erroring.
- Agent-aware CLI help. `meridian spawn --agent <name> --help` shows profile-specific help supplements alongside generic spawn help.
- Portless dev workflow. `meridian chat --dev` discovers frontend via `MERIDIAN_DEV_FRONTEND_ROOT` and serves it without requiring a separate port. `PORT` env var support for backend.
- `meridian-web` dev workflow and shared workspace config scaffolding.
- `meridian chat` management commands: `ls`, `show`, `log`, `close`; server discovery file; REST `GET /chat` and `GET /chat/{id}/events`.
- `meridian chat --headless/--no-headless`; non-headless says frontend absent, keeps API-only mode.
- `meridian chat` starts the local headless chat backend with host/port/model/harness options.
- Codex/OpenCode chat normalizers plus cross-harness parity tests for turn/content/file events.
- Chat backend SQLite projection, HITL REST responses, and git checkpoint create/revert.
- Chat backend FastAPI transport: REST command wrappers, bidirectional WebSocket command acks, fan-out, replay, close replay, and restart lost-backend recovery.
- Claude chat backend vertical slice: harness normalizer registry, Claude event normalization, and cold SpawnManager acquisition with observer-before-start.
- Chat substrate: normalized ChatEvent, shared ChatCommand dispatch, JSONL event log, lifecycle service, backend handle, observer bridge, and persistence-first pipeline.

- Harness connection runtime HITL seam. Codex server requests now pass through typed handler policy before JSON-RPC responses; default auto-accept keeps existing spawn behavior.
- `meridian mermaid check` style warnings: ox-edge, bare-end, fill-no-color
- `--strict` flag: treat warnings as errors
- `--no-style` flag: disable style checks
- `--disable` flag: suppress specific warning categories
- Inline suppression via `%% mermaid-check-ignore` comments
- JSON output includes warnings array and counts
- Failed spawn sentinel. Terminal `failed` transition writes `failure.json`; app service can read it back.
- Lifecycle telemetry event model, observer protocol, event names, and per-spawn sequence counter skeleton for future spawn observer hooks.
- `scripts/quality-issues.sh` helper. Lists open quality/immediate GitHub issues, skips `future`, groups by priority: high, medium, low, unprioritized.
- Arbitrary named contexts. Define `[context.<name>]` in `meridian.toml` for custom context roots. `meridian context <name>` resolves and displays. `ContextEntryOutput` model exposes source, path, resolved fields.
- Frontend chat: multi-column spawn view, chat composer with submit/clear, thread activity tracking, session list sidebar, spawn header with streaming controls, ChatContext LRU eviction, conversation effects refactor.

### Removed
- `MERIDIAN_HARNESS_COMMAND` env var. Harness adapters are the only launch path.
- `backlog/` directory deleted — tracking moved to GitHub Issues.
- Windows CI matrix. Ubuntu-only until Windows support is re-validated.
- Archived old frontend, FastAPI app server, HCP chat stack, `meridian app` command, app-backed tests, and built UI artifacts. Active codebase clear for fresh `meridian chat` / `meridian app` rebuild.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- `meridian spawn wait` yield default now harness-aware: unknown 240s, Claude 270s, Codex 900s; mixed waits use shortest. `--yield-after-secs` still overrides.
- Harness event semantics now live in narrow pure helpers for terminal outcome, activity transitions, and signal clearing.
- App-server-backed extension invocation now reports no app server while local extension discovery/dispatch stays active.
- `POST /api/spawns` resolve-before-persist. No spawn row created on composition failure. Row metadata (model, agent, harness) reflects resolved values — no "unknown" placeholders.
- `POST /api/spawns/{id}/archive` routed through `SpawnApplicationService`. Terminal-only gate: 409 if spawn not yet terminal. Idempotent: returns `{noop: true}` if already archived.
- Spawn cancel now uses one application service for CLI and HTTP; managed primary cancel behavior shared across both surfaces.
- Codex startup telemetry now emits canonical typed phases via lifecycle observers, not string callback messages.
- `scripts/release.sh` now keeps pytest output visible during pre-release checks, so long full-suite runs no longer look hung.
- Pyright warning cleanup across CLI, state, app, and launch code. Type-check baseline now clean: `0 errors, 0 warnings`.
- Launch policy model resolve now one-pass carry-through. Reuse one resolved alias entry for harness pick, final model, same-layer compatibility check, and model defaults.
- Launch effort/autocompact precedence now one named ladder helper: explicit user -> profile `models:` -> profile defaults -> alias defaults -> none. `launch.resolve` compatibility shim for `resolve_policies` removed; unmatched profile `models:` fallback now debug-only log.
- Prompt package deps unpinned in `mars.toml`; `meridian-dev-workflow` lock now v0.1.8.
- SpawnManager now supports post-persist event observers. Slow/failing observers isolated from drain loop and subscriber fan-out; legacy `on_event` stays as shim.

### Fixed
- Chat final-gate leaks/races: `meridian chat --harness` now honors global parse, checkpoints serialize git mutations per server, and failed chat heartbeat startup stops spawned backend.
- Chat backend final-gate blockers: per-turn generation fencing, checkpoint multi-chat guard, failed acquisition observer rollback, and harness selection regression coverage.
- Codex confirm-mode approval requests rejected again in websocket adapter. Slow observer shutdown now times out, so spawn teardown no hang forever.
- `kg check` skips `[!FLAG]` blocks and git conflict markers inside fenced code blocks.
- Mermaid style checks skip YAML frontmatter and directive bodies; `fill-no-color` no longer treats `stroke-color:` as text color.
- `meridian doctor` active-spawn warning now post-reconcile. Warning only lists genuinely live sessions, not stale rows just repaired. Same-run `--prune` can now clean artifacts that became eligible after reconciliation. Cached summary no longer suggests `--prune --global` when only live sessions remain.
- Failure sentinels now write after terminal state persists. Stale `failure.json` ignored unless spawn still `failed`.
- Startup telemetry now carries harness/model/agent context and Codex emits phases outside observer mode too.
- Context query error message now lists all available context names including extra contexts.
- Spawn model aliases keep alias defaults through prepare. `spawn -m gpt55` now matches primary `--model gpt55` effort.

## [0.0.45] - 2026-04-25

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- `mars-agents` 0.1.18 -> 0.1.19. Mars model listing now uses harness-aware runnable visibility and OpenCode provider/model availability.
- Background spawn note trimmed. `meridian spawn --bg` now returns a short "Backgrounded. Spawn id: ... Collect later with \`meridian spawn wait\`." hint instead of a long immediate-wait warning.
- `meridian models list` now fails fast. Use `meridian mars models list`.
- Codex managed primary stabilized. `meridian codex` fresh, resume, and fork sessions all use managed `app-server` path — no silent black-box fallback. Fresh sessions gate TUI attach on rollout materialization (`session_meta` present, not full turn completion). Startup telemetry phases shown on stderr: `Starting Codex app-server...` → `Connecting managed observer...` → `Creating fresh Codex thread...` → `Materializing rollout...` → `Attaching Codex TUI...`. Managed startup failure is loud — not silent. See [codex-tui-passthrough.md](docs/codex-tui-passthrough.md).
- App server TCP launch now auto-increments default port when `7676` busy.

### Added
- `state.retention_days` config key. TTL for stale state pruning: `-1` never prune, `0` prune immediately, positive = days. Default 30. Env var `MERIDIAN_STATE_RETENTION_DAYS`.
- `meridian doctor --prune` deletes stale spawn artifacts for the current project. `--prune --global` also prunes orphan project dirs machine-wide under `~/.meridian/projects/`.
- Background doctor cache. `meridian` / `meridian app` launch kicks scan after 24h; next text command shows one-line cleanup hint.
- Session-scoped `MERIDIAN_HOME` test isolation. Tests no longer leak project dirs into user state.
- AG-UI replay cursor pagination core. Raw seq cursor, lazy history iterator, invalid cursor errors.
- WebSocket replay attach flow. Client sends `replay_ack` cursor; live stream skips already replayed events and keeps terminal sentinel.
- `meridian spawn cancel-all`. Cancels all running spawns, optionally scoped to work item.
- HCP core skeleton: capabilities, errors, lifecycle types, session manager. App lifespan restores HCP chats.
- HCP harness adapters for Claude, Codex, OpenCode. HCP chats persist native session IDs from connection or stream events.

### Fixed
- Fresh managed `meridian codex` attach now waits for rollout `session_meta`, not full bootstrap turn completion.
- Claude AG-UI assistant snapshots now emit thinking, tool calls, and exact text newlines.
- AG-UI replay lazy history scan no longer loads full `history.jsonl`.
- Model alias resolution no longer fails dry-run/policy paths when mars reports the target harness binary is unavailable on the host. Explicit mars harness route still used.
- HCP chat launch failure now finalizes spawn and stops session. Active HCP chats get heartbeat. Restore skips stopped chats.
- HCP adapters no longer start harness connections; SpawnManager owns lifecycle.
- Codex launch spec now carries base, developer, user-turn instruction channels for managed websocket plumbing.
- Codex subprocess projection inline again, so spawn inventory/report text reaches child prompt.
- OpenCode streaming now sends system instructions via message `system`, user/context via `parts`.
- OpenCode HTTP connection: removed dead API path probing (`/sessions`, `/api/health`, cancel/stop variants), HTML workaround code, and speculative payload variants. Paths now match known opencode API surface.
- OpenCode TUI projection no longer emits `--variant` (only valid for `opencode run`, not the bare TUI command). Was causing `meridian opencode` to exit immediately with help text.
- OpenCode workspace projection now emits `external_directory` as `{path: "allow"}` object instead of array. Matches opencode's Effect/Zod permission schema.
- OpenCode and Codex primary launches always use managed backend (`serve` → HTTP API → `attach`). Previously gated to resume-only, which forced fresh launches through black-box TUI path — losing system prompt delivery and session tracking.
- OpenCode TUI projection no longer emits `--prompt` for interactive launches. System prompt is delivered via managed backend's message system field; user types the first message.
- OpenCode system prompt now materialised as temp file in `/tmp` and injected via `OPENCODE_CONFIG_CONTENT` `instructions` config. Path is opaque to the model (OpenCode prefixes instructions with `Instructions from: <path>`). Merges with existing `OPENCODE_CONFIG_CONTENT` entries.
- OpenCode spawn message delivery now uses `prompt_async` endpoint (fire-and-forget, 204). Old `/message` endpoint streamed the LLM response in the body — early `response.release()` was cancelling prompt execution server-side, so spawns never got assistant responses. Falls back to `/message` for older OpenCode versions.
- OpenCode adapter no longer sets `agent_name` on launch spec. OpenCode doesn't support native meridian agents; agent body goes via system prompt composition.
- OpenCode adapter no longer sets `OPENCODE_WORKSPACE_ID=meridian` env override. Was causing "Workspace Unavailable" popup in TUI.
- Spawn finalization now treats `history.jsonl` as output before legacy `output.jsonl`.
- Windows CI path assertions now use Meridian's slash-normalized prompt and hook payload paths.
- Concurrent `work ensure` metadata initialization now serializes status-file creation before atomic replace.
- App locator prune test no longer expects POSIX UDS cleanup on Windows.
- Hook timeout cleanup terminates the whole subprocess tree before draining pipes. Windows shell wrappers no longer leave child hooks holding stdout open.
- Plugin API file-lock contract test reads the PID after releasing the lock, matching Windows exclusive-lock semantics.
- Smoke tests run CLI subprocesses against the repo project explicitly. Windows no longer burns time resolving `uv run meridian` from each temp repo.

## [0.0.44] - 2026-04-24

### Added
- `meridian models list --all` — delegates to mars, shows all alias-filter candidates.
- `meridian test chat` — single-spawn browser chat.
- `meridian kg` — knowledge graph analysis CLI. `kg` bare shows stats + hints. `kg graph` shows link topology tree with box-drawing connectors, `--depth N` (default 3), `--external`, `--exclude`, `--format json`. `kg check` broken-link CI gate (exit 0/1). `.kgignore` for persistent exclusions via `pathspec`. Renamed from `lib/kb` → `lib/kg`.
- `meridian mermaid check` — mermaid diagram validation. Python heuristic parser (default), optional JS strict parser with Node.js. Scans `.md`, `.mmd`, `.mermaid` files. `--depth`, `--exclude`, `--format json`, `.mermaidignore`. Catches unknown diagram types, unclosed directives, mismatched blocks.
- `lib/ignores.py` — shared gitignore-style pattern loader (used by kg and mermaid).
- `pathspec` dependency for gitignore-style ignore file matching.
- Per-spawn details endpoint, infinite scroll.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- `meridian kg check` now reports broken links, `[!FLAG]` blocks, and git conflict markers. Broken links and flags are warnings (exit 0); conflict markers are errors (exit 1). `--strict` makes warnings exit-affecting. JSON includes all categories/counts. No early exit.
- `git-autosync` rebase conflicts stay in clone for review by default; `conflict_policy = "abort"` restores old abort behavior. Future runs detect existing rebase state, skip all operations.
- `mars-agents` 0.1.17 → 0.1.18. Mars alias defaults (`default_effort`, `autocompact`) now flow into Meridian model resolution.
- Manual e2e guides for repeatable CLI checks migrated to automated `tests/smoke/` tests. `tests/e2e/README.md` now lists only remaining manual harness/network guides.
- `mars-agents` 0.1.16 → 0.1.17. Three-step resolve, three-tier list, user wildcards.
- `lib/kb` → `lib/kg`. Coverage/symbol analysis removed.
- `BroadcastHub[T]` extracted from duplicated broadcasters.
- Runner split into phase helpers (#97). Reaper split via strategy (#96). Observer into connection contract. `primary_meta.py` + `managed_primary.py` extracted.

### Fixed
- `resolve_model` passes mars-resolved `model_id` to harness (`opus-4-6` → `claude-opus-4-6`).
- Watchdog flag reconcile on early completion.
- Observer mode stale flag on restart.
- Spawn fallback respects finalizing for `live_first`.
- Drain loop grace period before force-cancel.
- Leaked subprocess on launcher death.
- OpenCode `turn_active` mapping.
- Finalizing prefers transcript over stale live output.
- Session seed double-seeding.
- Hide attempt exit fields on active spawns.

### Added
- **Extension system**: `meridian ext list|show|commands|run` CLI commands and `extension_list_commands`/`extension_invoke` MCP tools. Offline discovery works without app server; app-bound invocation uses locator + token auth. Exit codes: 2=no server, 3=stale, 7=invalid args.
- **Extension registry CLI generation**: All CLI command modules switched from `registration.py` to registry-based `ext_registration.py`. Handler keys now fully qualified (e.g. `meridian.work.start`). Old `registration.py` deleted.
- **`ExtensionCommandSpec` augmentation**: `cli_group`, `cli_name`, `agent_default_format`, `sync_handler` fields. `from_op()` factory wraps op-style handlers. Registry gains `get_by_cli()` and `list_for_cli_group()`.
- **Remote extension invoker**: Shared `RemoteExtensionInvoker` with sync/async methods for CLI and MCP dispatch.
- **`lib/markdown`** — thin wrapper around `markdown-it-py` for heading, fenced block, link, image, and wikilink extraction.
- **`lib/kg`** — knowledge graph analysis: broken link detection, orphan identification, missing backlinks, connected clusters. `meridian kg graph` and `meridian kg check` commands; `/api/kg/*` HTTP routes.
- **`lib/mermaid`** — Python wrapper for mermaid diagram validation via bundled JS parser. Node.js preflight, per-block validation with timeout.
- **`lib/core/depth.py`** — extracted depth helpers from inline usage across CLI, doctor, reaper, and work lifecycle.
- `lib/core/formatting.py` — shared text formatting (`tabular`, `kv_block`) extracted from CLI layer so ops/catalog models no longer import `cli.format_helpers` (#85).
- `ResolvedContext.from_environment()` accepts explicit `explicit_project_root` / `explicit_runtime_root` kwargs — context resolution no longer mutates `os.environ` (#81).
- `plugin_api` contract tests pin the narrowed public surface and verify unstable helpers stay in submodules.
- **Managed primary attach**: `PrimaryAttachLauncher` orchestrates backend + TUI lifecycle for Codex/OpenCode. Activity tracking via `primary_meta.json` sidecar. TOCTOU port retry (3 attempts). Black-box fallback on managed startup failure. Codex/OpenCode non-fork → managed path; Claude and fork → black-box.
- **Primary observer mode**: Codex and OpenCode connections support observer mode — skip initial turn/message, Codex declines server RPCs with -32601 so TUI handles approvals.
- **Primary transcript resolution hardening**: Primary sessions resolve via native harness transcripts only. Lazy session ID detection with persistence. Harness adapters respect `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `XDG_DATA_HOME`.
- App server: multi-viewer WebSocket with `EventBroadcaster` fan-out, 30s keepalive ping, 90s stale timeout.
- Extension manifest hash now includes `args_schema` and `result_schema` — schema-only changes rotate the hash.
- Extension invocation observability: app server dispatcher writes to `extension-invocations.jsonl`.
- Extension invoke accepts `work_id` and `spawn_id` selector fields through CLI/MCP/HTTP.
- Constant-time token comparison via `secrets.compare_digest` in app server auth.
- Local `meridian ext run` dispatches in-process for extensions that don't require app server.
- **Frontend AppShell**: Extension-driven shell with ActivityBar, TopBar, StatusBar, ModeViewport. Modes register via `ExtensionRegistry` singleton without shell hardcoding.
- **Frontend Sessions mode**: Live spawn list with FilterBar, grouped by work item, SSE-backed refetch, context menu actions (cancel/fork/archive). StatusBar shows live spawn counts.
- **Frontend Chat mode**: Multi-column spawn view (up to 4 side-by-side). ChatContext with LRU eviction, SessionList sidebar, SpawnHeader with streaming controls, ThreadColumn with composer.
- **Frontend ⌘K command palette**: Fuzzy search over registered commands via `cmdk`. Mode switching, new session, theme toggle. Global `⌘K`/`Ctrl+K` shortcut.
- **Frontend NewSessionDialog**: Submits to `POST /api/spawns` with agent/model/prompt selection.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Agent-mode CLI output defaults to text for all commands. Prior JSON defaults on `config.get`, `spawn.cancel`, `spawn.create`, `spawn.continue`, `spawn.wait`, `context`, `work.current` flipped to text. Explicit `--json` still available.
- `spawn.wait` omits report body by default; pass `--report` to include. Report path always shown.
- Lifecycle events (`meridian.spawn.start`, `meridian.spawn.done`) suppressed in agent mode for all commands. Human mode routes `TextSink.event()` to stderr.
- `SpawnWaitMultiOutput` and `SpawnDetailOutput` now have sparse `to_cli_wire()` projections for explicit JSON; omit internal fields like `harness_session_id`, `log_path`, `process_exit_code`.
- Background spawn output now explicitly instructs agents to wait: "You MUST run..." with machine-actionable fields `terminal`, `wait_required`, `wait_command` in JSON output.
- `MERIDIAN_DEPTH` parsing and nested-execution checks now share one core helper. CLI agent mode, doctor, reaper, work warnings, subrun events, and max-depth gates use same zero-based contract.
- Docs and KB now spell out zero-based depth, immediate parent spawn linkage, and fail-closed root-only repair gates.
- `plugin_api` public surface narrowed to hook types + state helpers only. Unstable utilities (`file_lock`, `generate_repo_slug`, `normalize_repo_url`, `resolve_clone_path`, `get_git_overrides`, `get_user_config`) moved to submodule imports (#94).
- 7 ops/catalog modules now import formatting from `lib/core/formatting` instead of `cli.format_helpers`. CLI shim re-exports for backwards compat (#85).
- User docs audited: `mcp-tools.md` rewritten (was listing 16 deleted tools), `commands.md` fixed wrong command names, `configuration.md` fixed stale `models.toml` section, `troubleshooting.md` fixed artifact paths.
- Codex WebSocket message size limit split from shared harness constant to per-adapter value.
- `OperationSpec` collapsed into `ExtensionCommandSpec` via `from_op()`. `OperationSpec` class, `OperationSurface`, and related APIs deleted from `manifest.py`.
- `ExtensionSurface.ALL` removed — surface sets now explicit per command (`{CLI, MCP, HTTP}`).
- App server health endpoint no longer requires auth — explicitly public.
- `archive_spawn` and related helpers promoted from private `_archive_spawn` to public API.

### Removed
- `registration.py` — CLI command registration replaced by extension registry.
- Old MCP `OperationSpec` → MCP tool projection in server. Extension system's `extension_list_commands`/`extension_invoke` replaces it.
- `ops_bridge.py` intermediate layer — collapsed into direct `from_op()` calls.

### Fixed
- `spawn show` no longer prints `Exited at` / `Process exit code` for active retrying spawns. Attempt-exit fields stay visible only after terminal status, so active retries no longer look stuck-finalized.
- Primary launch preserves `MERIDIAN_DEPTH`; root sessions stay depth `0` while delegated spawns still increment.
- Malformed non-empty `MERIDIAN_DEPTH` no longer enables root-only repair/reaper side effects.
- App server startup no longer crashes on Windows — `os.fchmod` guard behind `IS_WINDOWS` (#87).
- `_read_mars_merged_file` no longer falls back to `Path.cwd()` when `project_root` is None; returns empty dict instead of silently loading wrong project's aliases (#91).
- Context resolution (`ops/context.py`) no longer temporarily mutates `os.environ`; uses explicit kwargs instead (#81).

## [0.0.43] - 2026-04-22

### Added
- Nested Claude managed spawns now deny native delegation tools (Agent, TaskCreate, TaskGet, TaskList, TaskOutput, TaskStop, TaskUpdate) by default. Profiles opt out per-tool via `tools:` frontmatter listing. Prevents untracked sub-agent spawns outside Meridian policy.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- `resolve_child_execution_cwd()` always returns `project_root`. Prior CLAUDECODE→spawn_log_dir redirect removed; `.claude/settings.json` now discovered correctly in nested Claude contexts.

### Fixed
- Nested spawns keep `MERIDIAN_PROJECT_DIR` on project root when harness cwd moves to spawn artifact dir. Agent profile lookup no longer searches `.agents/` under artifact dirs.

## [0.0.42] - 2026-04-22

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Spawn prompt projection now has one shared inline path. Codex and OpenCode inherit base inline projection: system instructions, task context, then user task.
- Harness adapter docs now name the canonical prompt category routing and inline block order.

### Removed
- Dead `PromptPolicy` / `filter_launch_content` prompt-composition API.
- OpenCode `--file` reference projection. References now inline or omit empty files until real native delivery exists.

### Fixed
- Spawn prompt composition now always includes loaded skills and agent inventory before harness projection, so Claude/Codex/OpenCode receive the same semantic payload through their supported channels.
- OpenCode streaming no longer drops `-f` reference content by advertising native file injection it cannot deliver.

## [0.0.41] - 2026-04-22

### Fixed
- Claude spawn-prepare now projects loaded skills, agent inventory, and report instructions into `system-prompt`/`append-system-prompt` for fresh and continued sessions; system prompt no longer report-only.

## [0.0.40] - 2026-04-22

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- App chat UI migrated from frontend-v2.
- Thread activity internals now named around spawn activity and stream control.
- Agent mode output defaults now per command: control-plane -> JSON, read/browse -> text.
- JSON mode no hidden JSONL `AgentSink` envelope; command JSON writes direct.

### Fixed
- Git-backed context roots now project to harness launches as `--add-dir`, so Claude/Codex can read work/kb files under context clones without extra prompts.
- App streaming clears when harness emits `STEP_FINISHED`.
- Cancelled AG-UI events now emit `RUN_ERROR` with `isCancelled`.

## [0.0.40-rc.2] - 2026-04-22

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Default app server port changed from `8420` to `7676`. Vite proxy config updated to match.
- **Work items: directory is the work item.** Eliminated `work-items/` metadata index. Work item exists iff its directory exists in `work/` (active) or `archive/work/` (done). `__status.json` inside each dir holds mutable metadata. `meridian work list` scans the actual work directory — no separate index to drift. Auto-heals missing/malformed status files. Fixes #69, #70.
- `work list --done` now paginated: shows last 10 by default, `-n N` for custom limit, `--all` for everything.
- Archive/reopen crash-safe: archive moves dir first then writes metadata; reopen clears metadata first then moves. Crash leaves recoverable state.
- No lock files for work operations — all ops are single atomic steps or idempotent. Eliminates `fcntl.flock` from work path (Windows first-class).
- `.meridian/id` now committed to git — stable project identity across clones/worktrees. `ensure_gitignore()` migrates old `.gitignore` files automatically (strips `id` ignore, adds `!id` to required lines).
- **Naming overhaul**: no "repo" or "state root" anywhere. `repo_root` → `project_root`, `state_root` → `runtime_root`, `MERIDIAN_REPO_ROOT` → `MERIDIAN_PROJECT_DIR`, `MERIDIAN_STATE_ROOT` → `MERIDIAN_RUNTIME_DIR`, `get_user_state_root` → `get_meridian_home`, `get_project_state_root` → `get_project_data_root`, `StatePaths` → `ProjectPaths`, `StateRootPaths` → `RuntimePaths`, `RepoStatePaths` → `ProjectPaths`, `.state_root` field → `.runtime_root`. Breaking rename — no backwards compat aliases.

### Fixed
- Background spawns use `--project-root` for worker launch. No stale `--repo-root` crash after rename.
- `MERIDIAN_SPAWN_ID` now set to current spawn's own ID; `MERIDIAN_PARENT_SPAWN_ID` set to parent. Previously both were swapped.
- `spawn children` agent-mode output uses children view instead of raw spawn list.
- Integration tests no longer crash on structlog writes to stale capsys buffers (reset moved to integration conftest).

### Removed
- `work-items/` directory, `work-items.flock`, `work-items.rename.intent.json` — all replaced by directory-as-work-item model.
- `RuntimePaths.work_items_dir`, `work_items_flock`, `work_items_rename_intent` fields.
- `WorkRenameIntent` model, `reconcile_work_store()` function, all `lock_file()` calls in work_store.

## [0.0.40-rc.1] - 2026-04-22

### Added
- Launch artifacts now emit `references.json` when references exist, with per-item routing (`inline`, `native-injection`, `omitted`) and native flag detail.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Claude primary launch now separates system instructions from the starting user prompt instead of appending the full prompt to system.
- Launch artifacts now write from one shared projection path. Primary uses adapter `ProjectedContent` as authority for `system-prompt.md`, `starting-prompt.md`, and `projection-manifest.json`.
- Spawn prepare now excludes OpenCode native-injected files from inline prompt content, so `--file` delivery is single path, not duplicated inline+native.
- `session log` now reads active spawn `output.jsonl` when harness transcript missing, with source shown in output.

### Removed
- Spawn execute path no longer writes legacy `prompt.md` or `delivery-manifest.json` artifacts.
- `spawn log` command removed. Use `session log <spawn_id>`.

### Fixed
- Projection manifest routing for Codex/OpenCode primary launches now reflects adapter-declared inline channels instead of Claude-only defaults.
- Direct spawn execution now reloads `reference_files` into launch context so OpenCode native `--file` routing and `references.json` are computed from authoritative reference items.

## [0.0.39] - 2026-04-21

### Added
- **`-f` directory support**: `-f dir/` renders depth-3 tree in prompt (blocked dirs annotated, deterministic sort, cross-platform). Files always inline regardless of harness. Orchestrators pass context packages without enumerating every file.
- **Hook system**: Event-driven hooks for lifecycle events (`spawn.created`, `spawn.running`, `spawn.finalized`, `work.started`, `work.done`). External hooks (subprocess) and built-in hooks (Python). CLI: `hooks list`, `hooks check`, `hooks run`.
- **git-autosync**: Built-in hook for syncing git-backed contexts. Auto-registers when `source = "git"`. Interval throttling, fail-open semantics.
- **App server Phase 1-3**: Sessions/SSE/Work facade endpoints, Files mode, spawn archive, catalog endpoints, and thread inspector endpoints.
- **`meridian.local.toml`**: Personal config overrides, gitignored. Precedence: local > project > user.
- **Context backend**: Git-backed contexts via `[context.work]` and `[context.kb]` with `source = "git"` and `remote = "..."`. Paths resolve to `~/.meridian/git/<slug>/`. Lazy clone — bootstrap skips git-backed dirs, git-autosync handles cloning.
- **Plugin API v1**: Stable contract at `meridian.plugin_api` for hooks/plugins. Exports: hook types, state helpers, git helpers, config helpers, file locking.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- CLI help text updated: root epilogue and `spawn` description now advertise primary launch/resume/fork forms, session ref syntax (`c123`/`p123`/raw), foreground capture fallback (Unix TTY, falls back to subprocess on Windows/non-TTY), and correct `--autocompact` range (1-100). Agent root help updated to match.
- `spawn show/children/files/cancel/wait/log` accept chat_id refs (e.g. `c213`). Resolves to most recent spawn with that chat_id.
- `meridian context` command — returns context tuple (`work_id`, `repo_root`, `state_root`, `depth`). JSON when spawned or with `--json`; human-friendly text in TTY.
- Git clone slug shortened: `meridian-flow-docs` instead of `github.com-meridian-flow-docs`. Collision detection still works (errors if existing clone has different remote).
- Context resolver is now pure — no clone side effects. Bootstrap skips mkdir for git-backed paths.

### Fixed
- `spawn --from c123` now uses the chat's primary spawn and transcript pointer. No more latest-child report bleed. `spawn --from p123` keeps concrete spawn report/files context.
- Top-level unreadable `-f` directory now raises `PermissionError` instead of silent empty tree.
- Windows: `_fsync_directory` no-op on Windows (not supported).
- Windows: `output.jsonl` capture enabled on Windows.
- Windows: Guardrails platform dispatch for `.cmd`/`.ps1` scripts.
- Windows: `Path.home()` → `get_home_path()` to respect `HOME` env var.
- Windows: fcntl test skip on Windows.
- Windows: Path assertion normalized for cross-platform.
- git-autosync event name: `work.start` → `work.started` to match actual lifecycle dispatch.
- `source = "git"` without `remote`: warns and falls back to local instead of broken state.
- App server path security hardened: resolved-root validation, traversal guards, Unicode path coverage, and delete/rename boundary checks.
- Primary launch prompt materialization fixed for process projection path.
- Spec-driven launch argv projection now handles typed harness fields.
- Native reference delivery now carries `reference_items` through launch specs.

## [0.0.33] - 2026-04-17

### Fixed
- `meridian opencode` primary launch passed the startup prompt as the root positional `project` arg instead of OpenCode's `--prompt` flag. OpenCode tried to `open()` the whole session prompt as a path and quit immediately with `ENAMETOOLONG`.

## [0.0.32] - 2026-04-17

### Fixed
- Primary launch (`meridian` with no subcommand) dropped into JSON streaming mode instead of interactive TUI. Regression from 0.0.31 launch refactoring — `interactive` flag wasn't propagated to run inputs for PRIMARY composition surface.
- Primary launch viewport sizing: PTY now created with correct terminal dimensions before child process starts. Was using `pty.fork()` which sets size after child starts; now uses `pty.openpty()` + manual fork so child sees correct size from first query.

## [0.0.31] - 2026-04-17

### Added
- `workspace.local.toml` support for multi-repo context injection. Declare `[[context-roots]]` entries pointing at sibling repos; meridian projects them to harness launches. Local-only file, gitignored by default.
- `workspace init` command creates template file with commented examples, adds local gitignore coverage via `.git/info/exclude`.
- `config show` workspace surface: status, root counts, per-harness applicability. JSON: `workspace = {status, path?, roots:{count,enabled,missing}, applicability:{claude,codex,opencode}}`.
- `doctor` workspace findings: `workspace_invalid`, `workspace_unknown_key`, `workspace_missing_root`, `workspace_unsupported_harness`.
- Launch projection: Claude (`--add-dir`), OpenCode (`permission.external_directory` env). Codex deferred to `harness-permission-abstraction` (requires CODEX_HOME config generation).
- Invalid workspace pre-launch gate blocks spawn before harness contact.
- Shared `ConfigSurface` builder unifies `config show` and `doctor` workspace state.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Bundled `mars-agents` 0.1.2 → 0.1.3.
- Workspace file location follows `MERIDIAN_PROJECT_ROOT` — lives at `state_root.parent / workspace.local.toml`.

## [0.0.30] - 2026-04-16

### Added
- New `finalizing` spawn state between `running` and terminal. Covers the harness-exited-but-drain-in-flight window so the reaper stops stamping live spawns mid-drain. Shows up in `spawn show`, stats, and `--status` filter.
- `spawn show` renders `orphan_finalization` distinct from `orphan_run` — tells apart drain-window hangs from runner-dead-during-run.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- `meridian-dev-workflow` bumped 0.0.25 → 0.0.26 via `meridian mars sync`.
- `@impl-orchestrator` now runs a mandatory Explore phase before planning — verifies design against code reality, produces `plan/pre-planning-notes.md` as a gate artifact, terminates to a Redesign Brief when design is falsified.
- `agent-staffing` skill: new "Fan-Out vs Parallel Lanes" terminology section (same-prompt-different-models vs different-prompts-different-focus-areas); new `@reviewer as Architectural Drift Gate` section (CI-spawned reviewer enforces structural invariants semantically against a declared-invariant prompt).
- `AGENTS.md` model-routing block removed — model choice delegated to profile defaults and `meridian models list`.
- Reaper no longer false-positives over live spawns. Heartbeat window 120s, 15s startup grace, PID-reuse margin 30s, depth-gated so nested sweeps can't stamp their parents. Authority rule: runner/launcher/cancel writes always win over reconciler writes, so a late report corrects a premature stamp. Fixes recurrence of #14.
- Runner owns the heartbeat task now (30s tick, cancelled in outer `finally`), not the reaper.
- `finalize_spawn(..., origin=...)` is mandatory at every call site.
- `update_spawn` no longer accepts `status=` — lifecycle transitions go through `mark_finalizing` / `finalize_spawn` only.
- `meridian-base` package ref bumped to pick up refined prompt-writing guidance in `agent-creator` and `skill-creator`.
- Bundled `mars-agents` bumped from `0.0.14` to `0.1.1`.

### Fixed
- Codex adapter silently truncated initial prompts over 50 KiB, emitted a `warning/promptTruncated` event, and continued with the mutilated input — turning over-limit planner briefs into "no task provided" runs. Claude and OpenCode had no analogous ceiling at all. All three adapters now share one 10 MiB ceiling via `validate_prompt_size()` in `lib/harness/connections/base.py`; over-limit prompts raise `PromptTooLargeError` naming actual vs allowed bytes and the harness, before any transport contact.
- `meridian spawn --help` text now matches behavior. Was "Runs in background by default. Use --foreground to block." — both halves wrong (default is foreground, `--foreground` flag does not exist). Now describes `--background` as the opt-in flag it actually is.

### Removed
- "awaiting finalization" heuristic in detail view. Replaced by real `finalizing` status.
- Checked-in git submodules `meridian-base/` and `meridian-dev-workflow/`. Mars package deps now source of truth.

### Reverted
- R06 launch-refactor skeleton (8 commits, `3f8ad4c..45d18d7`) that landed post-v0.0.29 but never shipped in a tagged release. Skeleton was built while the codex prompt-truncation bug above was corrupting coder briefs; smoke evidence after Fix A revealed structural regressions (fork lineage split-brain in new `launch/fork.py`, row-before-fork ordering, OpenCode report extraction returning raw `session.idle` envelopes). Design package preserved under `.meridian/work/workspace-config-design/` as input for a clean retry on top of the restored tagged-stable baseline. No user impact since v0.0.29 is before the skeleton.

## [0.0.28] - 2026-04-13

### Added
- Primary `meridian` launch startup agent catalog. Fresh and forked sessions now show installed agents before user input. Claude gets it in appended system prompt; Codex and OpenCode inline.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Startup inventory now agent-only. Skills still load through normal harness launch path, but not duplicated in startup catalog.

### Fixed
- `session log` and `session search` now tell `chat not found` apart from `chat has no transcript yet`.
- Chat ref resolution now falls back to primary spawn harness session id when the chat row has none.
- `pytest-llm` launcher now uses current interpreter path more reliably.

## [0.0.27] - 2026-04-12

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Dev workflow package updated for unified `impl-orchestrator`.

### Fixed
- Spawn model validation now resolves models from the Meridian repo root instead of drifting with CWD.
- Codex streamed report extraction now accepts current event names instead of dropping final agent output.

## [0.0.26] - 2026-04-12

### Added
- **Streaming runner**: bidirectional streaming spawn pipeline. All three harnesses (Claude, Codex, OpenCode) route through unified `execute_with_streaming` path with connection-level event consumption, budget tracking, and retry.
- **`ResolvedLaunchSpec` hierarchy**: transport-neutral launch spec per harness. `ClaudeLaunchSpec`, `CodexLaunchSpec`, `OpenCodeLaunchSpec` — each adapter owns `resolve_launch_spec()` and `build_command()`. Replaces strategy maps.
- **`--debug` mode**: structured JSONL tracing across all pipeline layers. `meridian spawn --debug` emits trace events for harness launch, event consumption, extraction, and finalization.
- **`psutil`-based process liveness**: cross-platform (Linux, macOS, Windows). PID-reuse detection via `create_time()`. Replaces `/proc/stat` parsing and `os.kill(pid, 0)`.
- **`SpawnExitedEvent`**: new event type separating process exit from finalization. Spawn stays `running` after process exits until report extraction completes — prevents false orphan detection.
- **`runner_pid` tracking**: each spawn records which PID is responsible for finalization. Foreground spawns set it in `start` event; background spawns set it in `update` after wrapper launches.
- **`MERIDIAN_ACTIVE_WORK_DIR` and `MERIDIAN_ACTIVE_WORK_ID` exported** into harness sessions.
- `CHANGELOG.md` resumed after staleness. Now in caveman style.

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- **Reaper rewrite**: 500-line state machine → 119 lines (~30 core). No PID files, no heartbeat, no foreground/background dispatch. Just: is `runner_pid` alive? Branch on `exited_at` presence.
- **PID/heartbeat file elimination**: `harness.pid`, `background.pid`, `heartbeat` removed. PIDs come from event stream only. Spawn directories are artifact-only.
- **`SpawnExtractor` protocol**: extraction split from adapter into composable protocol. `StreamingExtractor` wraps harness bundle for connection-aware extraction.
- **Streaming parity**: all three harnesses converge on shared launch context, env invariants, permission pipeline, and projection paths. 8-phase implementation.
- **Bundle registry**: immutable after registration. Import-time side effects populate global registry.
- Claude readline limit raised to 128 MiB for large conversation echoes.
- `.agents/` and `.claude/` removed from tracking — generated output only.

### Fixed
- Spawn orphan false-failures: `exited` event + psutil liveness prevents reaper from racing runner's post-exit finalization.
- Streaming runner completion/signal races: F2 residual race when completion and signal land on same wakeup.
- Harness binary not found now produces diagnostic error instead of silent failure.
- Codex: server-initiated JSON-RPC requests handled; send lock prevents interleaved writes.
- OpenCode: chunked response handling on message POST.
- SIGTERM masked during `streaming_serve` finalization — prevents double-cleanup.
- Continue/fork wired for Claude and Codex streaming adapters.
- Child env `WORK_DIR` fallback and `autocompact` inheritance (#12).
- Effort field wired through `PreparedSpawnPlan` to both runners.

## [0.0.5] - 2026-03-21

### Added
- `gpt52` builtin alias for `gpt-5.2`; Claude `tools` passthrough in launch plan

### Changed
- Pi lifecycle machine events now transport via append-only JSONL sidecar file (`pi-lifecycle-events.jsonl`) instead of stdout/stderr; extensions write via `fs.writeSync` append-mode fd, Python reads via `PiLifecycleEventTailer` with forced catch-up before quiescence decisions; no lifecycle JSON leaks to TUI or stderr.
- Git autosync: extract `autosync_store.py` — single owner of `.meridian/autosync/` file layout. Eliminates duplicated path construction and JSON parsing across `git_autosync.py`, `sync_conflicts.py`, `context.py`.
- Auto-resolve builtin aliases from discovered models; manifest-first bootstrap

## [0.0.4] - 2026-03-17

### Added
- Model catalog split with routing, visibility, descriptions, and `models.toml` config

## [0.0.3] - 2026-03-17

### Added
- Bootstrap state tracking with builtin skills and source recording; designer agent

## [0.0.2] - 2026-03-17

### Fixed
- `.meridian/.gitignore` seeding and stale CLI commands in docs

## [0.0.1] - 2026-02-25

Initial release — core CLI (`spawn`, `session`, `work`), harness adapters (Claude Code, Codex, OpenCode), agent profiles, skill system, sync engine, JSONL state stores.
