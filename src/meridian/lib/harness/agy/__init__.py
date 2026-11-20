"""Meridian from native Antigravity (``agy``): hook, MCP server and their state.

Meridian never launches ``agy``. A project plugin (``src/meridian/mars/``, installed by
``meridian mars sync``) runs ``meridian harness-hook agy <event>`` from ``agy``'s hooks and
starts ``meridian harness-mcp agy`` as its one MCP server. See ``harness/AGENTS.md``.

State lives under ``<meridian home>/agy/``:

- ``<pid>-<birth>/requests/<conversation>.json``: one request per conversation, written
  by the hook. It is also the shell guard's "already seen" marker.
- ``<pid>-<birth>/rows/<conversation>.json``: the spawn and chat the server holds for it.
- ``inbox/<conversation>.jsonl``, ``inflight/<conversation>.jsonl``: queued and
  delivered-but-unconfirmed messages (see ``inbox.py``).

``mars/hooks/meridian-session/guard.sh`` computes the same paths, so a
change to the layout changes both.
"""
