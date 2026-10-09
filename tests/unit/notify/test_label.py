from pathlib import Path

import pytest

from meridian.lib.notify.label import build_session_label


@pytest.mark.parametrize(
    ("tmux_session", "project_dir", "work_id", "expected"),
    [
        ("a2", "/repos/meridian-cli", "idle-cache-notify", "[a2] meridian-cli · idle-cache-notify"),
        (None, "/repos/meridian-cli", "idle-cache-notify", "meridian-cli · idle-cache-notify"),
        ("a2", None, "idle-cache-notify", "[a2] checkout · idle-cache-notify"),
        ("a2", "/repos/meridian-cli", None, "[a2] meridian-cli"),
        (None, None, None, "checkout"),
    ],
)
def test_build_session_label_with_each_optional_part_missing(
    tmux_session: str | None,
    project_dir: str | None,
    work_id: str | None,
    expected: str,
) -> None:
    environment = {}
    if tmux_session is not None:
        environment["TMUX_PANE"] = "%7"
    if project_dir is not None:
        environment["MERIDIAN_PROJECT_DIR"] = project_dir
    if work_id is not None:
        environment["MERIDIAN_ACTIVE_WORK_ID"] = work_id

    label = build_session_label(
        environ=environment,
        cwd=Path("/repos/checkout"),
        resolve_tmux_session=lambda _pane: tmux_session,
    )

    assert str(label) == expected
    assert label.tmux_session == tmux_session
