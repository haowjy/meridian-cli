"""Capability guidance and harness-templated spawn behavior contracts."""

from __future__ import annotations

import shlex
from pathlib import Path

from meridian.lib.bundled_skills import list_bundled_skills
from meridian.lib.catalog.agent import AgentMode, AgentProfile
from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolver import (
    ResolvedContextPaths,
    context_env_key,
    resolve_context_paths,
)
from meridian.lib.harness.adapter import SpawnUsageContractVariants
from meridian.lib.launch.composition import (
    CAPABILITIES_BLOCK_NAME,
    CompositionBlock,
    GuidancePhase,
)

_GENERAL_CAPABILITIES: tuple[tuple[AgentMode | None, str], ...] = (
    (None, "- Work item (tracked effort): `meridian work`."),
    (
        None,
        "- Past sessions/transcripts: `meridian session search` with a ref, `--work`, "
        "`--workspace`, or `--global`; read with `meridian session log`.",
    ),
    ("primary", "- Notify the human: `meridian notify`."),
    (
        "primary",
        "- Message a running spawn or another live session: `meridian spawn message`; "
        "read `meridian skills messaging__meridian`.",
    ),
    (
        None,
        "- Validate Markdown links / inspect the link graph / validate Mermaid: "
        "`meridian kg check` / `meridian kg graph` / `meridian mermaid check`.",
    ),
    (None, "- Project guidance graph: `meridian qi graph`."),
)

_SPAWN_CAPABILITIES = (
    "- Delegate to an `@agent`: `meridian spawn`; write the brief under the work directory's "
    "`prompts/` with `meridian work path`, pass its absolute path to `--prompt-file`, attach "
    "skills with `--skills`, pass files or a folder with `-f`, carry a prior run with "
    "`--from`, choose its checkout with `--task-dir`, or override its model with `-m`.\n"
    "- Agents you can use: `meridian spawn subagents`.\n"
    "- Live model catalog / model-specific prompting: `meridian mars models list` / "
    "`meridian mars models prompting`."
)

_SPAWN_PROMPTING = """\
# Prompting subagents

Every spawn starts with fresh context. Lead with the task, then give it what
you know and it doesn't: the decisions already made and why, the files and
artifacts that matter by path, the constraints, and what done looks like.
Pass a directory to show the shape of a tree, and name the few files whose
content matters most.

Point to where the work starts, and leave open what the spawn should judge
for itself: how far to look, how to work, and what else might be wrong. A
brief that lists the bugs to hunt or a range to stay inside gets back only
what you already suspected; name your suspicions as risks to check, beside
the open question. State what the spawn may edit.

Agent defaults fit most work. Say why before overriding the model: do so only
when the caller asks or the work needs more depth or a capability the defaults
lack. To get more depth, change the model, not the agent."""

def _build_meridian_skills_prompt(
    *,
    profile_mode: AgentMode,
    loaded: frozenset[str],
) -> str:
    lines = ["# Meridian skills", ""]
    for skill in list_bundled_skills():
        if skill.name in loaded:
            continue
        if skill.mode is not None and skill.mode != profile_mode:
            continue
        description = skill.description
        if description.startswith("When "):
            description = "when " + description.removeprefix("When ")
        lines.append(
            f"- {skill.name}: {description} Read `meridian skills {skill.name}`."
        )
    return "\n".join(lines)


def _work_link_line(work_link: Path) -> str:
    """The one work-link wording, for a primary's prompt and a spawn's run block."""

    return (
        f"- `{work_link.as_posix()}` — work directory "
        "(`$MERIDIAN_ACTIVE_WORK_DIR`); write this literal path, never the "
        "directory it resolves to: the link follows `work start`, `work switch`, "
        "`work done` and `work rename`."
    )


def _task_link_line(task_link: Path, *, detail: str = "", note: str = "") -> str:
    """The one task-link wording; a surface adds only what it alone knows.

    *detail* follows the environment variable and *note* the command it names, so
    both surfaces carry the literal-path rule verbatim.
    """

    return (
        f"- `{task_link.as_posix()}` — source checkout (`$MERIDIAN_TASK_DIR`){detail}; "
        "write this literal path, never the directory it resolves to: the link "
        f"follows `meridian task-dir` and a work switch{note}."
    )


def build_capabilities_prompt(
    *,
    spawn_capable: bool,
    profile_mode: AgentMode,
    project_root: Path,
    control_root: Path,
    task_dir: Path,
    task_link: Path | None = None,
    work_link: Path | None = None,
    paths_in_user_turn: bool = False,
    has_message_route: bool,
    chat_id: str | None = None,
    context_config: ContextConfig | None = None,
    resolved_context: ResolvedContextPaths | None = None,
) -> str:
    """Render the stable capability and directory guidance block.

    Only values shared by every launch of this conversation belong here: an exact
    continue recomposes the block from the launch snapshot, and a fresh launch of
    the same agent in the same work context must share the cached prefix. Per-launch
    identity (the run ID, the conversation that launched the run, and a spawn's
    per-chat links) goes in the first user turn through
    :func:`build_launch_facts_prompt`; a primary's own chat ID and its per-chat
    links stay here because they outlive the launch.

    The work directory has one line, and a surface either places it here or in the
    user turn: an item's ID and its scratch path are per-conversation state that a
    ``work switch`` or ``work done`` invalidates, so the link is the only path named.
    ``paths_in_user_turn`` says the user turn names them instead, which a spawn does
    because its link paths belong to its own chat; a primary cannot, because a
    ``--continue`` replays no user turn.
    """

    if resolved_context is None:
        if context_config is None:
            from meridian.lib.state.paths import load_context_config

            context_config = load_context_config(project_root) or ContextConfig()
        resolved_context = resolve_context_paths(project_root, context_config)

    lines = [
        "# Meridian capabilities",
        "",
        "Run each Meridian command as its own shell call, for example `meridian task-dir`.",
        "Restricted shells refuse `cd ... && meridian ...`, pipes, `$()`, `VAR=1`, and "
        "`$VAR` expansion.",
        "Every command has `-h`; if one is refused, tell your caller or the human—do "
        "not work around it.",
        "",
    ]
    if spawn_capable:
        lines.append(_SPAWN_CAPABILITIES)
    lines.extend(
        capability
        for required_mode, capability in _GENERAL_CAPABILITIES
        if required_mode is None or required_mode == profile_mode
    )

    if work_link is not None:
        # A per-chat link outlives the launch and follows `work start`,
        # `work switch`, `work done` and `work rename`, so it replaces both the
        # work-item ID and its scratch directory: either would go stale mid-run.
        lines.append(_work_link_line(work_link))

    # The goal text never enters a prompt: it is per-conversation state that can be
    # edited mid-conversation, and a primary with no task must start no turn. The
    # command resolves the item itself, so the pointer carries no ID that could go
    # stale after `work switch`.
    lines.append("- Your work item's goal and status: run `meridian work show`.")

    resolved_chat_id = (chat_id or "").strip()
    if resolved_chat_id:
        # Only a primary renders this: its chat is the conversation's own, so it
        # survives resume. A spawn's launching chat is per-launch and lives in the
        # `# Meridian run` block.
        lines.append(
            f"- `{resolved_chat_id}` — this conversation's chat "
            "(`$MERIDIAN_CHAT_ID`); use it in names, and as the parent for handoff `--from`."
        )
        if has_message_route:
            # Chat IDs count per project, so the bare ID is ambiguous to a peer in
            # another repo; the command carries the project it belongs to.
            lines.append(
                "- To message this session from another session or repo: "
                f"`meridian -C {shlex.quote(project_root.as_posix())} spawn message "
                f'{resolved_chat_id} --message "..."`.'
            )

    task_path = task_dir.as_posix()
    control_path = control_root.as_posix()
    spawn_suffix = (
        "; set the work item's checkout with `meridian work task-dir`, or one spawn's "
        "checkout with `meridian spawn --task-dir`"
        if spawn_capable
        else ""
    )
    if task_link is not None:
        # A per-chat link: every task-dir change repoints it, so the literal path is
        # the one the session keeps using, and the directory it resolves to is not.
        lines.append(
            _task_link_line(
                task_link,
                detail=(
                    f"; shell cwd is `{control_path}` (`$MERIDIAN_PROJECT_DIR`), "
                    "so use absolute paths"
                ),
                note=f"; inspect: `meridian task-dir`{spawn_suffix}",
            )
        )
    elif paths_in_user_turn:
        # The user turn names the link; naming the directory it resolves to here would
        # be a second answer to "where do I work".
        lines.append(
            f"- Shell cwd is `{control_path}` (`$MERIDIAN_PROJECT_DIR`); your work "
            "directory and source checkout are the links `$MERIDIAN_ACTIVE_WORK_DIR` and "
            "`$MERIDIAN_TASK_DIR`, which this run's `# Meridian run` block names. Write "
            "those literal paths, never the directories they resolve to; inspect: "
            f"`meridian task-dir`{spawn_suffix}."
        )
    elif task_dir == control_root:
        lines.append(
            f"- `{task_path}` — source checkout and shell cwd "
            "(`$MERIDIAN_TASK_DIR`, `$MERIDIAN_PROJECT_DIR`); inspect: `meridian task-dir`"
            f"{spawn_suffix}."
        )
    else:
        lines.append(
            f"- `{task_path}` — source checkout (`$MERIDIAN_TASK_DIR`); shell cwd is "
            f"`{control_path}` (`$MERIDIAN_PROJECT_DIR`), so use absolute paths; inspect: "
            f"`meridian task-dir`{spawn_suffix}."
        )

    if resolved_context.kb_root is None:
        lines.append(
            "- No resolved knowledge base (`$MERIDIAN_CONTEXT_KB_DIR`); inspect: "
            "`meridian context kb`."
        )
    else:
        lines.append(
            f"- `{resolved_context.kb_root.as_posix()}` — durable knowledge base "
            "(`$MERIDIAN_CONTEXT_KB_DIR`); inspect: `meridian context kb`."
        )

    lines.append(
        "- Your run ID is in `$MERIDIAN_SPAWN_ID`; `meridian task-dir --json` prints "
        "it as `spawn_id`, and a launch with a first user turn also gets it in that "
        "turn's `# Meridian run` block."
    )

    other_context: list[str] = []
    if resolved_context.work_archive is not None:
        other_context.append(
            f"`{resolved_context.work_archive.as_posix()}` — work archive "
            f"(`${context_env_key('work_archive')}`)"
        )
    for name in sorted(resolved_context.extra):
        path, _ = resolved_context.extra[name]
        other_context.append(
            f"`{path.as_posix()}` — {name} (`${context_env_key(name)}`)"
        )
    if other_context:
        lines.append("- " + "; ".join(other_context) + "; inspect: `meridian context`.")

    return "\n".join(lines)


def build_launch_facts_prompt(
    *,
    spawn_id: str | None = None,
    chat_id: str | None = None,
    work_link: Path | None = None,
    task_link: Path | None = None,
) -> str:
    """Render per-launch identity for the first user turn.

    Only values that change between launches of the same conversation belong here:
    the run ID, the conversation that launched this run, and a spawn's per-chat
    links, whose paths name the chat it runs in. Keeping them out of the system
    prompt lets sibling spawns send byte-identical system bytes, so the harness
    prompt cache is shared.
    """

    lines: list[str] = []
    run_id = (spawn_id or "").strip()
    if run_id:
        lines.append(
            f"- `{run_id}` — your run ID (`$MERIDIAN_SPAWN_ID`), the ID your caller "
            "holds; use it in names."
        )
    launched_by = (chat_id or "").strip()
    if launched_by:
        lines.append(
            f"- `{launched_by}` — the conversation that launched this run "
            "(`$MERIDIAN_CHAT_ID`), parent for handoff `--from`."
        )
    if work_link is not None:
        lines.append(_work_link_line(work_link))
    if task_link is not None:
        lines.append(_task_link_line(task_link))
    if not lines:
        return ""
    return "\n".join(("# Meridian run", "", *lines))


def build_spawn_usage_contract(variants: SpawnUsageContractVariants) -> str:
    """Render the behavioral spawn contract filled with adapter-owned phrasing."""

    return f"""\
# Meridian spawn rules

- Use Meridian, not the harness's native agent tool, when a listed Meridian agent fits.
- {variants.double_wrap_bullet}
- {variants.timeout_bullet}
- Drain every --bg spawn with no-arg `meridian spawn wait` before you
  respond to the user, start dependent work, or end your turn. It finds
  running and owed spawns by session; re-invoke to keep waiting. The wait
  itself can run in your native background execution to keep working
  while it blocks — it's the launch, not the wait, that must never be
  background-wrapped. Do not pipe or truncate its output. Un-waited
  background spawns are lost.
- Before a child closes, Meridian may ask it to confirm completion; follow the
  confirmation message's instructions."""


def has_spawn_capability(profile: AgentProfile | None) -> bool:
    if profile is None:
        return False
    if profile.meridian_capabilities is not None and profile.meridian_capabilities.spawn is False:
        return False
    return len(profile.subagents) > 0


def build_guidance_blocks(
    *,
    profile: AgentProfile | None,
    loaded_skills: frozenset[str],
    spawn_usage_contract: str,
    bundle_inventory_prompt: str | None,
    harness_guidance: str | None = None,
) -> tuple[CompositionBlock, ...]:
    """Build all system-prompt guidance blocks.

    Spawn-gated (only when has_spawn_capability): inventory, spawn capabilities,
    prompting judgment, and the spawn behavior contract. Profile-mode gates apply to
    bundled skill triggers and individual general capabilities. Context paths are always
    present. The empty capability block is filled at bind, after its directory and run
    identity facts are known.
    """
    blocks: list[CompositionBlock] = []
    spawn_capable = has_spawn_capability(profile)
    profile_mode: AgentMode = profile.mode if profile is not None else "subagent"
    if spawn_capable:
        inventory = (bundle_inventory_prompt or "").strip()
        if inventory:
            blocks.append(CompositionBlock("inventory", GuidancePhase.GUIDANCE, 0, inventory))
    blocks.append(
        CompositionBlock(
            "meridian-skills",
            GuidancePhase.GUIDANCE,
            4,
            _build_meridian_skills_prompt(
                profile_mode=profile_mode,
                loaded=loaded_skills,
            ),
        )
    )
    blocks.append(
        CompositionBlock(
            CAPABILITIES_BLOCK_NAME,
            GuidancePhase.GUIDANCE,
            5,
            "",
        )
    )
    if spawn_capable:
        blocks.append(
            CompositionBlock("spawn-prompting", GuidancePhase.GUIDANCE, 7, _SPAWN_PROMPTING)
        )
        blocks.append(
            CompositionBlock(
                "spawn-contract",
                GuidancePhase.GUIDANCE,
                10,
                spawn_usage_contract,
            )
        )
    harness_hint = (harness_guidance or "").strip()
    if harness_hint:
        blocks.append(
            CompositionBlock("harness-guidance", GuidancePhase.GUIDANCE, 23, harness_hint)
        )
    return tuple(blocks)


__all__ = [
    "build_capabilities_prompt",
    "build_guidance_blocks",
    "build_launch_facts_prompt",
    "build_spawn_usage_contract",
    "has_spawn_capability",
]
