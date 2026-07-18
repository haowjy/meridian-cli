# lib/safety/ — Context

Four focused subsystems consumed by the launch layer. None depends on another
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
- `_MERIDIAN_GUARDRAIL_OUTPUT_LOG` — path to the spawn output log
- `_MERIDIAN_GUARDRAIL_REPORT_PATH` — optional report path

**Command resolution:**
- Legacy native-Windows branch (untested): `.cmd`/`.bat` → `cmd.exe /d /c`;
  `.ps1` → `powershell.exe -NoProfile -NonInteractive -File`
- POSIX: executable bit → direct; else `bash <script>`; fallback on `OSError` → retry with `bash`

Exit code conventions: non-zero → `GuardrailFailure`; timeout → 124; `OSError` → 127.

## Permission Resolution (`permissions.py`)

`PermissionConfig` holds transport-neutral intent: `sandbox` and `approval` modes.
The resolver translates intent into harness flags.

**Resolver selection via `build_permission_resolver()`:**

| Condition | Resolver | Emits |
|---|---|---|
| Neither list provided | `TieredPermissionResolver` | `()` — harness derives from sandbox/approval |
| `allowed_tools` only | `ExplicitToolsResolver` | `--allowedTools <csv>` (Claude) |
| `disallowed_tools` only | `DisallowedToolsResolver` | `--disallowedTools <csv>` (Claude) |
| Both provided | `CombinedToolsResolver` | Combined; allowlist takes precedence for OpenCode JSON |
| Explicit unsafe opt-out | `UnsafeNoOpPermissionResolver` | `()` + warning log |

**Tool name normalization:** `_normalize_tool_name` strips Claude-style qualifiers
(`tool(args)` → `tool`) and lowercases. Applied before all allow/deny operations.

**`resolve_permission_pipeline()` in this module** is a re-export shim that delegates
to `lib/launch/permissions.py`. The launch layer owns the actual pipeline.
`PermissionConfig` and the resolver types live here.

## Secret Redaction (`redaction.py`)

`redact_secrets(text, secrets)` and `redact_secret_bytes(data, secrets)` replace
secret values with `[REDACTED:<key>]` placeholders.

**Ordering:** secrets sorted longest-first before replacement. Prevents a shorter
secret that is a substring of a longer one from partially redacting the longer
secret's placeholder.

**Empty value guard:** secrets with empty `value` are skipped.

`redact_secret_bytes` decodes bytes as UTF-8 with `errors="replace"`, redacts, and
re-encodes. Non-UTF-8 input bytes become replacement characters — the output may
differ from input in byte length.

## Circular Import Constraints

The dependency graph must remain:
- `budget.py` → `harness.common` (lazy only)
- `permissions.py` → `launch.permissions` (re-export only)

Do not add direct top-level imports between these modules and `harness.*` or `launch.*`.
The lazy import in `extract_cost_usd_from_json_line` is load-bearing.
