# harness/extractors/ — Attempt Facts

One stateless extractor per harness. `create_fold()` makes an attempt-local
fold with its own cursor and bounded facts. Calling it with a `RawHarnessEvent`
updates facts synchronously before event persistence. Retries use fresh folds. Extractors
never read runner history or artifact-store output. Claude `--print` is the
black-box exception: the runner folds its captured stdout after exit.

`detect_session_id_from_event` remains the live connection identity port.
Session IDs need owned protocol evidence: assistant prose and nested tool values
are not identity evidence. Facts do not rebind chats; `NativeRun` decides identity.

`read_native_turn(key, ids)` is the only fallback read: exact event-named replies
in the recorded native store, never the latest message in a conversation or an
ambient namespace. OpenCode V2 prefers this read over streamed text.

Access extractors through `get_harness_bundle().extractor`, after harness bootstrap.
Keep harness-specific parsing here; runners only carry facts and register folds.

→ [.context/CONTEXT.md](.context/CONTEXT.md) — precedence, ownership, usage semantics.
