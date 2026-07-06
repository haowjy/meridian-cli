"""Per-harness coordinator env passthrough declarations for child launches.

TODO(P3-bundle-contract): harness-specific env policy belongs under ``harness/``;
this launch-local data module is interim only to avoid bootstrap import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.core.types import HarnessId

_COMMON_PROVIDER_CREDENTIALS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "MISTRAL_API_KEY",
        "COHERE_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
    }
)

_CLAUDE_EXACT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
    }
)

_CODEX_EXACT = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
    }
)

_HARNESS_EXACT: dict[HarnessId, frozenset[str]] = {
    HarnessId.CLAUDE: _CLAUDE_EXACT,
    HarnessId.CODEX: _CODEX_EXACT,
    HarnessId.OPENCODE: _COMMON_PROVIDER_CREDENTIALS,
    HarnessId.CURSOR: frozenset(),
    HarnessId.PI: _COMMON_PROVIDER_CREDENTIALS,
}

_HARNESS_PREFIXES: dict[HarnessId, tuple[str, ...]] = {
    HarnessId.CLAUDE: ("CLAUDE_", "CLAUDE_CODE_", "ANTHROPIC_VERTEX_"),
    HarnessId.CODEX: ("CODEX_", "AZURE_OPENAI_", "OPENAI_ORG"),
    HarnessId.OPENCODE: ("OPENCODE_",),
    HarnessId.CURSOR: ("CURSOR_",),
    HarnessId.PI: ("PI_", "AZURE_OPENAI_", "OPENAI_ORG"),
}


@dataclass(frozen=True)
class HarnessEnvPassthrough:
    """Coordinator env keys one harness may inherit beyond the default allowlist."""

    exact: frozenset[str] = frozenset()
    prefixes: tuple[str, ...] = ()


def harness_env_passthrough(harness_id: HarnessId) -> HarnessEnvPassthrough:
    """Return declared passthrough for one harness."""

    return HarnessEnvPassthrough(
        exact=_HARNESS_EXACT.get(harness_id, frozenset()),
        prefixes=_HARNESS_PREFIXES.get(harness_id, ()),
    )


__all__ = ["HarnessEnvPassthrough", "harness_env_passthrough"]
