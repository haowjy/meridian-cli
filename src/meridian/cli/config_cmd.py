"""CLI command handlers for config.* operations."""

from collections.abc import Callable
from functools import partial

from cyclopts import App

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.cli.invocation import Emitter, emit
from meridian.cli.utils import cli_project_root_posix, optional_cli_project_root_posix
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.ops.config import (
    ConfigGetInput,
    ConfigResetInput,
    ConfigSetInput,
    config_get_sync,
    config_reset_sync,
    config_set_sync,
)


def _config_set(emit: Emitter, key: str, value: str) -> None:
    root = cli_project_root_posix()
    emit(config_set_sync(ConfigSetInput(key=key, value=value, project_root=root)))


def _config_get(emit: Emitter, key: str) -> None:
    emit(config_get_sync(ConfigGetInput(key=key, project_root=optional_cli_project_root_posix())))


def _config_reset(emit: Emitter, key: str) -> None:
    emit(config_reset_sync(ConfigResetInput(key=key, project_root=cli_project_root_posix())))


def register_config_commands(app: App) -> None:
    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        # config.init and config.show are auto-generated (no required CLI args).
        "meridian.config.set": lambda: partial(_config_set, emit),
        "meridian.config.get": lambda: partial(_config_get, emit),
        "meridian.config.reset": lambda: partial(_config_reset, emit),
    }
    register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="config",
        handlers=handlers,
    )
