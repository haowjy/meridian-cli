"""Work item naming policy: normalization and the 64-character bound."""

from meridian.lib.state.work_state import slugify


def test_slugify_normalizes_and_truncates() -> None:
    assert slugify("Hello_world  2026!!!") == "hello-world-2026"
    assert slugify("___") == ""
    assert slugify("a" * 80) == "a" * 64
