"""Byte-preservation contracts for secret redaction."""

from meridian.lib.safety.redaction import SecretSpec, redact_secret_bytes


def test_nonmatching_redaction_preserves_invalid_utf8_bytes() -> None:
    data = b"prefix\xffsuffix"
    secrets = (SecretSpec(key="TOKEN", value="not-present"),)

    assert redact_secret_bytes(data, secrets) == data


def test_matching_secret_is_still_redacted_from_invalid_utf8_bytes() -> None:
    data = b"prefix\xffsecret-suffix"
    secrets = (SecretSpec(key="TOKEN", value="secret"),)

    assert redact_secret_bytes(data, secrets) == b"prefix\xef\xbf\xbd[REDACTED:TOKEN]-suffix"
