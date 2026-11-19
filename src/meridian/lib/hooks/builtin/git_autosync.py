"""The removed `git-autosync` hook: a row still loads, does nothing, and says why.

Git contexts sync through the `meridian-sync` daemon now (`[context.<name>] autosync`).
Kept for one minor release so an old `meridian.toml` isn't a load error; delete with it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from meridian.plugin_api import Hook, HookContext, HookEventName, HookResult

_REMOVED = (
    "git-autosync was removed; git contexts sync by themselves "
    "(see `[context.<name>] autosync`). Delete this row."
)
_LOCAL_ONLY = " It also made local-only commits for `options.path`, which nothing replaces now."


class GitAutosyncOptions(BaseModel):
    """Whatever an old row carried."""

    model_config = ConfigDict(frozen=True, extra="allow")


class RemovedGitAutosync:
    name: str = "git-autosync"
    requirements: tuple[str, ...] = ()
    default_events: tuple[HookEventName, ...] = (
        "spawn.finished",
        "work.activated",
        "work.done",
    )
    supported_events: tuple[HookEventName, ...] | None = None
    default_interval: str | None = None
    options_model: type[BaseModel] = GitAutosyncOptions
    identity_options: tuple[str, ...] = ("remote", "path")

    def check_requirements(self) -> tuple[bool, str | None]:
        return True, None

    def execute(self, context: HookContext, config: Hook) -> HookResult:
        warning = _REMOVED + (_LOCAL_ONLY if config.options.get("path") else "")
        return HookResult(
            hook_name=config.name,
            event=context.event_name,
            outcome="skipped",
            success=True,
            skipped=True,
            skip_reason="removed",
            warning=warning,
        )


GIT_AUTOSYNC = RemovedGitAutosync()
