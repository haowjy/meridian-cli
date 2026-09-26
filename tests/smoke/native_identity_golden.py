"""Runtime launch golden; only shell shims and synthetic stores.

Run from the lane venv with env -i HOME=... PATH=... TERM=dumb and
PYTHONPATH=<checkout>/src:<checkout>, selecting base then candidate:
    uv run python tests/smoke/native_identity_golden.py /tmp/base-fixtures > /tmp/base.json
    uv run python tests/smoke/native_identity_golden.py /tmp/new-fixtures /tmp/base.json

The optional baseline argument verifies the exact allowed-difference table.
OpenCode fork capability is overridden only to reach its blackbox fallback:
normal policy downgrades it, because the streaming transports reject forks.
Codex preforked fixtures model already-materialized rollouts and use dry_run=False;
no harness/model turn is launched. UUID assignment is fixed for stable argv.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import PropertyMock

import pytest

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.claude_sessions import project_slug
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.state.session_store import ConversationModelSelection
from tests.support.executables import prepend_fake_executables
from tests.support.launch import stub_bundle_request_and_resolve
from tests.support.opencode_db import write_opencode_db_session
from tests.support.pi_extensions import configure_pi_extension_projection

SID = "12345678-1234-4234-8234-123456789abc"
FORK = "22345678-1234-4234-8234-123456789abc"
MINT = "32345678-1234-4234-8234-123456789abc"
base = Path(sys.argv[1]).resolve()
base.mkdir(parents=True, exist_ok=True)
rows = []
for harness in ("claude", "codex", "opencode", "pi"):
    for op in (
        "create",
        "resume",
        "fork",
        "untracked-resume",
        *(["preforked"] if harness == "codex" else []),
        *(["untracked-fork"] if harness == "pi" else []),
        *(
            ["missing-tracked", "missing-untracked", "mismatched", "unbound", "traversal"]
            if harness == "claude"
            else []
        ),
    ):
        for interactive in (False, True):
            for canonical in (True, False):
                model = "" if harness == "opencode" else "openai/test-model"
                name = (
                    f"{harness}/{op}/{'interactive' if interactive else 'noninteractive'}/"
                    f"{'canonical' if canonical else 'noncanonical'}"
                )
                case = base / name
                case.mkdir(parents=True, exist_ok=True)
                root = case / "repo"
                root.mkdir()
                (root / "mars.toml").write_text(f'[settings]\ntargets = [".{harness}"]\n')
                home = case / "home"
                home.mkdir()
                with pytest.MonkeyPatch.context() as mp:
                    mp.setenv("HOME", str(home))
                    mp.setenv("MERIDIAN_HOME", str(case / "meridian-home"))
                    mp.setenv("CLAUDE_CONFIG_DIR", str(case / "native" / "claude"))
                    mp.setenv("CODEX_HOME", str(case / "native" / "codex"))
                    mp.setenv("OPENCODE_DB", str(case / "native" / "opencode.db"))
                    mp.setenv("PI_CODING_AGENT_SESSION_DIR", str(case / "native" / "pi"))
                    mp.setenv("PI_CODING_AGENT_DIR", str(case / "pi-agent"))
                    configure_pi_extension_projection(mp, case)
                    stub_bundle_request_and_resolve(mp, model=model, harness=HarnessId(harness))
                    prepend_fake_executables(mp, case, harness)
                    (case / "fake-bin" / harness).write_text(
                        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo 1.0.0; '
                        'else echo "--mode rpc --session-id --fork --session --session-dir '
                        "--no-extensions --no-skills --no-context-files --no-prompt-templates "
                        '-e --extension PI_CODING_AGENT_SESSION_DIR"; fi\n'
                    )
                    store = {
                        "claude": case / "native" / "claude" / "projects" / project_slug(root),
                        "codex": case / "native" / "codex" / "sessions",
                        "opencode": case / "native" / "opencode.db",
                        "pi": case / "meridian-home" / "meridian-pi" / "sessions",
                    }[harness]
                    ids = [SID, FORK] if op == "preforked" else [SID]
                    if harness == "opencode":
                        write_opencode_db_session(db_path=store, session_id=SID, messages=[])
                    else:
                        store.mkdir(parents=True, exist_ok=True)
                        for sid in ids:
                            filename = {
                                "claude": f"{sid}.jsonl",
                                "codex": f"rollout-2026-01-01T00-00-00-{sid}.jsonl",
                                "pi": f"1_{sid}.jsonl",
                            }[harness]
                            header = {
                                "claude": {"sessionId": sid},
                                "codex": {"type": "session_meta", "payload": {"id": sid}},
                                "pi": {"type": "session", "id": sid},
                            }[harness]
                            (store / filename).write_text(json.dumps(header) + "\n")
                    if op.startswith("missing"):
                        (store / f"{SID}.jsonl").unlink()
                    if op == "mismatched":
                        (store / f"{SID}.jsonl").write_text(json.dumps({"sessionId": FORK}) + "\n")
                    # Existing ../ component keeps the recorded path usable but noncanonical.
                    (store.parent / "alias").mkdir(exist_ok=True)
                    recorded = (
                        str(store)
                        if canonical
                        else str(store.parent / "alias") + "/../" + store.name
                    )
                    tracked = op != "create" and "untracked" not in op
                    source_id = "../x" if op == "traversal" else SID
                    session = SessionRequest(
                        requested_harness_session_id=None if op == "create" else source_id,
                        continue_fork="fork" in op or op == "preforked",
                        continue_source_tracked=tracked,
                        continue_source_ref="c1" if tracked else None,
                        source_native_store=recorded if tracked and op != "unbound" else None,
                        conversation_intent=ConversationModelSelection(
                            selection_source="recorded_selection"
                        ),
                    )
                    adapter = HarnessRegistry.with_defaults().get(HarnessId(harness))
                    if harness == "opencode" and op == "fork":
                        mp.setattr(
                            type(adapter),
                            "capabilities",
                            PropertyMock(
                                return_value=adapter.capabilities.model_copy(
                                    update={"supports_session_fork": True}
                                )
                            ),
                        )
                    original = adapter.finalize_native_identity
                    finalized = []
                    writes = []

                    def finalize(
                        *args,
                        _case=case,
                        _original=original,
                        _writes=writes,
                        _finalized=finalized,
                        **kwargs,
                    ):
                        _finalized.append(True)
                        marker = _case / "marker"
                        marker.touch()
                        try:
                            return _original(*args, **kwargs)
                        finally:
                            _writes.extend(
                                subprocess.check_output(
                                    ["find", str(_case), "-newer", str(marker)], text=True
                                ).splitlines()
                            )

                    mp.setattr(adapter, "finalize_native_identity", finalize)
                    mp.setattr("meridian.lib.harness.claude.uuid4", lambda: MINT)
                    mp.setattr("meridian.lib.harness.pi.mint_session_id", lambda store: MINT)
                    try:
                        ctx = build_launch_context(
                            spawn_id="p42",
                            request=SpawnRequest(
                                harness=harness,
                                model=model,
                                prompt="hello",
                                session=session,
                                launch_policy_snapshot=LaunchPolicySnapshot(
                                    model=model, harness=harness
                                ),
                            ),
                            runtime=LaunchRuntime(
                                argv_intent=LaunchArgvIntent.REQUIRED,
                                composition_surface=LaunchCompositionSurface.PRIMARY
                                if interactive
                                else LaunchCompositionSurface.DIRECT,
                                runtime_root=str(case / "rt"),
                                project_paths_project_root=str(root),
                                project_paths_execution_cwd=str(root),
                            ),
                            plan_overrides={
                                "PI_CODING_AGENT_SESSION_DIR": str(case / "native" / "pi")
                            },
                            harness_registry=HarnessRegistry.with_defaults(),
                            dry_run=op != "preforked",
                            forked_harness_session_id=FORK if op == "preforked" else None,
                        )
                        spec = ctx.binding.spec
                        identity = getattr(spec, "native_identity", None) or getattr(
                            spec, "native_identity_plan", None
                        )
                        row = {
                            "case": name,
                            "accepted": True,
                            "identity": [
                                harness,
                                identity.native_store,
                                getattr(
                                    identity,
                                    "session_id",
                                    getattr(identity, "harness_session_id", None),
                                ),
                                identity.operation,
                            ],
                            "argv": list(ctx.binding.argv),
                            "env": {
                                k: v
                                for k, v in ctx.binding.environment.final_env.items()
                                if k
                                in (
                                    "CLAUDE_CONFIG_DIR",
                                    "CODEX_HOME",
                                    "OPENCODE_DB",
                                    "PI_CODING_AGENT_SESSION_DIR",
                                )
                            },
                            "finalize_writes": writes,
                        }
                    except Exception as exc:
                        row = {
                            "case": name,
                            "accepted": False,
                            "error": type(exc).__name__,
                            "code": getattr(exc, "failure_code", str(exc)),
                            "finalize_writes": writes,
                        }
                    normalized = (
                        json.dumps(row)
                        .replace(project_slug(root), "<SLUG>")
                        .replace(str(case), "<CASE>")
                    )
                    rows.append(json.loads(normalized))
print(json.dumps(rows, indent=2))

if len(sys.argv) > 2:
    before = json.loads(Path(sys.argv[2]).read_text())
    assert len(before) == len(rows) == 92
    assert sum(row["accepted"] for row in before) == 86
    assert sum(row["accepted"] for row in rows) == 60
    changes = []
    for old, new in zip(before, rows, strict=True):
        assert old["case"] == new["case"]
        assert not new["finalize_writes"], new
        if old == new:
            continue
        harness, operation, _, canonical = new["case"].split("/")
        if harness == "claude" and operation in {
            "missing-tracked",
            "missing-untracked",
            "mismatched",
            "unbound",
            "traversal",
        }:
            assert old["accepted"] and not new["accepted"], new
            reason = "d"
        elif (
            harness in {"opencode", "pi"}
            and canonical == "noncanonical"
            and (operation == "resume" or (harness == "opencode" and operation == "fork"))
        ):
            assert old["accepted"] and new.get("code") == "native_transcript_missing", new
            reason = "noncanonical"
        elif harness == "opencode" and operation == "fork":
            assert old["accepted"] and new["accepted"], new
            assert old["identity"][2:] == [SID, "resume"]
            assert new["identity"][2:] == [None, "fork"]
            assert {k: v for k, v in old.items() if k != "identity"} == {
                k: v for k, v in new.items() if k != "identity"
            }
            reason = "g"
        else:
            raise AssertionError((old, new))
        changes.append((new["case"], reason))
    print(f"Verified {len(rows)} cases; {len(changes)} allowed differences", file=sys.stderr)
    for case, reason in changes:
        print(f"{case}: {reason}", file=sys.stderr)
