"""Secret parsing and redaction helpers."""

import re

from pydantic import BaseModel, ConfigDict

_SECRET_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class SecretSpec(BaseModel):
    """In-memory secret assignment for one run."""

    model_config = ConfigDict(frozen=True)

    key: str
    value: str


def redact_secrets(text: str, secrets: tuple[SecretSpec, ...]) -> str:
    """Replace secret values with key-specific placeholders."""

    redacted = text
    by_length = sorted(
        (secret for secret in secrets if secret.value),
        key=lambda item: len(item.value),
        reverse=True,
    )
    for secret in by_length:
        redacted = redacted.replace(secret.value, f"[REDACTED:{secret.key}]")
    return redacted


def redact_secret_bytes(data: bytes, secrets: tuple[SecretSpec, ...]) -> bytes:
    """Byte wrapper around redaction for streamed logs."""

    encoded_secrets = tuple(
        secret.value.encode("utf-8")
        for secret in secrets
        if secret.value
    )
    if not encoded_secrets or not any(secret in data for secret in encoded_secrets):
        return data
    decoded = data.decode("utf-8", errors="replace")
    return redact_secrets(decoded, secrets).encode("utf-8")
