"""CLI command handlers for models.* operations."""

import sys
from collections.abc import Callable
from functools import partial
from typing import Any

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.lib.extensions.registry import get_first_party_registry

Emitter = Callable[[Any], None]

_MODELS_LIST_REDIRECT = (
    "`meridian models list` has moved to Mars.\nUse `meridian mars models list` instead."
)


def _models_list_stub(
    emit: Emitter,
    all: bool = False,
    show_superseded: bool = False,
) -> None:
    _ = emit, all, show_superseded
    print(_MODELS_LIST_REDIRECT, file=sys.stderr)
    raise SystemExit(1)


def maybe_handle_models_redirect(argv: list[str]) -> None:
    """Fast-path removed `meridian models list` before primary-launch parsing."""
    if len(argv) >= 2 and argv[0] == "models" and argv[1] == "list":
        print(_MODELS_LIST_REDIRECT, file=sys.stderr)
        raise SystemExit(1)


def register_models_commands(app: Any, emit: Emitter) -> tuple[set[str], dict[str, str]]:
    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        "meridian.models.list": lambda: partial(_models_list_stub, emit),
    }
    return register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="models",
        handlers=handlers,
        emit=emit,
        default_handler=partial(_models_list_stub, emit),
    )
