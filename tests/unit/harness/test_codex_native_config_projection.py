# qa-validated: mars-launch-bundle-design
"""Codex native-config projection boundary tests."""

from meridian.lib.harness.projections.project_codex_common import (
    project_codex_native_config_flags,
)


def test_project_codex_native_config_flags_serializes_supported_values_in_key_order() -> None:
    flags, warnings = project_codex_native_config_flags(
        {
            "model": "gpt-5",
            "temperature": 0.7,
            "allowed_tools": ["Bash", "Read"],
            "sandbox_workspace_write.network_access": True,
        }
    )

    assert warnings == ()
    assert flags == (
        "-c",
        'allowed_tools=["Bash","Read"]',
        "-c",
        'model="gpt-5"',
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-c",
        "temperature=0.7",
    )


def test_project_codex_native_config_flags_warns_and_skips_nested_maps() -> None:
    flags, warnings = project_codex_native_config_flags(
        {
            "permissions": {"allow": ["Bash(*)"]},
            "sandbox_workspace_write.network_access": True,
        }
    )

    assert flags == ("-c", "sandbox_workspace_write.network_access=true")
    assert warnings == (
        "native-config key 'permissions' has nested map value; "
        "Codex -c requires scalar/array values or dotted keys. Skipped.",
    )
