"""Filesystem fixture helpers shared across tests."""

from pathlib import Path


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_agent(
    project_root: Path,
    *,
    name: str,
    model: str,
    skills: list[str] | tuple[str, ...] = (),
    subagents: list[str] | tuple[str, ...] = (),
    harness: str | None = None,
    sandbox: str | None = None,
    mcp_tools: list[str] | tuple[str, ...] | None = None,
    tools: list[str] | tuple[str, ...] | None = None,
    body: str | None = None,
) -> Path:
    """Write one agent profile under `.mars/agents/<name>.md`."""

    lines = [
        "---",
        f"name: {name}",
        f"model: {model}",
        f"skills: [{', '.join(skills)}]",
    ]
    if subagents:
        lines.append(f"subagents: [{', '.join(subagents)}]")
    if harness is not None:
        lines.append(f"harness: {harness}")
    if sandbox is not None:
        lines.append(f"sandbox: {sandbox}")
    if mcp_tools is not None:
        lines.append(f"mcp-tools: [{', '.join(mcp_tools)}]")
    if tools is not None:
        lines.append(f"tools: [{', '.join(tools)}]")
    lines.append("---")
    lines.extend(["", body if body is not None else f"# {name}"])
    return _write(project_root / ".mars" / "agents" / f"{name}.md", "\n".join(lines) + "\n")


def allow_headless_claude(project_root: Path) -> None:
    """Clear a project headless-harness deny so Claude spawn-prepare reaches
    tool resolution. Use in tests that exercise spawn-prepare mechanics, not
    the headless-deny policy itself."""
    (project_root / "meridian.toml").write_text(
        "[spawn]\ndeny_headless_harnesses = []\n", encoding="utf-8"
    )
