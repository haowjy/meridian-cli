"""Synthetic journal fixtures; no native files, owners, adapters or model calls."""

from pathlib import Path

from meridian.lib.state import session_authority as a


def append(root: Path, event: a.JournalEvent) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "sessions.jsonl").open("a") as handle:
        handle.write(event.model_dump_json(exclude_none=False) + "\n")


def pinned(root: Path, chat: str = "c1", store: str = "/synthetic/native"):
    begin = a.BeginEventV4(
        run_id=chat,
        attempt_id=chat,
        transport_scope_id=chat,
        harness="pi",
        store=store,
        operation="fresh",
        attempt_number=1,
    )
    fact = a.BoundaryFactV4(
        run_id=chat,
        attempt_id=chat,
        boundary="entry",
        key=a.NativeSessionKey(harness="pi", store=store, native_session_id="conversation"),
        evidence=a.BoundaryEvidence(
            transport_scope_id=chat,
            order=1,
            correlation="entry",
            selection=a.CreatedSelection(creation_request="fresh"),
        ),
        file=a.QualifiedLocalFile(
            kind="local_file",
            path=store + "/conversation.jsonl",
            store_object={"device": 1, "inode": 10},
            file_object={"device": 1, "inode": 11},
            rule="pi-session-file:v1",
        ),
    )
    builder = a._JournalBuilder()
    a.fold_row(builder, begin)
    transition = a.plan_attempt(
        builder.attempt_view(), builder.identity(), fact, assigned_chat=chat
    )
    assert isinstance(transition, a.AttemptTransition) and transition.row is not None
    append(root, begin)
    append(root, transition.row)
    source = a.RecordedNativeSource(
        ref=a.NativeSourceRef(
            chat_id=chat,
            binding_event_id=a.boundary_digest_v4(fact),
            locator_event_id=a.boundary_digest_v4(fact),
        ),
        key=fact.key,
        locator=fact.file,
    )
    start = a.SessionStartEvent(
        chat_id=chat,
        kind="primary",
        harness="pi",
        harness_session_id="conversation",
        model="policy",
        session_instance_id="generation",
        started_at="now",
        spawn_id="p1",
    )
    append(root, start)
    event = a.SourceModelSelectionEvent(
        kind="invocation_started",
        harness="pi",
        harness_session_id="conversation",
        chat_id=chat,
        session_instance_id="generation",
        spawn_id="p1",
        startup_attempt_id="startup",
        recorded_at="now",
        source=source,
        selection=a.ConversationModelSelection(
            requested_token="model",
            selected_token="model",
            canonical_model_id="model",
            harness_model_id="model",
            model_mode="named",
            selection_source="recorded_selection",
            provenance={"source": "fixture"},
        ),
    )
    return source, start, event
