"""Meridian from native Antigravity (``agy``): hook, MCP server and their state.

Meridian never runs ``agy``: ``meridian agy`` builds the launch and prints the command for
the person to run (``agy/adapter.py``, ``lib/harness/printed.py``). A project plugin
(``src/meridian/mars/``, installed by ``meridian mars sync``) runs
``meridian harness-hook agy <event>`` from ``agy``'s hooks and starts
``meridian harness-mcp agy`` as its one MCP server. See ``harness/AGENTS.md``.

State lives under ``<meridian home>/agy/``:

- ``<pid>-<birth>/requests/<conversation>.json``: one request per conversation, written
  by the hook. It is also the shell guard's "already seen" marker. It carries the agent,
  launch directory (as ``cwd``) and approval the printed command set (``_MERIDIAN_AGY_AGENT``,
  ``_MERIDIAN_AGY_PROJECT``, ``_MERIDIAN_AGY_APPROVAL``), which
  ``holds.py`` writes into the row's launch snapshot so a continue replays them.
- ``launch/<id>.sh``: a printed command too long for a shell line.
- ``<pid>-<birth>/rows/<conversation>.json``: the spawn and chat the server holds for it.
- ``<pid>-<birth>/briefed/<conversation>``: an empty file, written once the hook has
  dealt with the facts of the conversation's row. The shell guard reads it.
- ``<pid>-<birth>/seen/<conversation>``: ``<offset> <transcript path>``, how far the hook
  has read the transcript. The shell guard greps the bytes after it for a compaction.
- ``inbox/<conversation>.jsonl``, ``inflight/<conversation>.jsonl``: queued and
  sent-but-unconfirmed messages (see ``inbox.py``).

``mars/hooks/meridian-session/guard.sh`` computes the same paths, so a
change to the layout changes both.
"""
