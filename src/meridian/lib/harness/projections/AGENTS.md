# harness/projections/ — Spec-to-Args Projection

Maps `HarnessLaunchSpec` fields to CLI flags, environment variables, and payload
content for each harness and launch mode. Step 2 of the translation pipeline
(`HarnessLaunchSpec` → `list[str] + env dict`).

## Mental Model

Each projector is a pure function: `spec → (argv_additions, env_overrides)`. It
knows everything about how a specific harness and mode receives its configuration —
which flags exist, which are mutually exclusive, what goes in env vs argv.

**Drift guard:** each projector declares `_PROJECTED_FIELDS` and `_DELEGATED_FIELDS`;
`check_projection_drift()` raises `ImportError` at import when a spec field is in neither.

## Per-Harness Notes

- **Claude** (`project_claude.py`): single module covers both subprocess and streaming;
  uses `--append-system-prompt-file` channel to avoid `ARG_MAX` limits.
- **Codex** (`project_codex_subprocess.py` + `project_codex_streaming.py`): split because
  `codex exec` and `codex app-server` accept different flag sets.
- **OpenCode** (`project_opencode_subprocess.py` + `project_opencode_streaming.py`):
  the optional prompt, compiled permission policy, fresh-session model and workspace roots go through
  `OPENCODE_CONFIG_CONTENT` (JSON), built at bind by `project_opencode_config.py`, not
  flags or a connection-time merge. `spec.agent_name` is selected wherever a session is opened or resumed; see
  [OpenCode prompt, agent and model delivery](../.context/CONTEXT.md#opencode-prompt-agent-and-model-delivery).
- **Permission compile** (`permissions_<harness>.py`): turns `spec.permission_policy` into a
  typed native value plus `EnforcementReport`; projectors spell the result as argv, env or
  payload and nothing else reads the policy's rules. Report and `effective_policy` ports
  (what the child-spawn guardrail compares): [.context/CONTEXT.md](.context/CONTEXT.md).

## Key Rules

**Don't call projectors directly from outside the adapter layer.** They are called
by `adapter.resolve_launch_spec()` — that's the only intended callsite.

**An interactive starting prompt is argv's last item, after `--`.** Exception: a
fresh Claude primary that loaded `meridian-first-turn` gets no positional prompt; the
mod submits `starting-prompt.md` (named by `_MERIDIAN_CLAUDE_FIRST_TURN_PATH`) as the
first turn, so the prompt is off the command line (`delivers_first_turn_by_mod`).
Claude's
`--add-dir <directories...>` is variadic: with no terminator it swallows a trailing
positional prompt, and the TUI opens with an empty composer. Claude and Pi end with
`-- <prompt>`; the bare OpenCode TUI takes `--prompt <prompt>`. Pass every prompt
argument through `_prompt_arg.check_prompt_argument` (rejects ≥ 128 KiB, the Linux
per-argument limit). Pi reads a leading `@` as a file reference even after `--`, so
its projector prefixes one space. 

## Entry Points

Each `project_<harness>_<mode>.py` exports one top-level function:
`project_<harness>_spec_to_cli_args(spec) → (list[str], dict[str, str])`.

`_guards.py` — `check_projection_drift()`: the import-time enforcement mechanism.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — drift guard mechanics, field accounting
   rules, permission flag mapping table, OpenCode env-based projection detail.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — translation pipeline; where
   projection fits; `_PROJECTED_FIELDS` / `_DELEGATED_FIELDS` invariant.
