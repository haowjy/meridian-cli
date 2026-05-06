from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import pytest


@contextmanager
def _isolated_meridian_modules() -> Iterator[None]:
    saved = {
        module_name: module
        for module_name, module in sys.modules.items()
        if module_name == "meridian" or module_name.startswith("meridian.")
    }
    for module_name in tuple(saved):
        del sys.modules[module_name]
    try:
        yield
    finally:
        for module_name in tuple(sys.modules):
            if module_name == "meridian" or module_name.startswith("meridian."):
                del sys.modules[module_name]
        sys.modules.update(saved)


def test_meridian_lib_cold_import_does_not_load_core_exports() -> None:
    with _isolated_meridian_modules():
        importlib.import_module("meridian.lib")

        assert "meridian.lib.core.domain" not in sys.modules
        assert "meridian.lib.core.types" not in sys.modules


@pytest.mark.parametrize(
    ("symbol", "expected_module", "forbidden_module"),
    [
        ("HarnessId", "meridian.lib.core.types", "meridian.lib.core.domain"),
        ("ModelId", "meridian.lib.core.types", "meridian.lib.core.domain"),
        ("SpawnId", "meridian.lib.core.types", "meridian.lib.core.domain"),
        ("Spawn", "meridian.lib.core.domain", None),
    ],
)
def test_meridian_lib_lazy_exports_resolve_expected_symbols(
    symbol: str,
    expected_module: str,
    forbidden_module: str | None,
) -> None:
    with _isolated_meridian_modules():
        module = importlib.import_module("meridian.lib")

        resolved = getattr(module, symbol)

        assert resolved.__name__ == symbol
        assert expected_module in sys.modules
        if forbidden_module is not None:
            assert forbidden_module not in sys.modules


def test_meridian_lib_rejects_unknown_export() -> None:
    with _isolated_meridian_modules():
        module = importlib.import_module("meridian.lib")

        with pytest.raises(AttributeError, match="has no attribute"):
            _ = module.NotARealExport  # type: ignore[attr-defined]
