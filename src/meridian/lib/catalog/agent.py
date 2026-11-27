from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

AgentMode = Literal["primary", "subagent"]


class MeridianCapabilities(BaseModel):
    """Typed meridian-specific capability overrides from profile frontmatter."""

    model_config = ConfigDict(frozen=True)

    spawn: bool | None = None


class AgentProfile(BaseModel):
    """Parsed agent profile with frontmatter defaults + markdown body."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    mode: AgentMode = "subagent"
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


def _meridian_overrides(frontmatter: dict[str, object]) -> dict[object, object]:
    """Return the `harness-overrides.meridian-cli` block; Meridian owns it, Mars only keeps it."""

    overrides = frontmatter.get("harness-overrides")
    if not isinstance(overrides, dict):
        return {}
    meridian = cast("dict[object, object]", overrides).get("meridian-cli")
    if not isinstance(meridian, dict):
        return {}
    return cast("dict[object, object]", meridian)


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

    markdown = path.read_text(encoding="utf-8")
    frontmatter, body = _split_markdown_frontmatter(markdown)

    name_value = frontmatter.get("name")
    description_value = frontmatter.get("description")
    mode_value = frontmatter.get("mode")
    model_invocable_value = frontmatter.get("model-invocable")
    meridian_overrides = _meridian_overrides(frontmatter)
    resident_rearm_budget_value = meridian_overrides.get("resident-rearm-budget")

    profile_name = str(name_value).strip() if name_value is not None else path.stem
    model_invocable = model_invocable_value if isinstance(model_invocable_value, bool) else True
    mode = str(mode_value).strip() if mode_value is not None else "subagent"
    if mode not in {"primary", "subagent"}:
        mode = "subagent"

    return AgentProfile(
        name=profile_name,
        description=str(description_value).strip() if description_value is not None else "",
        mode=cast("AgentMode", mode),
        subagents=_normalize_string_list(frontmatter.get("subagents")),
        meridian_capabilities=_parse_meridian_capabilities(meridian_overrides.get("capabilities")),
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
