# Content Meridian ships for Mars

`meridian mars sync` runs `mars provide meridian@<version>=<this directory>`. Mars reads
three subdirectories: `skills/` (Meridian's own skills, for every harness), `hooks/` and
`mcp/` (copied only into targets whose harness Meridian does not launch, today `.agy`).

`agents/` is not provided: Mars reads it in place as the last layer of every by-name lookup
(`--provided-root <this directory>`, sent by `mars_content_args` in `lib/catalog/mars_roots.py`).
It holds profiles only; any other `*.md` there, an `AGENTS.md` included, would be read as an
agent.

- Provide this directory, never its parent. A provided directory with none of the three
  subdirectories retracts everything Meridian provided, so moving or emptying one is one
  change with the repoint in `lib/bundled_skills.py`.
- Every `hook.toml` says `visibility = "exported"`, or the dependency reader skips it.
- `hooks/meridian-session/guard.sh` hard-codes the state paths in `lib/harness/agy/state.py`
  (requests, rows, `briefed` and `seen` markers, the inbox file, and the
  `busy` file it creates on `PreInvocation`); change them together. `agy.json` names the events
  it runs for, and `lib/harness/agy/commands.py` (`HOOK_EVENTS`, which `hook.py` reuses) lists the same events. The entry names `meridian-session` and `meridian`
  key the entries in a project's `.agy/`, so they must not collide with a user's.
- Skills follow [skills/AGENTS.md](skills/AGENTS.md).
