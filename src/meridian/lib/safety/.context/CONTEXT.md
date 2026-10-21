# lib/safety/ — Context

Three focused subsystems consumed by the launch layer. None depends on another
except through lazy imports that break circular dependency cycles.

## Budget Enforcement (`budget.py`)

`LiveBudgetTracker` is a mutable streaming tracker. Feed it one raw stdout line at
a time via `observe_json_line(raw_line)`.

**Cost field priority:** extracts the first recognized field in order:
`total_cost_usd`, `cost_usd`, `cost`, `total_cost`, `totalCostUsd`.

**Monotonic update invariant:** `run_cost_usd` only increases. Harnesses emit
running totals, not deltas. If a new value is less than the current tracked value,
it is ignored. A harness that emits deltas instead of totals will under-count.

**Breach check:** returns `BudgetBreach` on first violation. Caller is responsible
for terminating the harness — the tracker does not stop it.

**Lazy import:** `extract_cost_usd_from_json_line` imports `harness.common` lazily
to break a circular dependency: `safety.budget` → `harness.common` → `harness.adapter`
→ `safety.permissions`. Do not hoist this import to the module level.

## Post-Run Guardrails (`guardrails.py`)

`run_guardrails(guardrails, *, spawn_id, cwd, env, report_path, output_log_path)` runs
user-supplied scripts sequentially after spawn completion.

**All attempted regardless of earlier failures.** Returns `GuardrailResult(ok=False, failures=(...))`
with all collected failures.

**Env sanitization contract:** the child environment strips every key matching
`MERIDIAN_SECRET_*` before passing env to any guardrail script. This is a security
boundary — repo scripts are untrusted. Do not weaken this.

Injected env vars available to guardrail scripts:
- `_MERIDIAN_GUARDRAIL_RUN_ID` — the spawn ID
- `_MERIDIAN_GUARDRAIL_CHAT_ID` — chat ID for `meridian session log`, when known
- `_MERIDIAN_GUARDRAIL_REPORT` — optional path to `report.md`

**Command resolution:**
- Legacy native-Windows branch (untested): `.cmd`/`.bat` → `cmd.exe /d /c`;
  `.ps1` → `powershell.exe -NoProfile -NonInteractive -File`
- POSIX: executable bit → direct; else `bash <script>`; fallback on `OSError` → retry with `bash`

Exit code conventions: non-zero → `GuardrailFailure`; timeout → 124; `OSError` → 127.

## Permission Resolution (`permissions.py`)

`PermissionConfig` holds transport-neutral intent: `sandbox` and `approval` modes,
plus `opencode_permission_override`, the OpenCode permission JSON compiled from
`tools`. The resolver translates intent into harness flags.

**Resolver selection via `build_permission_resolver()`:**

| Condition | Resolver | Emits |
|---|---|---|
| `tools` is `None` | `TieredPermissionResolver` | `()` — harness derives from sandbox/approval |
| `tools` is a `ToolsField` | `ToolsPermissionResolver` | Claude `--allowedTools` / `--disallowedTools` from `compile_tools_to_claude_flags`; OpenCode JSON from `opencode_permission_json()` |
| Explicit unsafe opt-out | `UnsafeNoOpPermissionResolver` | `()` + warning log |

**`tools` is the abstract `ToolsField`** (`lib/tools.py`): a map from capability
(or scoped `bash(rg *)` key) to `allow` / `deny` / `ask`, or the `allow` / `deny`
shorthand. Each harness compiles it differently: Claude flags treat `ask` as
`allow` (no flag expresses it), OpenCode keeps `ask`, and Codex ignores the map
except for `infer_codex_sandbox_from_tools`. Profiles reach this type through
`launch/bundle_adapter.py`, which rebuilds it from Mars's `tools.allowed` /
`tools.disallowed` lists, so a profile never produces `ask`.

**`resolve_permission_pipeline()` in this module** is a re-export shim that delegates
to `lib/launch/permissions.py`. The launch layer owns the actual pipeline.
`PermissionConfig` and the resolver types live here.

**Known gaps.** Mars lowers tool names to the target harness's native names before
Meridian sees them (Codex `exec_command` / `apply_patch`, Claude `Bash` / `Edit`),
and two compilers here assume capability names:

- `infer_codex_sandbox_from_tools` looks for `bash` and `edit`, so on Codex it never
  matches and returns `read-only` whenever `tools` is set and `sandbox` is not.
- `compile_tools_to_claude_flags` lowercases each key and re-capitalizes only its
  first letter, so multi-word Claude tools (`WebSearch`, `WebFetch`,
  `AskUserQuestion`) become `Websearch` etc., which Claude ignores.

User-facing behavior is in `docs/agent-profiles.md#permissions-by-harness`.

## Circular Import Constraints

The dependency graph must remain:
- `budget.py` → `harness.common` (lazy only)
- `permissions.py` → `launch.permissions` (re-export only)

Do not add direct top-level imports between these modules and `harness.*` or `launch.*`.
The lazy import in `extract_cost_usd_from_json_line` is load-bearing.
