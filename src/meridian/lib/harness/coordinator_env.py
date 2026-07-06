"""Per-harness coordinator env passthrough declarations for child launches."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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


class CoordinatorEnvPassthrough(BaseModel):
    """Coordinator env keys one harness may inherit beyond the default allowlist."""

    model_config = ConfigDict(frozen=True)

    exact: frozenset[str] = Field(default_factory=frozenset)
    prefixes: tuple[str, ...] = ()


CLAUDE_COORDINATOR_ENV = CoordinatorEnvPassthrough(
    exact=_CLAUDE_EXACT,
    prefixes=("CLAUDE_", "CLAUDE_CODE_", "ANTHROPIC_VERTEX_"),
)

CODEX_COORDINATOR_ENV = CoordinatorEnvPassthrough(
    exact=_CODEX_EXACT,
    prefixes=("CODEX_", "AZURE_OPENAI_", "OPENAI_ORG"),
)

OPENCODE_COORDINATOR_ENV = CoordinatorEnvPassthrough(
    exact=_OPENCODE_EXACT,
    prefixes=("OPENCODE_",),
)

CURSOR_COORDINATOR_ENV = CoordinatorEnvPassthrough()

PI_COORDINATOR_ENV = CoordinatorEnvPassthrough(
    exact=_PI_EXACT,
    prefixes=("PI_", "AZURE_OPENAI_", "OPENAI_ORG"),
)


def coordinator_env_passthrough_for(harness_id: HarnessId) -> CoordinatorEnvPassthrough:
    """Return declared passthrough for one harness from its registered contract."""

    from meridian.lib.harness.bundle import get_harness_bundle

    return get_harness_bundle(harness_id).adapter.contract.coordinator_env_passthrough


__all__ = [
    "CLAUDE_COORDINATOR_ENV",
    "CODEX_COORDINATOR_ENV",
    "CURSOR_COORDINATOR_ENV",
    "OPENCODE_COORDINATOR_ENV",
    "PI_COORDINATOR_ENV",
    "CoordinatorEnvPassthrough",
    "coordinator_env_passthrough_for",
]
