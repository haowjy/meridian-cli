from __future__ import annotations

from meridian.lib.core.types import HarnessId
from meridian.lib.harness import opencode_idle
from meridian.lib.harness.bundle import get_harness_bundle


def test_opencode_bundle_registers_environment_facts() -> None:
    bundle = get_harness_bundle(HarnessId.OPENCODE)
    facts = bundle.idle_env_facts

    assert bundle.detect_ttl is None
    assert facts is opencode_idle.idle_env_facts
    assert facts is not None
    assert facts({"OPENCODE_DISABLE_AUTOCOMPACT": "1"}) == {
        "harness_autocompact_off": True
    }
