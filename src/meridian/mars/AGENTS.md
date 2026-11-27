# Content Meridian ships for Mars

Mars reads this directory in place; nothing copies it into a project's `.mars/`.

- Lookups (launch bundles, `mars agents`, `mars skills`) get it as `--provided-root <this
  directory>`, the last layer of every by-name lookup (`mars_content_args` in
  `lib/catalog/mars_cli.py`). `meridian skills` reads `skills/` from the wheel itself
  (`lib/bundled_skills.py`).
- `meridian mars sync` gives it to the sync as `--provided-root meridian@<version>=<this
  directory>` (`provided_sync_args`): `skills/`, `agents/`, `hooks/` and `mcp/` become the host
  layer, installed only into targets whose harness Meridian does not launch, today `.agy`.

`agents/` holds profiles only; any other `*.md` there, an `AGENTS.md` included, would be read as
an agent.

- Pass this directory, never its parent. A sync's host layer is what this directory holds, so
  moving or emptying a subdirectory retracts it from every project's `.agy` on the next sync;
  move it in one change with `provided_dir` in `lib/catalog/mars_cli.py`.
- Every `hook.toml` says `visibility = "exported"`, or the dependency reader skips it.
- `hooks/meridian-session/guard.sh` hard-codes the state paths in `lib/harness/agy/state.py`
  (requests, rows, `briefed` and `seen` markers, the inbox file, and the
  `busy` file it creates on `PreInvocation`); change them together. `agy.json` names the events
  it runs for, and `lib/harness/agy/commands.py` (`HOOK_EVENTS`, which `hook.py` reuses) lists the same events. The entry names `meridian-session` and `meridian`
  key the entries in a project's `.agy/`, so they must not collide with a user's.
- Skills follow [skills/AGENTS.md](skills/AGENTS.md).
