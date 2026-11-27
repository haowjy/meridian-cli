from pathlib import Path

import pytest

from meridian.lib.catalog.mars_cli import mars_content_args


def test_every_mars_lookup_hands_mars_the_built_in_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Built-in agents exist only through this flag; without it `-a inbox-router` fails
    # outside a project that installs one, and nothing else in CI launches it.
    monkeypatch.setenv("MERIDIAN_HOME", tmp_path.as_posix())
    args = mars_content_args()

    provided = Path(args[args.index("--provided-root") + 1])
    assert (provided / "agents" / "inbox-router.md").is_file()
