"""Per-harness coordinator env passthrough declarations for child launches.

TODO(P3-bundle-contract): harness-specific env policy belongs under ``harness/``;
this launch-local data module is interim only to avoid bootstrap import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.core.types import HarnessId

_PROVIDER_API_KEYS = frozenset(
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
        "DEEPSEEK_API_KEY",
        "NVIDIA_API_KEY",
        "CEREBRAS_API_KEY",
        "FIREWORKS_API_KEY",
        "TOGETHER_API_KEY",
        "AI_GATEWAY_API_KEY",
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

_CODEX_EXACT = _PROVIDER_API_KEYS | frozenset(
    {
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
    }
)

_PI_PROVIDER_EXACT = frozenset(
    {
        "ANTHROPIC_OAUTH_TOKEN",
        "ANT_LING_API_KEY",
        "ZAI_API_KEY",
        "ZAI_CODING_CN_API_KEY",
        "MINIMAX_API_KEY",
        "MOONSHOT_API_KEY",
        "OPENCODE_API_KEY",
        "KIMI_API_KEY",
        "CLOUDFLARE_API_KEY",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_GATEWAY_ID",
        "XIAOMI_API_KEY",
        "XIAOMI_TOKEN_PLAN_CN_API_KEY",
        "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
        "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_REGION",
    }
)

_PI_EXACT = _PROVIDER_API_KEYS | _PI_PROVIDER_EXACT

_OPENCODE_EXACT = _PROVIDER_API_KEYS | frozenset(
    {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
    }
)

_HARNESS_EXACT: dict[HarnessId, frozenset[str]] = {
    HarnessId.CLAUDE: _CLAUDE_EXACT,
    HarnessId.CODEX: _CODEX_EXACT,
    HarnessId.OPENCODE: _OPENCODE_EXACT,
    HarnessId.CURSOR: frozenset(),
    HarnessId.PI: _PI_EXACT,
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
