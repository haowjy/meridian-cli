"""Tool rule types shared by the launch bundle, persisted snapshots and the harness compiles."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

type ToolAction = Literal["allow", "deny", "ask"]
# The pre-rules shape of a profile's tools: native key -> action. Only old persisted
# launch snapshots still hold it; ``legacy_tool_rules`` is the one reader.
type ToolsField = dict[str, ToolAction]
type NativeStatus = Literal["known", "absent", "unknown"]

LEGACY_SNAPSHOT_ORIGIN = "legacy-snapshot"
PLATFORM_ORIGIN = "platform"


class ToolRule(BaseModel):
    """One tool rule as Mars resolved it for the launch's harness.

    ``capability`` and ``pattern`` carry meaning (what the profile wrote). Mars gives two
    native facts, and nothing derives one from the other or from the capability:

    - ``native``: the permission-rule spellings, already scoped by ``pattern``. A compile
      emits these as permission rules.
    - ``native_tools``: the unscoped harness tools the rule concerns. A compile reads these
      to decide which tools a rule leaves callable or removes (``--tools``, exclusions,
      gating). They differ where the harness scopes a tool through another's permission:
      Claude's ``write(src/*)`` is ``Edit(src/*)`` but concerns ``Write``.

    ``native_status``: ``known`` means ``native`` names the rule; ``absent`` means the
    harness has no such tool; ``unknown`` means Mars has no name for it (a gap to report);
    ``note`` can explain that gap. The ``*`` rule names no tool: capability ``*`` and no
    ``native``.

    ``capability`` is ``None`` only for rules read from a launch snapshot an older build
    wrote: those carry native keys alone, so a harness can emit them but not interpret them.
    ``native_tools`` is ``None`` only for rules a launch snapshot kept from before Mars gave
    them; ``tools`` then reads them from ``native``.
    """

    model_config = ConfigDict(frozen=True)

    capability: str | None
    pattern: str | None = None
    action: ToolAction
    native: tuple[str, ...] = ()
    native_tools: tuple[str, ...] | None = None
    native_status: NativeStatus = "known"
    origin: str | None = None
    note: str | None = None

    @property
    def is_wildcard(self) -> bool:
        return self.capability == "*"

    @property
    def is_scoped(self) -> bool:
        """Whether the rule covers only some calls of its tools.

        Read from ``native``, which a legacy snapshot rule (no ``pattern``) also scopes.
        """

        return any(split_native(native)[1] is not None for native in self.native)

    @property
    def tools(self) -> tuple[str, ...]:
        """The harness tools this rule leaves callable or removes, by its action.

        ``native_tools``; for a rule persisted without them, each ``native`` name unscoped.
        That fallback is right wherever the permission spelling names its own tool, and
        wrong where it names another's (Claude's ``Edit(path)`` for a scoped write).
        """

        if self.native_tools is not None:
            return self.native_tools
        return tuple(dict.fromkeys(split_native(native)[0] for native in self.native))


def split_native(name: str) -> tuple[str, str | None]:
    """Split a native ``tool(scope)`` name into the tool and its scope (``None`` if unscoped).

    The one reader of the spelling: Mars writes it for every harness, and Claude's own
    permission strings (passthrough grants, emitted channels) share it.
    """

    tool, paren, rest = name.partition("(")
    if paren and tool and rest.endswith(")"):
        return tool, rest[:-1]
    return name, None


def legacy_tool_rules(tools: ToolsField | None) -> tuple[ToolRule, ...]:
    """Read an old snapshot's tools map as native-only rules (no capability).

    The map's ``*`` key was always the default action, not a tool name, so it stays the
    ``*`` rule.
    """

    return tuple(
        ToolRule(
            capability="*" if key == "*" else None,
            action=action,
            native=() if key == "*" else (key,),
            origin=LEGACY_SNAPSHOT_ORIGIN,
        )
        for key, action in (tools or {}).items()
    )


def legacy_platform_tool_rules() -> tuple[ToolRule, ...]:
    """Platform denies no source could name, so the report can say they are not enforced.

    Only a replayed launch snapshot an older build wrote lacks the names, and only when Mars
    could not build a bundle to supply them.
    """

    note = (
        "persisted launch snapshot predates tools.capability_natives and Mars gave no "
        "bundle to name them, so it is not enforced"
    )
    return tuple(
        ToolRule(
            capability=capability,
            action="deny",
            native_status="unknown",
            origin=PLATFORM_ORIGIN,
            note=note,
        )
        for capability in ("agent", "task")
    )


def unscoped_action(rules: Iterable[ToolRule], capability: str) -> ToolAction | None:
    """The action of the unscoped rule naming exactly ``capability``; the last one wins."""

    action: ToolAction | None = None
    for rule in rules:
        if rule.capability == capability and rule.pattern is None:
            action = rule.action
    return action
