# Claude session observations

This classic command-hook plugin appends Claude `SessionStart` identity facts to
a launcher-owned journal. It loads for primary and spawned Claude launches and
does nothing outside a prepared Meridian launch.

The hook records the launch nonce, parent PID, record epoch (`recorded_at`),
SessionStart source, native session ID, exact transcript path, and (when Claude
publishes it) an absolute `messaging_socket` path that fits a Unix socket address (107 bytes, 103 on macOS).
Each record is one bounded JSONL line
written with one `O_APPEND` write to a mode-0600 file. The stdlib-only writer
never imports Meridian, starts a subprocess, creates a missing parent directory,
or emits conversation output.

The hook deliberately does not read or record Claude's messaging token. Meridian
is not Claude's child process, so presenting that token would incorrectly attest
child ownership and could bypass Claude's inbound-message controls. Inbox posts
use the socket without authentication and remain subject to Claude's own
`crossSessionInbound` policy.

The launcher starts a per-run drainer after it knows the actual Claude PID. The
Claude adapter rejects records with a foreign nonce or PID and decodes only exact
native paths. `NativeRun` serializes those observations with transport identity:
an eligible initial fact can complete the immutable entry, while session switches
create or reuse chats and durably advance the current-chat pointer in journal
order. A later `/resume` is not mistaken for startup merely because the initial
hook was missed. An overlap with another live owner is recorded and warned about,
not denied after the native switch has already happened.

The journal is observation evidence, not a transcript. A missing transcript can
still make a later read or resume fail exact validation. Last-observed identity
is not final-exit proof; SessionEnd is deliberately not used. If the launcher is
hard-killed, facts after its last successful drain may remain unobserved. The
journal is capped at 1 MiB; once full, no further observations are emitted. The
last accepted pointer remains available, but later switches cannot be inferred.

Journal files are `claude-session-boundary.jsonl` under the spawn artifact
directory. `claude plugin validate <plugin-dir>` checks the manifest. Python
integration tests exercise the actual recorder subprocess and launcher wiring;
runtime qualification should additionally use real Claude in an isolated home.
