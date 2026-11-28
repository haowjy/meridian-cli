# Context

What a session's environment says about where it runs. `resolved.py` (`ResolvedContext`)
turns `MERIDIAN_*` inputs into the project root, runtime root, spawn and chat, work item and
named context directories, and composes the same variables for a child; `runtime.py`
(`RuntimeContext`) is its frozen projection for commands. `resolver.py` resolves the named
context paths (the KB, work directories).

Read resolution does not materialize identity or fall back to repo-local paths;
ad-hoc directories can be outside the user's control. Git-backed paths name the
expected clone without cloning it.
