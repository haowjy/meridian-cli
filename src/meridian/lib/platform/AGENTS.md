# Platform

Use `python_module.module_argv` for package re-exec: plain `python -m` can run a
project's `meridian/` outside its sandbox. `-P` does not protect against empty
or relative `PYTHONPATH` entries.

Lock ordering and stable inode rules are in [../state/AGENTS.md](../state/AGENTS.md).
[.context/CONTEXT.md](.context/CONTEXT.md) describes the launch gate.
