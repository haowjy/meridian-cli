"""Search witnesses must describe the very snapshot the normalizer reads."""

import json
import sqlite3

import pytest

from meridian.lib.harness.native_witness import OpenCodeV1Witness, OpenCodeV2Witness
from meridian.lib.harness.opencode_snapshot import (
    opencode_session_witnesses,
    read_opencode_snapshot,
)
from tests.support.opencode_db import write_opencode_db_session, write_opencode_v2_db_session


@pytest.mark.parametrize("version", [1, 2])
def test_grouped_witness_and_events_share_read_snapshot(tmp_path, version):
    path = tmp_path / "opencode.db"
    if version == 1:
        write_opencode_db_session(
            db_path=path, session_id="one", messages=[("assistant", "original")]
        )
        expected = OpenCodeV1Witness(1, 1778945817031, 1, 1778945817030, 1778945817030)
        table = "part"
    else:
        write_opencode_v2_db_session(
            db_path=path, session_id="one", messages=[("assistant", {"text": "original"})]
        )
        expected = OpenCodeV2Witness(1, 1, 1789782212000, 1789782212000)
        table = "session_message"
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        assert opencode_session_witnesses(path, ("one", "missing")) == {"one": expected}
        with read_opencode_snapshot(path, "one") as (witness, events):
            writer.execute(
                f"UPDATE {table} SET time_updated=time_updated+1,data=?",
                (json.dumps({"type": "text", "text": "replacement"}),),
            )
            writer.commit()
            assert witness == expected
            assert "original" in json.dumps(list(events))
        assert opencode_session_witnesses(path, ("one",))["one"] != witness
