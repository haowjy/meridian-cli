# Operations

Work attachment publishes item links before session updates. Holding the work
and session locks together deadlocks against restore; done and reopen move the
item while its links carry attached chats to the new location.

Relocation's old-root candidates come from the harness record and control root,
not task or execution cwd. A worktree chat is pinned there intentionally;
treating its task cwd as the old project would pull it out of the worktree.
Only offer a runnable relocate command when both endpoints are known.

A chat transcript uses its recorded native binding. A missing binding or source
is unavailable, not permission to substitute a different transcript or runner
output. KB `decisions/native-session-identity.md` explains the identity choice.
