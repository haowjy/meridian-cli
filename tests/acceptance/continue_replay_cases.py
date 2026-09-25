"""C1 + C2 + C3 acceptance in a pre-import, permanently guarded interpreter."""

from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.harness import model_observation as native
from meridian.lib.launch import continue_model_intent as collector
from meridian.lib.launch.continue_replay import (
    ContinueReplayIntent,
    ContinueReplayRefused,
    ContinueReplaySource,
    EligibleContinueObservation,
    build_continue_replay_contract,
    select_continue_replay_intent,
)
from meridian.lib.ops.reference import (
    AuthorizedSourceMetadata,
    AuthorizedSourceUse,
    resolve_authorized_source_metadata,
)
from meridian.lib.state import session_authority as a
from meridian.lib.state import session_store as s
from meridian.lib.state import spawn_store
from tests.support.session_authority import pinned


def fixture(root, *, seed=False, default=False, absent=False, legacy=False):
    source, _, event = pinned(root)
    if default:
        event = event.model_copy(
            update={
                "selection": a.ConversationModelSelection(
                    requested_token="historical-request",
                    selected_token="historical-selected",
                    model_mode="harness_default",
                    selection_source="initial_launch",
                    provenance={"diagnostic": "retained"},
                )
            }
        )
    if seed:
        event = event.model_copy(update={"kind": "initial_seed", "startup_attempt_id": None})
    if legacy:
        payload = event.model_dump()
        payload.pop("source")
        event = a.SessionModelSelectionEvent.model_validate({**payload, "v": 1})
    if not absent:
        assert s.record_model_selection(root, event)
    # Synthetic downstream admission only. A v1 lifecycle plus v4 pin is not
    # production entry qualification (the public resolver remains fail-closed).
    authority = s.read_native_source_use_snapshot(root)
    assert isinstance(authority, s.NativeSourceUseSnapshot)
    admitted = AuthorizedSourceUse("resume", "c1", source, root, authority)
    snapshot = LaunchPolicySnapshot(model="never-snapshot-fallback", harness="pi")
    row = spawn_store.SpawnRecord(
        id="p1",
        chat_id="c1",
        session_instance_id="generation",
        harness="pi",
        harness_session_id="conversation",
        launch_policy_snapshot=snapshot,
    )
    with patch.object(spawn_store, "get_spawn", return_value=row) as read:
        metadata = resolve_authorized_source_metadata(admitted)
    assert read.call_args.kwargs == {"include_prompt": False}
    assert isinstance(metadata, AuthorizedSourceMetadata)
    assert source == admitted.source
    return admitted, metadata


def replay_source(admitted, metadata):
    return ContinueReplaySource(
        source_ref=admitted.original_ref,
        harness_session_id=admitted.source.key.native_session_id,
        harness="pi",
        source_chat_id="c1",
        source_work_id="work",
        source_execution_cwd="/task",
        source_control_root=None,
        source_claude_config_dir=None,
        source_pi_session_dir=None,
        source_launch_policy_snapshot=metadata.launch_policy_snapshot,
        tracked=True,
        recorded_native_source=admitted.source,
    )


def observation(source, provider="openai", model="gpt-5.4-mini"):
    return a.ExactModelObservation(
        source=source,
        provider_contract="pi-0.87.1-legacy-v3-settings-v1",
        view_basis="reopen-default",
        selected_leaf_id="leaf",
        model_basis="selected_reopen_default",
        model_token=model,
        native_provider=provider,
        evidence_entry_id="selected-without-turn",
        byte_length=100,
        content_sha256="a" * 64,
        file_object=source.locator.file_object,
        store_object=source.locator.store_object,
        observed_at="synthetic",
    )


def run(root: Path) -> None:
    # Both original components coexist in this interpreter; C1 standalone also
    # retains its stronger layer-import denial in its own existing acceptance.
    from tests.acceptance.session_model_intent_cases import run as run_c1

    run_c1(root / "c1")
    print("combined C1 import and acceptance passed")
    named = fixture(root / "named")
    seeded = fixture(root / "seed", seed=True)
    defaults = fixture(root / "default", default=True)
    seed_default = fixture(root / "default-seed", seed=True, default=True)
    absent = fixture(root / "absent", absent=True)
    legacy = fixture(root / "legacy", legacy=True)
    wrong = fixture(root / "wrong")
    admitted, metadata = named
    calls = Counter()

    def forbidden(name):
        def fail(*args, **kwargs):
            calls[name] += 1
            raise AssertionError("forbidden effect: " + name)

        return fail

    def collect(pair, evidence=None, override=None, view="reopen-default"):
        source, meta = pair
        before = calls["exact"]

        def reader(received, *, view):
            calls["exact"] += 1
            assert received == source.source
            assert view == "reopen-default"
            return evidence if evidence is not None else a.ModelEvidenceUnavailable("no_model")

        result = collector._collect_exact_continue_intent(
            source,
            meta,
            requested_model_override=override,
            view=view,
            reader=reader,
        )
        expected_reads = int(
            override is None
            and view == "reopen-default"
            and meta.admitted == source
            and source.operation == "resume"
            and source.source.key.harness == "pi"
            and not isinstance(
                source.authority.replay_model_facts(source.source), a.FactsUnavailable
            )
        )
        assert calls["exact"] - before == expected_reads
        return result

    with ExitStack() as stack:
        for module, names in (
            (
                collector,
                (
                    "run_mars_models_resolve",
                    "read_last_executed_model",
                    "get_model_selection",
                    "get_initial_model_selection",
                    "get_last_executed_model",
                    "record_model_observation",
                    "utc_now_iso",
                ),
            ),
            (
                s,
                (
                    "read_native_source_use_snapshot",
                    "get_model_selection",
                    "get_initial_model_selection",
                    "get_last_executed_model",
                    "record_model_selection",
                    "record_model_observation",
                    "read_journal",
                ),
            ),
            (spawn_store, ("get_spawn",)),
        ):
            for name in names:
                stack.enter_context(patch.object(module, name, forbidden(name)))

        # Selected setting is never a route: variants/spelling/nested IDs all
        # fall back identically, retaining the exact evidence and warning.
        for provider, model in (
            ("openai", "gpt-5.4-mini"),
            ("openai-codex", "gpt-5.4-mini"),
            ("openai", "gpt-5-4-mini"),
            ("p", "nested/model"),
        ):
            evidence = observation(admitted.source, provider, model)
            for pair in (named, seeded, defaults, seed_default):
                result = collect(pair, replace(evidence, source=pair[0].source))
                assert isinstance(result, collector._ExactContinueCollection)
                assert result.disposition == "pair_projection_unproven"
                assert result.evidence.native_provider == provider
                assert result.warnings[0].code == "pair_projection_unproven"
                assert "different provider or model" in result.warnings[0].message
                selection = result.intent.selection
                if pair in (defaults, seed_default):
                    assert selection.model_mode == "harness_default"
                    assert selection.requested_token is selection.selected_token is None
                    assert selection.routing_token is selection.mars_model is None
                    assert selection.literal_model
                else:
                    assert selection.routing_token == "model"
                assert result.intent.initial_model_selection is None
                contract = build_continue_replay_contract(
                    source=replay_source(*pair),
                    intent=result.intent,
                )
                assert contract.session.recorded_native_source == pair[0].source
                assert contract.session.conversation_intent == selection
                assert contract.task_dir == "/task" and contract.work_id == "work"
                # Neither returned selection nor contract can mutate C1 facts.
                contract.session.conversation_intent.provenance["diagnostic"] = "mutated"
                selection.provenance["diagnostic"] = "mutated-again"
                repeated = collect(pair, replace(evidence, source=pair[0].source))
                assert repeated.intent.selection.provenance.get("diagnostic") != "mutated-again"

        for override in ("explicit", "", "   "):
            result = collect(absent, override=override)
            assert result.intent.selection.requested_token == override
            assert result.intent.selection.selection_source == "explicit_override"
            assert result.evidence is None and result.warnings == ()
        for pair in (absent, legacy):
            result = collect(pair, observation(pair[0].source))
            assert isinstance(result, ContinueReplayRefused) and result.reason == "missing_intent"
            assert "explicit --model" in result.message and "pair-preserving" in result.message
            assert collect(pair).reason == "missing_intent"
        for reason in ("missing", "inaccessible", "incomplete", "no_model", "changed_during_read"):
            assert (
                collect(named, a.ModelEvidenceUnavailable(reason)).intent.selection.routing_token
                == "model"
            )
        assert collect(defaults).intent.selection.routing_token is None
        for reason in ("unsupported_provider", "unsupported_view", "unsupported_dialect"):
            assert isinstance(
                collect(named, a.ModelEvidenceUnavailable(reason)), ContinueReplayRefused
            )
        for evidence in (
            a.ModelSourceConflict("store_changed"),
            replace(
                observation(admitted.source),
                source=admitted.source.model_copy(
                    update={"key": admitted.source.key.model_copy(update={"store": "/wrong"})}
                ),
            ),
            replace(observation(admitted.source), view_basis="process-active"),
            replace(observation(admitted.source), model_basis="executed"),
            replace(observation(admitted.source), complete=False),
            replace(observation(admitted.source), provider_contract="other-contract"),
        ):
            assert isinstance(collect(named, evidence), ContinueReplayRefused)
        for override in (None, "explicit"):
            assert (
                collect(named, override=override, view="process-active").reason
                == "unsupported_view"
            )
            assert collect((admitted, wrong[1]), override=override).reason == "metadata_mismatch"
            fork = replace(admitted, operation="fork")
            assert (
                collect((fork, replace(metadata, admitted=fork)), override=override).reason
                == "unsupported_view"
            )
            changed = replace(
                admitted,
                source=admitted.source.model_copy(
                    update={"key": admitted.source.key.model_copy(update={"store": "/wrong"})}
                ),
            )
            assert (
                collect((changed, replace(metadata, admitted=changed)), override=override).reason
                == "source_conflict"
            )

        # Pure eligible-slot fixture is not a Pi managed producer or permission.
        facts = admitted.authority.replay_model_facts(admitted.source)
        evidence = observation(admitted.source)
        choice = select_continue_replay_intent(
            operation="resume",
            exact_facts=facts,
            recorded_native_source=admitted.source,
            eligible_observation=EligibleContinueObservation(
                a.ConversationModelSelection(
                    requested_token="eligible", selection_source="unknown"
                ),
                evidence,
            ),
        )
        assert isinstance(choice, ContinueReplayIntent)
        assert choice.selection.routing_token == "eligible"
        assert choice.selection.provenance["model_basis"] == "selected_reopen_default"
        source = replay_source(admitted, metadata)
        contract = build_continue_replay_contract(source=source, intent=choice)
        assert contract.session.conversation_intent.provenance["native_provider"] == "openai"
        for changed in (
            replace(source, harness_session_id="other"),
            replace(source, source_ref="c999"),
            replace(source, source_chat_id="other"),
            replace(
                source,
                recorded_native_source=wrong[0].source.model_copy(
                    update={"key": wrong[0].source.key.model_copy(update={"store": "/wrong"})}
                ),
            ),
        ):
            try:
                build_continue_replay_contract(source=changed, intent=choice)
            except ValueError:
                pass
            else:
                raise AssertionError("builder accepted alternate source")
        assert isinstance(
            select_continue_replay_intent(
                operation="resume",
                exact_facts=facts,
                recorded_native_source=admitted.source,
                fallback=a.ConversationModelSelection(
                    requested_token="snapshot", selection_source="initial_launch"
                ),
            ),
            ContinueReplayRefused,
        )
        # C2 dispatch is also imported and exercised; it may not discover a source.
        assert native.read_model_evidence_exact(
            admitted.source, view="process-active"
        ) == a.ModelEvidenceUnavailable("unsupported_view")
        combined_c2(admitted, metadata)
        assert not any(count for name, count in calls.items() if name != "exact"), calls
    legacy_entry_normalization(root / "untracked")
    print("exact budgets", dict(calls), "forbidden_effects", 0)
    print("pure, legacy-isolation, pair-fidelity, source/view and default contracts passed")


def legacy_entry_normalization(root):
    """Real primary/spawn callers keep distinct blank semantics with fake effects."""
    from meridian.lib import launch
    from meridian.lib.harness.registry import get_default_harness_registry
    from meridian.lib.launch.request import SessionRequest
    from meridian.lib.launch.types import LaunchRequest, SessionMode
    from meridian.lib.ops import reference
    from meridian.lib.ops.spawn import api as spawn_api
    from meridian.lib.ops.spawn.models import SpawnContinueInput

    calls = Counter()
    resolved = reference.ResolvedSessionReference(
        harness_session_id="native",
        harness="pi",
        source_chat_id=None,
        source_model="fallback",
        source_agent=None,
        source_skills=(),
        source_work_id=None,
        tracked=False,
    )
    legacy_source = ContinueReplaySource(
        source_ref="native",
        harness_session_id="native",
        harness="pi",
        source_chat_id=None,
        source_work_id=None,
        source_execution_cwd=None,
        source_control_root=None,
        source_claude_config_dir=None,
        source_pi_session_dir=None,
        source_launch_policy_snapshot=None,
        tracked=False,
        source_model="fallback",
    )
    seed = a.SessionModelSelectionEvent(
        kind="initial_seed",
        harness="pi",
        harness_session_id="native",
        chat_id="legacy",
        session_instance_id="g",
        spawn_id=None,
        startup_attempt_id=None,
        recorded_at="old",
        selection=a.ConversationModelSelection(
            requested_token="request",
            selected_token="selected",
            canonical_model_id="canonical",
            selection_source="initial_launch",
        ),
    )

    def observe(*args, **kwargs):
        calls["legacy_native"] += 1
        return "observed"

    def route(*args, **kwargs):
        calls["legacy_mars"] += 1
        return {"runnable_paths": [{"harness": "pi"}]}

    def write(*args, **kwargs):
        calls["legacy_write"] += 1
        return True

    with (
        patch.object(collector, "get_model_selection", return_value=None),
        patch.object(collector, "get_initial_model_selection", return_value=seed),
        patch.object(collector, "read_last_executed_model", side_effect=observe),
        patch.object(collector, "run_mars_models_resolve", side_effect=route),
        patch.object(collector, "record_model_observation", side_effect=write),
        patch.object(collector, "utc_now_iso", return_value="synthetic"),
        patch.object(native, "read_model_evidence_exact", side_effect=AssertionError("legacy C2")),
        patch.object(reference, "resolve_session_reference", return_value=resolved),
    ):
        for supplied in (None, "", "  ", "chosen"):
            before = calls.copy()
            request = launch._resolve_primary_source_request(
                request=LaunchRequest(
                    model=supplied,
                    harness="pi",
                    session_mode=SessionMode.RESUME,
                    session=SessionRequest(continue_source_ref="native"),
                ),
                project_root=root,
                harness_registry=get_default_harness_registry(),
                authorized_source=reference.UntrackedSourceUse(
                    "resume", "native", "native", "pi", root
                ),
            )
            assert request.model == ("chosen" if supplied == "chosen" else "observed")
            assert calls["legacy_native"] - before["legacy_native"] == int(supplied != "chosen")
            before = calls.copy()
            request = spawn_api._build_continue_create_input(
                payload=SpawnContinueInput(spawn_id="native", model=supplied, prompt="follow up"),
                source_spawn=spawn_store.SpawnRecord(id="p1", harness="pi"),
                source_spawn_id="native",
                resolved_reference=resolved,
                runtime_root=root,
            )
            assert request.model == ("observed" if supplied is None else supplied)
            assert calls["legacy_native"] - before["legacy_native"] == int(supplied is None)
        # No native/route/write effect for fork; legacy snapshot inheritance wins.
        before = calls.copy()
        fork = collector.collect_untracked_legacy_continue_intent(
            source=legacy_source,
            fork=True,
            runtime_root=root,
        )
        assert fork.selection.routing_token == "fallback" and calls == before
        with (
            patch.object(collector, "read_last_executed_model", return_value=None),
            patch.object(collector, "get_last_executed_model", return_value=None),
        ):
            recovered = collector.collect_untracked_legacy_continue_intent(
                source=legacy_source,
                runtime_root=root,
            )
            assert recovered.selection.routing_token == "canonical"
            assert recovered.initial_model_selection == seed
            overridden = collector.collect_untracked_legacy_continue_intent(
                source=legacy_source,
                requested_model_override="",
                runtime_root=root,
            )
            assert overridden.selection.selection_source == "explicit_override"
            assert overridden.initial_model_selection == seed
    print("legacy entry budgets", dict(calls), "exact_reads", 0)


def combined_c2(admitted, metadata):
    """Real C2 settings fold over fake exact content; no native store discovery."""
    import hashlib
    import json

    from meridian.lib.harness.pi_native_source import PiExactContent, PiExactValidatedContent

    rows = [
        {"type": "session", "version": 3, "id": "conversation", "cwd": "/synthetic"},
        {"type": "model_change", "id": "a", "parentId": None, "provider": "p1", "modelId": "m1"},
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "message": {
                "role": "assistant",
                "provider": "p2",
                "model": "m2",
                "content": [],
            },
        },
        {"type": "model_change", "id": "c", "parentId": "b", "provider": "p3", "modelId": "m3"},
        {
            "type": "model_change",
            "id": "off",
            "parentId": "a",
            "provider": "wrong",
            "modelId": "wrong",
        },
        {"type": "custom", "id": "leaf", "parentId": "c"},
    ]
    reads = 0

    def content_reader(source, *, validate):
        nonlocal reads
        reads += 1
        assert source == admitted.source
        data = b"\n".join(json.dumps(row).encode() for row in rows)
        content = PiExactContent(
            data,
            len(data),
            hashlib.sha256(data).hexdigest(),
            source.locator.file_object,
            source.locator.store_object,
        )
        return PiExactValidatedContent(content, validate(content))

    with patch.object(native, "read_pi_exact_content", side_effect=content_reader):
        result = collector._collect_exact_continue_intent(
            admitted,
            metadata,
            requested_model_override=None,
            view="reopen-default",
            reader=native.read_model_evidence_exact,
        )
        assert reads == 1
        assert result.evidence.model_token == "m3" and result.evidence.native_provider == "p3"
        assert result.intent.selection.routing_token == "model"
        assert result.disposition == "pair_projection_unproven"
        rows.append(
            {
                "type": "message",
                "id": "later",
                "parentId": "leaf",
                "message": {
                    "role": "assistant",
                    "provider": "p4",
                    "model": "m4",
                    "content": [],
                },
            }
        )
        result = collector._collect_exact_continue_intent(
            admitted,
            metadata,
            requested_model_override=None,
            view="reopen-default",
            reader=native.read_model_evidence_exact,
        )
        assert reads == 2 and result.evidence.model_token == "m4"
        assert result.evidence.model_basis == "selected_reopen_default"
    print("combined C2 settings fold passed; bounded exact reads", reads)
