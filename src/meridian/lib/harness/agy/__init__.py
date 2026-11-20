"""Meridian from native Antigravity (``agy``): hook, MCP server and their state.

Meridian never launches ``agy``. Jimmy's ``agy`` loads a project plugin whose hooks run
``meridian harness-hook agy <event>`` and whose one MCP server is
``meridian harness-mcp agy``. Nothing here is registered as a harness yet: the R6
adapter split lands the ``HarnessId`` entry and adapter next to these modules, and the
server then maps the root conversations it records (``state.record_call``) to spawns.

State lives under ``<meridian home>/agy/``:

- ``<pid>-<birth>/requests/<conversation>.json``: one request per conversation, written
  by the hook. It is also the shell guard's "already seen" marker.
- ``<pid>-<birth>/calls/<root conversation>.json``: written by the MCP server per call.
- ``inbox/<conversation>.jsonl``, ``inflight/<conversation>.jsonl``: queued and
  delivered-but-unconfirmed messages (see ``inbox.py``).

``mars/hooks/meridian-session/guard.sh`` computes the same paths, so a
change to the layout changes both.
"""
