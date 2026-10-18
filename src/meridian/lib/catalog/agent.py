"""Agent profile parser for `.mars/agents/*.md`."""
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.catalog.mars_roots import (
    mars_content_roots,
    resolve_mars_roots,
    user_mars_root,
)
from meridian.lib.config.project_root import resolve_project_root_resolution


class MeridianCapabilities(BaseModel):
    """Typed meridian-specific capability overrides from profile frontmatter."""

    model_config = ConfigDict(frozen=True)

    spawn: bool | None = None


class AgentProfile(BaseModel):
    """Parsed agent profile with frontmatter defaults + markdown body."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    mode: Literal["primary", "subagent"] = "subagent"
    subagents: tuple[str, ...] = ()
    meridian_capabilities: MeridianCapabilities | None = None
    model_invocable: bool = True
    resident_rearm_budget: int | None = None
    body: str
    path: Path
    raw_content: str


def _normalize_string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        normalized = value.strip()
        return (normalized,) if normalized else ()
    if isinstance(value, list):
        values = [str(item).strip() for item in cast("list[object]", value) if str(item).strip()]
        return tuple(values)
    return ()


def _split_markdown_frontmatter(markdown: str) -> tuple[dict[str, object], str]:
    import frontmatter  # pyright: ignore[reportMissingTypeStubs]  # python-frontmatter ships no type information.
    import yaml

    try:
        post = frontmatter.loads(markdown)
    except yaml.YAMLError:
        return {}, markdown
    return dict(post.metadata), post.content


def _files_have_equal_text(first: Path, second: Path) -> bool:
    try:
        return first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
    except OSError:
        return False


def _parse_meridian_capabilities(value: object) -> MeridianCapabilities | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    spawn_value: bool | None = None
    for raw_key, raw_value in cast("dict[object, object]", value).items():
        if str(raw_key).strip() != "spawn" or not isinstance(raw_value, bool):
            continue
        spawn_value = raw_value
    if spawn_value is None:
        return None
    return MeridianCapabilities(spawn=spawn_value)


def parse_agent_profile(path: Path) -> AgentProfile:
    """Parse a single markdown agent profile file."""

    markdown = path.read_text(encoding="utf-8")
    frontmatter, body = _split_markdown_frontmatter(markdown)

    name_value = frontmatter.get("name")
    description_value = frontmatter.get("description")
    mode_value = frontmatter.get("mode")
    model_invocable_value = frontmatter.get("model-invocable")
    resident_rearm_budget_value = frontmatter.get("resident-rearm-budget")

    profile_name = str(name_value).strip() if name_value is not None else path.stem
    model_invocable = (
        model_invocable_value if isinstance(model_invocable_value, bool) else True
    )
    mode = str(mode_value).strip() if mode_value is not None else "subagent"
    if mode not in {"primary", "subagent"}:
        mode = "subagent"

    return AgentProfile(
        name=profile_name,
        description=str(description_value).strip() if description_value is not None else "",
        mode=cast("Literal['primary', 'subagent']", mode),
        subagents=_normalize_string_list(frontmatter.get("subagents")),
        meridian_capabilities=_parse_meridian_capabilities(
            frontmatter.get("meridian-capabilities")
        ),
        model_invocable=model_invocable,
        resident_rearm_budget=(
            resident_rearm_budget_value
            if isinstance(resident_rearm_budget_value, int)
            and not isinstance(resident_rearm_budget_value, bool)
            else None
        ),
        body=body,
        path=path.resolve(),
        raw_content=markdown,
    )


def _agent_search_dirs(project_root: Path) -> list[Path]:
    roots = resolve_mars_roots(project_root)
    return [root / ".mars" / "agents" for root in mars_content_roots(roots)]


def scan_agent_profiles(
    project_root: Path | None = None,
    search_dirs: list[Path] | None = None,
    *,
    search_paths: object | None = None,
) -> list[AgentProfile]:
    """Parse all agent profiles from configured search directories."""

    root = resolve_project_root_resolution(project_root).project_root
    _ = search_paths
    directories = search_dirs if search_dirs is not None else _agent_search_dirs(root)
    profiles: list[AgentProfile] = []
    selected_by_name: dict[str, AgentProfile] = {}

    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                profile = parse_agent_profile(path)
            except (OSError, ValueError):
                continue
            existing = selected_by_name.get(profile.name)
            if existing is not None:
                if _files_have_equal_text(existing.path, profile.path):
                    continue
                continue
            selected_by_name[profile.name] = profile
            profiles.append(profile)
    return profiles


def load_agent_profile(
    name: str,
    project_root: Path | None = None,
    *,
    search_paths: object | None = None,
) -> AgentProfile:
    """Load one agent profile by filename stem or frontmatter name."""

    normalized = name.strip()
    if not normalized:
        raise ValueError("Agent profile name must not be empty.")

    root = resolve_project_root_resolution(project_root).project_root

    roots = resolve_mars_roots(root)
    for profile in scan_agent_profiles(project_root=root, search_paths=search_paths):
        if profile.path.stem == normalized or profile.name == normalized:
            return profile

    user_root = user_mars_root()
    if roots.scope == "project":
        user_profiles = scan_agent_profiles(
            project_root=root,
            search_dirs=[user_root / ".mars" / "agents"],
        )
        if any(
            profile.path.stem == normalized or profile.name == normalized
            for profile in user_profiles
        ):
            raise FileNotFoundError(
                f"Agent '{normalized}' found in user packages ({user_root}); "
                "add it here: meridian mars add <source>"
            )

    if roots.scope == "user":
        raise FileNotFoundError(
            f"Agent '{normalized}' not found in user packages ({user_root}). "
            "Run 'meridian mars --user add <source>'."
        )

    expected_path = Path(".mars") / "agents" / f"{normalized}.md"
    raise FileNotFoundError(
        "\n".join(
            (
                f"Agent '{normalized}' not found.",
                "",
                f"Expected: {expected_path.as_posix()}",
                "",
                "Run `meridian mars sync` to populate your agents directory, "
                "or see README.md for manual setup.",
            )
        )
    )
