"""Synthetic exact Pi model evidence; never touches a user/native store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import meridian.lib.harness.model_observation as model_observation
import meridian.lib.harness.pi_native_source as pi_source
from meridian.lib.harness.model_observation import (
    read_model_evidence_exact,
    )
from meridian.lib.harness.pi_native_source import PiSourceQualified, qualify_pi_source
from meridian.lib.state.session_authority import (
    ExactModelObservation,
    ModelEvidenceUnavailable,
    ModelSourceConflict,
    NativeSessionKey,
    NativeSourceRef,
    RecordedNativeSource,
)


def _entry(kind: str, ident: str, parent: str | None, **fields: object) -> dict[str, object]:
    return {"type": kind, "id": ident, "parentId": parent, **fields}


def _source(
    store: Path, rows: list[dict[str, object]], *, file_name: str = "session.jsonl"
) -> tuple[RecordedNativeSource, Path]:
    path = store / file_name
    store.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n".join(json.dumps(row).encode() for row in rows))
    qualified = qualify_pi_source(
        effective_store=store, session_id="same-id", session_file=str(path)
    )
    assert isinstance(qualified, PiSourceQualified)
    return RecordedNativeSource(
        ref=NativeSourceRef(chat_id="chat-a", binding_event_id="a" * 64, locator_event_id="b" * 64),
        key=NativeSessionKey(harness="pi", store=str(store), native_session_id="same-id"),
        locator=qualified.observation,
    ), path


def _header() -> dict[str, object]:
    return {"type": "session", "version": 3, "id": "same-id", "cwd": "/synthetic"}


def test_exact_reopen_setting_folds_model_change_and_assistant_in_lineage_order(
    tmp_path: Path,
) -> None:
    rows = [
        _header(),
        _entry("model_change", "a", None, provider="p1", modelId="m1"),
        _entry(
            "message",
            "b",
            "a",
            message={
                "role": "assistant",
                "provider": "p2",
                "model": "m2",
                "stopReason": "aborted",
                "content": [],
            },
        ),
        _entry("model_change", "c", "b", provider="p3", modelId="m3"),
        _entry(
            "message",
            "off-branch",
            "a",
            message={"role": "assistant", "provider": "wrong", "model": "wrong"},
        ),
        _entry("custom", "leaf", "c"),
    ]
    source, _ = _source(tmp_path / "store", rows)

    result = read_model_evidence_exact(source)

    assert isinstance(result, ExactModelObservation)
    assert (result.native_provider, result.model_token, result.evidence_entry_id) == (
        "p3",
        "m3",
        "c",
    )
    assert result.model_basis == "selected_reopen_default"
    assert result.complete and result.byte_length > 0 and len(result.content_sha256) == 64


@pytest.mark.parametrize("tail", [b"\n{torn", b'\n{"type":"future","id":"x","parentId":"a"}'])
def test_torn_or_unknown_lineage_never_returns_older_positive(tmp_path: Path, tail: bytes) -> None:
    rows = [_header(), _entry("model_change", "a", None, provider="p", modelId="m")]
    source, path = _source(tmp_path / "store", rows)
    path.write_bytes(path.read_bytes() + tail)
    source = source.model_copy(
        update={
            "locator": qualify_pi_source(
                effective_store=tmp_path / "store", session_id="same-id", session_file=str(path)
            ).observation
        }
    )  # type: ignore[union-attr]

    assert read_model_evidence_exact(source) == ModelEvidenceUnavailable("incomplete")


def test_exact_reader_handles_complete_final_row_without_newline_and_no_model(
    tmp_path: Path,
) -> None:
    rows = [
        _header(),
        _entry("message", "a", None, message={"role": "assistant", "provider": "p", "model": "m"}),
    ]
    source, _ = _source(tmp_path / "store", rows)
    assert isinstance(read_model_evidence_exact(source), ExactModelObservation)
    no_model, _ = _source(
        tmp_path / "empty", [_header(), _entry("message", "a", None, message={"role": "user"})]
    )
    assert read_model_evidence_exact(no_model) == ModelEvidenceUnavailable("no_model")


@pytest.mark.parametrize("mutation", ["append", "truncate", "swap", "delete"])
def test_completion_reobserves_namespace_and_descriptor_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    source, path = _source(
        tmp_path / "store",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="m")],
    )
    original = pi_source._reobserve_namespace

    def mutate(*args: object) -> str | None:
        if mutation == "append":
            with path.open("ab") as handle:
                handle.write(b"\n{}")
        elif mutation == "truncate":
            path.write_bytes(b"")
        elif mutation == "swap":
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
        else:
            path.unlink()
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(pi_source, "_reobserve_namespace", mutate)
    assert isinstance(read_model_evidence_exact(source), ModelSourceConflict)


@pytest.mark.parametrize("tail", [b"\n{torn", b"\n{\xff"])
def test_known_header_identity_conflict_precedes_bad_tail(tmp_path: Path, tail: bytes) -> None:
    source, path = _source(
        tmp_path / "store",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="m")],
    )
    path.write_bytes(path.read_bytes().replace(b"same-id", b"wrong-id") + tail)
    assert read_model_evidence_exact(source) == ModelSourceConflict("identity_mismatch")


def test_known_header_identity_conflict_precedes_unsupported_dialect(tmp_path: Path) -> None:
    source, path = _source(
        tmp_path / "store",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="m")],
    )
    changed = path.read_bytes().replace(b"same-id", b"wrong-id").replace(
        b'"version": 3', b'"version": 99'
    )
    path.write_bytes(changed)
    assert read_model_evidence_exact(source) == ModelSourceConflict("identity_mismatch")


@pytest.mark.parametrize(
    "bad_row",
    [
        _entry("custom", "orphan", "missing"),
        _entry("custom", "cycle-a", "cycle-b"),
        _entry("custom", "duplicate", None),
    ],
)
def test_malformed_off_branch_graph_never_claims_complete(
    tmp_path: Path, bad_row: dict[str, object]
) -> None:
    rows = [
        _header(),
        _entry("model_change", "a", None, provider="p", modelId="m"),
        bad_row,
        *([_entry("custom", "cycle-b", "cycle-a")] if bad_row["id"] == "cycle-a" else []),
        _entry("custom", "duplicate", "a")
        if bad_row["id"] == "duplicate"
        else _entry("custom", "leaf", "a"),
    ]
    source, path = _source(tmp_path / "store", rows)
    if bad_row["id"] == "duplicate":
        path.write_bytes(path.read_bytes() + b"\n" + json.dumps(bad_row).encode())
        source = source.model_copy(
            update={
                "locator": qualify_pi_source(
                    effective_store=tmp_path / "store", session_id="same-id", session_file=str(path)
                ).observation
            }
        )  # type: ignore[union-attr]
    assert read_model_evidence_exact(source) == ModelEvidenceUnavailable("incomplete")


def test_exact_read_uses_single_content_horizon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source, path = _source(
        tmp_path / "store",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="m")],
    )
    original_read = os.read
    captured = 0

    def counted_read(fd: int, amount: int) -> bytes:
        nonlocal captured
        value = original_read(fd, amount)
        captured += len(value)
        return value

    monkeypatch.setattr(os, "read", counted_read)
    assert isinstance(read_model_evidence_exact(source), ExactModelObservation)
    assert captured == path.stat().st_size


def test_exact_source_never_falls_back_to_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source, _ = _source(
        tmp_path / "store",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="m")],
    )
    monkeypatch.setattr(
        model_observation,
        "resolve_pi_agent_dir",
        lambda **kwargs: pytest.fail("discovery"),
    )
    monkeypatch.setattr(
        model_observation,
        "resolve_pi_spawn_session_root",
        lambda **kwargs: pytest.fail("discovery"),
    )
    monkeypatch.setattr(
        model_observation,
        "_read_pi_last_model",
        lambda *args: pytest.fail("legacy reader"),
    )
    assert isinstance(read_model_evidence_exact(source), ExactModelObservation)


def test_later_assistant_overrides_model_change_and_missing_attribution_cannot_reuse_old(
    tmp_path: Path,
) -> None:
    source, _ = _source(
        tmp_path / "store",
        [
            _header(),
            _entry("model_change", "a", None, provider="p1", modelId="m1"),
            _entry(
                "message",
                "b",
                "a",
                message={
                    "role": "assistant",
                    "provider": "p2",
                    "model": "m2",
                    "stopReason": "error",
                },
            ),
        ],
    )
    selected = read_model_evidence_exact(source)
    assert isinstance(selected, ExactModelObservation)
    assert (selected.native_provider, selected.model_token) == ("p2", "m2")

    incomplete, _ = _source(
        tmp_path / "incomplete",
        [
            _header(),
            _entry("model_change", "a", None, provider="p1", modelId="m1"),
            _entry("message", "b", "a", message={"role": "assistant", "content": []}),
        ],
    )
    assert read_model_evidence_exact(incomplete) == ModelEvidenceUnavailable("incomplete")


def test_same_native_id_in_another_store_does_not_change_exact_source(tmp_path: Path) -> None:
    first, _ = _source(
        tmp_path / "store-a",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="first")],
    )
    _source(
        tmp_path / "store-b",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="other")],
    )

    result = read_model_evidence_exact(first)
    assert isinstance(result, ExactModelObservation)
    assert result.model_token == "first"


def test_exact_read_reports_one_content_pass_and_bounded_namespace_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    source, path = _source(
        tmp_path / "store",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="m")],
    )
    opens = 0
    bytes_read = 0
    real_open, real_read = os.open, os.read

    def counted_open(*args: object, **kwargs: object) -> int:
        nonlocal opens
        opens += 1
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    def counted_read(fd: int, amount: int) -> bytes:
        nonlocal bytes_read
        chunk = real_read(fd, amount)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(os, "open", counted_open)
    monkeypatch.setattr(os, "read", counted_read)
    result = read_model_evidence_exact(source)

    assert isinstance(result, ExactModelObservation)
    assert bytes_read == path.stat().st_size == result.byte_length
    # Both no-follow traversals open every root component; content opens once.
    assert opens == 2 * (len(path.parent.parts[1:]) + 1) + 2


def test_unsupported_harness_and_active_view_do_not_read_native_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _source(tmp_path / "store", [_header()])
    import meridian.lib.harness.model_observation as observations

    monkeypatch.setattr(
        observations, "read_pi_exact_content", lambda _: pytest.fail("opened unsupported source")
    )
    assert observations.read_model_evidence_exact(
        source.model_copy(update={"key": source.key.model_copy(update={"harness": "claude"})})
    ) == ModelEvidenceUnavailable("unsupported_provider")
    assert observations.read_model_evidence_exact(
        source, view="process-active"
    ) == ModelEvidenceUnavailable("unsupported_view")


def test_replaced_file_is_conflict_not_fallback(tmp_path: Path) -> None:
    source, path = _source(
        tmp_path / "store",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="m")],
    )
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)

    assert read_model_evidence_exact(source) == ModelSourceConflict("file_changed")


@pytest.mark.parametrize("mutation", ["append", "truncate"])
def test_append_or_truncate_during_content_read_cannot_return_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import os

    source, path = _source(
        tmp_path / "store",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="m")],
    )
    real_read = os.read
    mutated = False

    def mutate_after_read(fd: int, amount: int) -> bytes:
        nonlocal mutated
        chunk = real_read(fd, amount)
        if chunk and not mutated:
            mutated = True
            if mutation == "append":
                with path.open("ab") as handle:
                    handle.write(b"\n")
            else:
                path.write_bytes(path.read_bytes()[:-1])
        return chunk

    monkeypatch.setattr(os, "read", mutate_after_read)
    assert read_model_evidence_exact(source) == ModelEvidenceUnavailable("changed_during_read")


def test_symlink_and_fifo_replacements_are_never_read_as_content(tmp_path: Path) -> None:
    source, path = _source(
        tmp_path / "store",
        [_header(), _entry("model_change", "a", None, provider="p", modelId="m")],
    )
    other = path.with_name("other.jsonl")
    other.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(other)
    assert read_model_evidence_exact(source) == ModelSourceConflict("file_changed")

    path.unlink()
    import os

    os.mkfifo(path)
    assert read_model_evidence_exact(source) == ModelSourceConflict("file_changed")


def test_guarded_acceptance_runs_in_isolated_child() -> None:
    """Keep irreversible audit guards and isolated HOME out of pytest workers."""
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MERIDIAN", "_MERIDIAN", "PI_", "CODEX", "CLAUDE", "XDG", "MARS"))
        and key
        not in {"HOME", "MERIDIAN_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"}
    }
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[2] / "support" / "exact_model_evidence_guard.py"),
        ],
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (
        result.stdout.strip()
        == '{"accepted": true, "denied_self_tests": 3, "external_attempts": 0}'
    )
