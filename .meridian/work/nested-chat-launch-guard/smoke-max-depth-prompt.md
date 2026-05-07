Smoke-test the project-level max depth override that was just added to meridian.toml:

[defaults]
max_depth = 4

Goals:
- Verify `meridian config show` reports `defaults.max_depth: 4 [source: file]` from the project config.
- Verify a spawn launched from this project sees the same configured max depth.
- If practical and safe, verify nesting behavior reflects max_depth=4 enough to prove this is not just primary-process display.
- Do not modify source files. Do not revert/stash/delete anything.
- Browser testing is only applicable if this setting affects a browser/web surface; otherwise explicitly report "browser not applicable" and why.

Return concise PASS/FAIL with commands run and evidence.
