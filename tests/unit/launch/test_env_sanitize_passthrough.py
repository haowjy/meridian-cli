"""Regression tests for child-env sanitization allowlists and harness passthrough."""

from __future__ import annotations

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.launch.env_sanitize import (
    build_connection_child_env,
    collect_child_env_passthrough,
    sanitize_child_env,
)
from meridian.lib.launch.harness_env_passthrough import harness_env_passthrough

_ALL_HARNESS_IDS = tuple(HarnessId)


def _corporate_proxy_env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "HOME": "/home/tester",
        "HTTPS_PROXY": "http://proxy.corp:8080",
        "NO_PROXY": "localhost,.corp",
        "SSL_CERT_FILE": "/etc/ssl/corp-ca.pem",
        "http_proxy": "http://proxy.corp:8080",
    }


def test_claude_spawn_keeps_auth_token_and_drops_unrelated_secrets() -> None:
    base_env = {
        "PATH": "/usr/bin",
        "HOME": "/home/tester",
        "ANTHROPIC_API_KEY": "key-1",
        "ANTHROPIC_AUTH_TOKEN": "token-1",
        "ANTHROPIC_BASE_URL": "https://api.example.com",
        "FOO_API_KEY": "drop-me",
        "MY_SECRET": "drop-me-too",
    }
    child_env = build_connection_child_env(
        harness_id=HarnessId.CLAUDE,
        base_env=base_env,
        env_overrides=None,
    )

    assert child_env["ANTHROPIC_API_KEY"] == "key-1"
    assert child_env["ANTHROPIC_AUTH_TOKEN"] == "token-1"
    assert child_env["ANTHROPIC_BASE_URL"] == "https://api.example.com"
    assert "FOO_API_KEY" not in child_env
    assert "MY_SECRET" not in child_env


@pytest.mark.parametrize("harness_id", _ALL_HARNESS_IDS)
def test_corporate_proxy_and_cert_vars_reach_every_harness(harness_id: HarnessId) -> None:
    child_env = build_connection_child_env(
        harness_id=harness_id,
        base_env=_corporate_proxy_env(),
        env_overrides=None,
    )

    assert child_env["HTTPS_PROXY"] == "http://proxy.corp:8080"
    assert child_env["NO_PROXY"] == "localhost,.corp"
    assert child_env["SSL_CERT_FILE"] == "/etc/ssl/corp-ca.pem"
    assert child_env["http_proxy"] == "http://proxy.corp:8080"


def test_windows_baseline_vars_survive_sanitization() -> None:
    base_env = {
        "PATH": "C:\\Windows\\System32",
        "USERPROFILE": "C:\\Users\\alice",
        "APPDATA": "C:\\Users\\alice\\AppData\\Roaming",
        "LOCALAPPDATA": "C:\\Users\\alice\\AppData\\Local",
        "SystemRoot": "C:\\Windows",
        "COMSPEC": "C:\\Windows\\System32\\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT",
        "TEMP": "C:\\Users\\alice\\AppData\\Local\\Temp",
        "TMP": "C:\\Users\\alice\\AppData\\Local\\Temp",
        "FOO_API_KEY": "drop-me",
    }
    sanitized = sanitize_child_env(
        base_env=base_env,
        env_overrides=None,
        pass_through=harness_env_passthrough(HarnessId.CODEX),
    )

    assert sanitized["USERPROFILE"] == "C:\\Users\\alice"
    assert sanitized["APPDATA"] == "C:\\Users\\alice\\AppData\\Roaming"
    assert sanitized["SystemRoot"] == "C:\\Windows"
    assert sanitized["COMSPEC"] == "C:\\Windows\\System32\\cmd.exe"
    assert "FOO_API_KEY" not in sanitized


def test_codex_openai_base_url_and_azure_prefix_pass() -> None:
    passthrough = collect_child_env_passthrough(harness_id=HarnessId.CODEX)
    sanitized = sanitize_child_env(
        base_env={
            "PATH": "/usr/bin",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
            "OPENAI_ORG_ID": "org-123",
            "RANDOM_API_KEY": "drop-me",
        },
        env_overrides=None,
        pass_through=passthrough,
    )

    assert sanitized["OPENAI_BASE_URL"] == "https://api.openai.com/v1"
    assert sanitized["AZURE_OPENAI_ENDPOINT"] == "https://example.openai.azure.com"
    assert sanitized["OPENAI_ORG_ID"] == "org-123"
    assert "RANDOM_API_KEY" not in sanitized


def test_pi_node_runtime_prefixes_pass() -> None:
    passthrough = collect_child_env_passthrough(harness_id=HarnessId.PI)
    sanitized = sanitize_child_env(
        base_env={
            "PATH": "/usr/bin",
            "NODE_OPTIONS": "--max-old-space-size=4096",
            "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org",
            "PNPM_HOME": "/home/tester/.local/share/pnpm",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "CUSTOM_TOKEN": "drop-me",
        },
        env_overrides=None,
        pass_through=passthrough,
    )

    assert sanitized["NODE_OPTIONS"] == "--max-old-space-size=4096"
    assert sanitized["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org"
    assert sanitized["PNPM_HOME"] == "/home/tester/.local/share/pnpm"
    assert sanitized["COREPACK_ENABLE_DOWNLOAD_PROMPT"] == "0"
    assert "CUSTOM_TOKEN" not in sanitized
