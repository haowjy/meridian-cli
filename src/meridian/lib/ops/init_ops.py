"""Init-with-add orchestration for meridian project setup."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.types import normalize_mars_target_name
from meridian.lib.core.util import FormatContext


def _empty_content() -> dict[str, list[str]]:
    return {}


@dataclass(frozen=True)
class InitAddResult:
    """What we extracted from mars add --json + post-sync filesystem."""

    declared_targets: list[str]
    declared_primary_agent: str | None
    content: dict[str, list[str]] = field(default_factory=_empty_content)


@dataclass(frozen=True)
class PrimaryAgentAction:
    action: str  # "none", "set", "already_set", "differs"
    agent: str | None = None
    current: str | None = None
    message: str | None = None


class InitResult(BaseModel):
    """Output from run_init_flow. Supports text and JSON output."""

    model_config = ConfigDict(frozen=True)

    ok: bool = True
    project_root: str
    config_created: bool
    packages_added: list[str]
    packages_requested: list[str]
    targets_linked: list[str]
    content: dict[str, list[str]]
    primary_agent: PrimaryAgentAction | None = None
    next_step: str = "Run `meridian` to start."

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines = [f"Initialized {self.project_root}", ""]
        if self.packages_requested:
            pkg_str = ", ".join(self.packages_requested)
            lines.append(f"  Packages:   {pkg_str} ({len(self.packages_requested)} packages)")
        if self.targets_linked:
            lines.append(f"  Targets:    {', '.join(self.targets_linked)}")
        if self.content:
            parts = [f"{len(items)} {kind}" for kind, items in self.content.items()]
            lines.append(f"  Content:    {', '.join(parts)}")
        if self.primary_agent and self.primary_agent.action == "set":
            lines.append(f"  Primary:    {self.primary_agent.agent} (set in meridian.toml)")
        elif self.primary_agent and self.primary_agent.action == "differs":
            lines.append(f"  Primary:    {self.primary_agent.message}")
        lines.append("")
        lines.append(f"{self.next_step}")
        return "\n".join(lines)


def _run_mars_json(
    project_root: Path,
    command: str,
    args: list[str],
    *,
    executable: str,
) -> dict[str, Any]:
    """Run a mars command with --json and return parsed output."""

    from meridian.cli.mars_passthrough import (
        execute_mars_passthrough,
        parse_mars_passthrough,
    )

    full_args = ["--root", project_root.as_posix(), "--json", command, *args]
    request = parse_mars_passthrough(full_args, executable=executable, output_format="json")
    result = execute_mars_passthrough(request)
    if result.returncode != 0:
        if result.stderr_text:
            sys.stderr.write(result.stderr_text)
        raise SystemExit(result.returncode)
    return json.loads(result.stdout_text) if result.stdout_text else {}


def _scan_mars_content(project_root: Path) -> dict[str, list[str]]:
    """Scan .mars/ for materialized content by type.

    Returns a dict like {"agents": ["coder", "reviewer"], "skills": ["spawn", ...]}.
    Discovers content types dynamically from subdirectory names.
    """
    mars_dir = project_root / ".mars"
    if not mars_dir.is_dir():
        return {}
    # Dirs that are internal implementation state, not user-visible content.
    _SKIP_DIRS = {"cache"}
    content: dict[str, list[str]] = {}
    for subdir in sorted(mars_dir.iterdir()):
        if not subdir.is_dir():
            continue
        if subdir.name in _SKIP_DIRS:
            continue
        # Skills and bootstrap are stored as directories; agents are stored as files.
        # Include both so all content types are counted correctly.
        items = sorted(f.stem for f in subdir.iterdir() if f.is_file() or f.is_dir())
        if items:
            content[subdir.name] = items
    return content


def run_mars_add_json(
    project_root: Path,
    sources: list[str],
    *,
    executable: str,
) -> InitAddResult:
    """Run mars add with JSON output and scan materialized content."""

    parsed = _run_mars_json(project_root, "add", sources, executable=executable)
    content = _scan_mars_content(project_root)
    declared_targets: list[str] = parsed.get("declared_targets", [])
    declared_primary_agent: str | None = parsed.get("declared_primary_agent")
    return InitAddResult(
        declared_targets=declared_targets,
        declared_primary_agent=declared_primary_agent,
        content=content,
    )


def maybe_set_primary_agent(
    project_root: Path,
    declared_primary_agent: str | None,
) -> PrimaryAgentAction:
    """Set primary.agent if unset. Returns what happened."""

    if not declared_primary_agent:
        return PrimaryAgentAction(action="none")

    config_path = project_root / "meridian.toml"
    if not config_path.is_file():
        return PrimaryAgentAction(action="none")

    content = config_path.read_text(encoding="utf-8")

    import tomlkit

    doc_map = cast("dict[str, Any]", tomlkit.parse(content))
    primary_table_raw: Any = doc_map.get("primary")
    primary_table: dict[str, Any] | None = (
        cast("dict[str, Any]", primary_table_raw) if isinstance(primary_table_raw, dict) else None
    )
    if primary_table is not None:
        current_agent_raw: Any = primary_table.get("agent")
        if isinstance(current_agent_raw, str) and current_agent_raw.strip():
            if current_agent_raw == declared_primary_agent:
                return PrimaryAgentAction(action="already_set", agent=declared_primary_agent)
            return PrimaryAgentAction(
                action="differs",
                agent=declared_primary_agent,
                current=current_agent_raw,
                message=(
                    f"Package recommends '{declared_primary_agent}' as primary agent. "
                    f"Current primary is '{current_agent_raw}'. "
                    f"Run `meridian -a {declared_primary_agent}` to try it, "
                    f"or `meridian config set primary.agent {declared_primary_agent}` "
                    f"to change your default."
                ),
            )

    from meridian.lib.config.preserving_edit import set_scalar_option
    from meridian.lib.config.settings import OPTION_CATALOG
    from meridian.lib.state.atomic import atomic_write_text

    option = OPTION_CATALOG.resolve_key("primary.agent")
    edit_result = set_scalar_option(content, option=option, value=declared_primary_agent)
    atomic_write_text(config_path, edit_result.text)
    return PrimaryAgentAction(action="set", agent=declared_primary_agent)


def maybe_scaffold_claude_agent_copy(project_root: Path, targets: list[str]) -> bool:
    """Enable Claude native agent copies in mars.toml when a `.claude` target is linked.

    Writes ``[settings.meridian.agent_copy] harnesses = ["claude"]`` once. Idempotent:
    a no-op when no claude target is linked or the table already exists. Returns True
    only when the table was newly added.
    """
    import tomlkit

    normalized = {normalize_mars_target_name(target) for target in targets}
    if "claude" not in normalized:
        return False
    mars_toml = project_root / "mars.toml"
    if not mars_toml.is_file():
        return False

    # Round-trip edit so we preserve comments/formatting and never corrupt valid TOML.
    # A raw string append would break documents that already define settings.meridian
    # (e.g. an inline `meridian = { ... }`), producing a "declared twice" parse error.
    doc = cast("dict[str, Any]", tomlkit.parse(mars_toml.read_text(encoding="utf-8")))

    from tomlkit.items import InlineTable

    settings_raw: Any = doc.get("settings")
    settings: dict[str, Any]
    if isinstance(settings_raw, dict):
        settings = cast("dict[str, Any]", settings_raw)
    else:
        settings = cast("dict[str, Any]", tomlkit.table())
        doc["settings"] = settings

    meridian_raw: Any = settings.get("meridian")
    meridian: dict[str, Any]
    if isinstance(meridian_raw, dict):
        meridian = cast("dict[str, Any]", meridian_raw)
        parent_inline = isinstance(meridian_raw, InlineTable)
    else:
        meridian = cast("dict[str, Any]", tomlkit.table())
        settings["meridian"] = meridian
        parent_inline = False

    if isinstance(meridian.get("agent_copy"), dict):
        return False

    # A standard table cannot nest inside an inline table; match the parent's shape.
    agent_copy: dict[str, Any] = cast(
        "dict[str, Any]", tomlkit.inline_table() if parent_inline else tomlkit.table()
    )
    agent_copy["harnesses"] = ["claude"]
    agent_copy["include_fanout"] = False
    meridian["agent_copy"] = agent_copy

    from meridian.lib.state.atomic import atomic_write_text

    atomic_write_text(mars_toml, tomlkit.dumps(doc))
    return True


def run_init_flow(
    *,
    project_root: Path,
    add_sources: list[str],
    link_targets: list[str] | None = None,
    output_format: str = "text",
) -> InitResult | dict[str, Any]:
    """Full init-with-add orchestration.

    Sequence:
    1. Bootstrap meridian.toml (config_init_sync)
    2. mars init if no mars.toml
    3. mars add <sources>  (skipped when add_sources is empty)
    4. Determine targets (--link overrides, else declared)
    5. mars link for each target
    6. Set primary agent if applicable
    7. Return result
    """

    from meridian.cli.mars_passthrough import resolve_mars_executable
    from meridian.lib.ops.config import ConfigInitInput, config_init_sync

    # 1. Bootstrap meridian.toml
    config_result = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))

    # Resolve mars executable
    executable = resolve_mars_executable()
    if executable is None:
        sys.stderr.write(
            "error: Failed to execute 'mars'. Install meridian with dependencies and retry.\n"
        )
        raise SystemExit(1)

    # 2. mars init if no mars.toml
    mars_toml = project_root / "mars.toml"
    if not mars_toml.is_file():
        _run_mars_json(project_root, "init", [], executable=executable)

    # 3. mars add (skipped when no sources requested)
    add_result: InitAddResult | None = None
    if add_sources:
        add_result = run_mars_add_json(project_root, add_sources, executable=executable)

    # 4. Determine targets
    declared_targets = add_result.declared_targets if add_result else []
    targets: list[str] = link_targets if link_targets is not None else declared_targets

    # 5. mars link for each target
    for target in targets:
        _run_mars_json(project_root, "link", [target], executable=executable)

    # 5b. Enable Claude native agent copies when a .claude target is linked.
    maybe_scaffold_claude_agent_copy(project_root, targets)

    # 6. Primary agent
    declared_primary_agent = add_result.declared_primary_agent if add_result else None
    primary_action = maybe_set_primary_agent(project_root, declared_primary_agent)

    # 7. Build result
    content: dict[str, list[str]] = add_result.content if add_result else {}

    result = InitResult(
        project_root=project_root.as_posix(),
        config_created=config_result.created,
        packages_added=add_sources,
        packages_requested=add_sources,
        targets_linked=targets,
        content=content,
        primary_agent=primary_action,
    )

    if output_format == "json":
        return result.model_dump()
    return result
