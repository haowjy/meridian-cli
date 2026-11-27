import json
import os
import sys
from pathlib import Path

import pytest

from meridian.lib.core.delivery import DeliveryKind
from meridian.lib.message import people
from meridian.lib.message.result import Delivered, Failed, MessageResult, Receiver

# A people program that answers by address, and records what it was given.
_PROGRAM = """
import json, os, sys, time
address, text = sys.argv[-1], sys.stdin.read()
with open(os.environ["PEOPLE_LOG"], "w") as log:
    json.dump({"argv": sys.argv[1:], "text": text, "env": dict(os.environ)}, log)
if address == "@posted":
    print(json.dumps({"ok": True, "delivery": "queued", "note": "woke p1"}))
elif address == "@refused":
    print(json.dumps({"ok": False, "error": {"kind": "not_found", "text": "no such person"}}))
elif address == "@strange-kind":
    print(json.dumps({"ok": False, "error": {"kind": "martian", "text": "odd"}}))
elif address == "@no-kind":
    print(json.dumps({"ok": False, "error": {"text": "odd"}}))
elif address == "@garbage":
    print("not json")
elif address == "@hang":
    time.sleep(60)
"""


@pytest.fixture
def program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "people.py"
    script.write_text(_PROGRAM, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        f"[message]\npeople = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_HOME", str(home))
    return tmp_path / "log.json"


async def _send(address: str, log: Path, *, env: dict[str, str] | None = None) -> MessageResult:
    return await people.deliver_to_person(
        address,
        "the PR is up",
        Receiver(address=address),
        env={**os.environ, "PEOPLE_LOG": str(log), **(env or {})},
        cwd=None,
    )


@pytest.mark.asyncio
async def test_program_gets_the_address_last_the_text_on_stdin_and_the_env_it_was_given(
    program: Path,
) -> None:
    result = await _send("@posted", program, env={"MERIDIAN_SPAWN_ID": "p7"})

    assert isinstance(result, Delivered)
    assert (result.delivery, result.note) == (DeliveryKind.QUEUED, "woke p1")
    seen = json.loads(program.read_text(encoding="utf-8"))
    assert seen["argv"][-1] == "@posted"
    assert seen["text"] == "the PR is up"
    assert seen["env"]["MERIDIAN_SPAWN_ID"] == "p7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "kind"),
    [
        ("@refused", "not_found"),
        ("@strange-kind", "failed"),
        # Without a whole answer the post may have gone out; only this kind says so.
        ("@no-kind", "maybe_delivered"),
        ("@garbage", "maybe_delivered"),
        ("@silent", "maybe_delivered"),
    ],
)
async def test_program_answer_that_is_not_ok_has_a_kind(
    program: Path, address: str, kind: str
) -> None:
    result = await _send(address, program)

    assert isinstance(result, Failed)
    assert result.kind == kind


@pytest.mark.asyncio
async def test_program_that_hangs_is_stopped_and_may_have_posted(
    program: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(people, "PEOPLE_TIMEOUT_SECONDS", 0.5)

    result = await _send("@hang", program)

    assert isinstance(result, Failed)
    assert result.kind == "maybe_delivered"


@pytest.mark.asyncio
async def test_missing_program_and_unset_config_are_no_route(
    program: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "home" / "config.toml").write_text(
        '[message]\npeople = ["/nonexistent/people-program"]\n', encoding="utf-8"
    )
    missing = await _send("@posted", program)
    (tmp_path / "home" / "config.toml").unlink()
    unset = await _send("@posted", program)

    assert isinstance(missing, Failed) and missing.kind == "no_route"
    assert isinstance(unset, Failed) and unset.kind == "no_route"


def test_program_is_read_from_the_user_config_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `meridian message` runs outside the sandbox; a repository file must not pick the program.
    people_toml = '[message]\npeople = ["sh", "-c", "touch pwned"]\n'
    (tmp_path / "meridian.toml").write_text(people_toml, encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text(people_toml, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", str(home))
    monkeypatch.setenv("MERIDIAN_CONFIG", str(elsewhere))
    monkeypatch.chdir(tmp_path)

    command = people.people_command("@alice")

    assert isinstance(command, Failed) and command.kind == "no_route"
