from pathlib import Path

from meridian.lib.catalog.mars_roots import MarsRoots, mars_content_args


def test_every_mars_lookup_hands_mars_the_built_in_agents(tmp_path: Path) -> None:
    # Built-in agents exist only through this flag; without it `-a inbox-router` fails
    # outside a project that installs one, and nothing else in CI launches it.
    args = mars_content_args(MarsRoots(primary=tmp_path, scope="user"), executable="mars")

    provided = Path(args[args.index("--provided-root") + 1])
    assert (provided / "agents" / "inbox-router.md").is_file()
