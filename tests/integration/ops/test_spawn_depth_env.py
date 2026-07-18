from pathlib import Path

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.depth import max_depth_reached
from meridian.lib.ops.spawn.execute_init import depth_limits


def test_public_max_depth_caps_internal_depth_counter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", tmp_path.as_posix())
    monkeypatch.setenv("MERIDIAN_MAX_DEPTH", "2")
    monkeypatch.setenv("_MERIDIAN_DEPTH", "2")

    config = load_config(tmp_path, resolve_models=False)
    context = RuntimeContext.from_environment()
    current_depth, max_depth = depth_limits(config.max_depth, ctx=context)

    assert (current_depth, max_depth) == (2, 2)
    assert max_depth_reached(current_depth, max_depth) is True
