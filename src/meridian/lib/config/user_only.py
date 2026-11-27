"""Config sections read from the user's own ``config.toml`` and from nowhere else.

A section here names a program Meridian runs outside the harness sandbox. A project's
``meridian.toml`` is repository content that a sandboxed session can write, and
``--config`` or ``MERIDIAN_CONFIG`` can name such a file, so none of them may supply it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final

from meridian.lib.state.user_paths import get_user_home

USER_ONLY_SECTIONS: Final = frozenset({"message", "inbox"})


def user_config_path() -> Path:
    return get_user_home() / "config.toml"


def is_user_config_file(path: Path) -> bool:
    try:
        return path.resolve() == user_config_path().resolve()
    except OSError:
        return False


def read_user_only_section(name: str) -> object | None:
    """Return ``[name]`` from the user's ``config.toml``, ``None`` when it isn't there.

    Raises ``ValueError`` naming the file when it can't be read or parsed.
    """

    path = user_config_path()
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"can't read {path}: {error}") from None
    return payload.get(name)


__all__ = [
    "USER_ONLY_SECTIONS",
    "is_user_config_file",
    "read_user_only_section",
    "user_config_path",
]
