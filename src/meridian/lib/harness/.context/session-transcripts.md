# lib/harness/ — Session Transcripts

`transcript.py` is the cross-harness read path for session data. It is independent of
the spawn/write paths and reads native JSONL or OpenCode SQLite/legacy JSON
storage. Runner `history.jsonl` is rejected as "not a native transcript", including
explicit `--file` reads. There is no runner-history decoder or fallback.

Before native-provider dispatch, the reader recognizes a sealed Meridian native
snapshot by its bounded storage header (including renamed explicit files), or by
the reserved `native-transcript.jsonl` name. State's snapshot codec unwraps and
validates it; native dictionaries still use the existing normalizer. Storage
headers/seals never become conversational events. A shared `TranscriptValidation`
stays partial until verified EOF, separately from `rendering_reason`; search
withholds unverified matches. Capture qualification: `transcript_capture.py` streams, hashes, and records
status (complete, known-incomplete, unavailable, or unsupported);
`capture_qualify.py` observers own dialect tails. Only complete observations are sealed into
`native-transcript.jsonl`. JSONL capture keeps raw lines; OpenCode keeps versioned
raw-row envelopes. Unfinished dialect tails refuse publication.
Resolved native files are distinct from explicit file inputs: their known native
session/harness binding is checked when a storage header is encountered. Snapshots
never use append checkpoints, even when their enclosing target is spawn-owned.

Pi native `message` and RPC `message_end` share message extraction. Native
compactions create segments with recorded summaries; branch summaries and parent
changes are typed annotations in append order, not reconstructed active context.
Unknown material records/content set `rendering_reason`; consumers must not call
that a complete empty rendering. Raw storage remains unchanged.

OpenCode session existence and the complete row read share one read-only
transaction. Missing sources and database errors must not become empty iteration;
a present DB session wins over legacy JSON even when empty.

Only the shared normalizer translates these records. It preserves initial-user
setup and summary compactions; unsupported roles,
parts and malformed material retain an explicit rendering reason. Rendering
support is not native capture qualification: a consistent DB transaction can
still contain unfinished work. Do not use normalized display events as capture
input or treat iterator creation as completed source validation.
