# Operations

A spawn or chat ID asks `claim_root` first (`locate()`), so a claimed ID names its session from
any project; selectors, native IDs and legacy IDs look in the current project. A located
target carries its runtime root and project root (a row's `control_root`; that directory may
be gone, which only commands that work in it refuse). The caller's spawn ID is meaningful
only in its own root: anything keyed by "the caller" asks `RuntimeContext.caller_in(root)`
or `core/caller.py` and gets none for another root, so no observation, pin or owed result
is written there. `-C` into another project still removes the caller's project-scoped
variables for the whole invocation (`cli/main.py`); `--continue` of a claimed ID in another
project re-runs the command under `-C` (`cli/continue_reentry.py`) rather than trying to
keep the caller's identity out of a launch.

Work attachment publishes item links before session updates. Holding the work
and session locks together deadlocks against restore; done and reopen move the
item while its links carry attached chats to the new location.

Relocation's old-root candidates come from the harness record and control root,
not task or execution cwd. A worktree chat is pinned there intentionally;
treating its task cwd as the old project would pull it out of the worktree.
Only offer a runnable relocate command when both endpoints are known.

A chat transcript uses its recorded native binding. A missing binding or source
is unavailable, not permission to substitute a different transcript or runner
output. KB `state/native-session-binding.md` explains the identity choice.

A session-log entry is the user's text, an injected message, or one agent step: its text,
tool calls and results. Only text the user typed has role `user`; a user-role message a
plugin, hook, scheduler, task notification or peer wrote gets role `injected` (Claude:
`origin.kind` other than `human`, or `isMeta`; Pi `custom_message`; Codex's injected
context; OpenCode `synthetic` text parts). Text Meridian injects itself (peer messages,
idle nudges) carries the `[Meridian]` marker from `harness/injected_marker.py`, which every
producer builds with and the shared normalizer reads for every harness; the module also
lists the opening of the pre-marker nudges so older transcripts classify. A new producer of
user-turn text must use it. Claude's `isCompactSummary` message is the segment's entry-0
handoff, not a user entry. Tool traffic is a message with a call or a result, whatever role the harness
stored it under; each adapter sets that where it parses (`harness/transcript.py`), the
renderer never sniffs text. Views choose parts: the default shows user and assistant
text, `--tools` adds tool traffic and injected messages. Entry ordinals come from the full transcript and
hidden entries leave gaps, so a printed position opens in any view, and window
selection (`session_render.py`) works on the shown entries by ordinal. Changing
grouping or what the parsers emit means bumping `PARSER_VERSION` in
`state/native_search_index.py`.
