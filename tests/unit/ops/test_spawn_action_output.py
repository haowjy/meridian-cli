"""Spawn action output rendering contracts."""

from meridian.lib.ops.spawn.models import SpawnActionOutput


def test_failed_spawn_without_session_log_omits_transcript_command() -> None:
    output = SpawnActionOutput(
        command="spawn.create",
        status="failed",
        spawn_id="p4735",
        session_log_available=False,
        duration_secs=0.5,
    )

    assert "Transcript:" not in output.format_text()
    assert "transcript_command" not in output.to_wire()
