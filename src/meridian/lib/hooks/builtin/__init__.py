"""Builtin hook registry."""

from meridian.lib.hooks.builtin.base import BuiltinHook
from meridian.lib.hooks.builtin.git_autosync import GIT_AUTOSYNC
from meridian.lib.hooks.builtin.notify import NOTIFY

BUILTIN_HOOKS: dict[str, BuiltinHook] = {
    GIT_AUTOSYNC.name: GIT_AUTOSYNC,
    NOTIFY.name: NOTIFY,
}

__all__ = ["BUILTIN_HOOKS", "BuiltinHook"]
