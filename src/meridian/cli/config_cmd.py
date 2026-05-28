"""CLI command handlers for config.* operations."""

from collections.abc import Callable
from functools import partial
from typing import Any

from cyclopts import App

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.cli.utils import implicit_ops_payload
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.ops.config import (
    ConfigGetInput,
    ConfigResetInput,
    ConfigSetInput,
    config_get_sync,
    config_reset_sync,
    config_set_sync,
)

Emitter = Callable[[Any], None]


def _config_set(emit: Emitter, key: str, value: str) -> None:
    emit(config_set_sync(ConfigSetInput(key=key, value=value, **implicit_ops_payload())))


def _config_get(emit: Emitter, key: str) -> None:
    emit(config_get_sync(ConfigGetInput(key=key, **implicit_ops_payload())))


def _config_reset(emit: Emitter, key: str) -> None:
    emit(config_reset_sync(ConfigResetInput(key=key, **implicit_ops_payload())))


def register_config_commands(app: App, emit: Emitter) -> tuple[set[str], dict[str, str]]:
    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        # config.init and config.show are auto-generated (no required CLI args).
        "meridian.config.set": lambda: partial(_config_set, emit),
        "meridian.config.get": lambda: partial(_config_get, emit),
        "meridian.config.reset": lambda: partial(_config_reset, emit),
    }
    return register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="config",
        handlers=handlers,
        emit=emit,
    )
