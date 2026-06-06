"""Agent profile parser for `.mars/agents/*.md`."""
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.catalog.skill import files_have_equal_text, split_markdown_frontmatter
from meridian.lib.config.project_root import resolve_project_root_resolution


class AgentProfile(BaseModel):
    """Parsed agent profile with frontmatter defaults + markdown body."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    mode: Literal["primary", "subagent"] = "subagent"
    skills: tuple[str, ...]
    model_invocable: bool = True
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


def parse_agent_profile(path: Path) -> AgentProfile:
    """Parse a single markdown agent profile file."""

    markdown = path.read_text(encoding="utf-8")
    frontmatter, body = split_markdown_frontmatter(markdown)

    name_value = frontmatter.get("name")
    description_value = frontmatter.get("description")
    mode_value = frontmatter.get("mode")
    model_invocable_value = frontmatter.get("model-invocable")

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
        skills=_normalize_string_list(frontmatter.get("skills")),
        model_invocable=model_invocable,
        body=body,
        path=path.resolve(),
        raw_content=markdown,
    )


def _agent_search_dirs(project_root: Path) -> list[Path]:
    return [project_root / ".mars" / "agents"]


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
                if files_have_equal_text(existing.path, profile.path):
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

    for profile in scan_agent_profiles(project_root=root, search_paths=search_paths):
        if profile.path.stem == normalized or profile.name == normalized:
            return profile

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
