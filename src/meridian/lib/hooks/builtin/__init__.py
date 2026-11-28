from meridian.lib.hooks.builtin.base import BuiltinHook
from meridian.lib.hooks.builtin.notify import NOTIFY

BUILTIN_HOOKS: dict[str, BuiltinHook] = {
    NOTIFY.name: NOTIFY,
}

__all__ = ["BUILTIN_HOOKS", "BuiltinHook"]
