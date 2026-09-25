# harness/extractors/ — Attempt Facts

One extractor per harness. `fold(facts, event)` updates bounded, attempt-local
facts synchronously before event persistence. Retries use fresh facts. Extractors
never read runner history or artifact-store output. Claude `--print` is the
black-box exception: the runner folds its captured stdout after exit.

`detect_session_id_from_event` remains the live connection identity port.
Session IDs need owned protocol evidence: assistant prose and nested tool values
are not identity evidence. Facts do not rebind chats; `NativeRun` decides identity.

`read_native_turn(key, ids)` is the only fallback read: exact event-named replies
in the recorded native store, never the latest message in a conversation or an
ambient namespace. OpenCode V2 prefers this read over streamed text.

Access extractors through `get_harness_bundle().extractor`, after harness bootstrap.
Keep harness-specific parsing here; runners only carry facts and register hooks.

→ [.context/CONTEXT.md](.context/CONTEXT.md) — precedence, ownership, usage semantics.
