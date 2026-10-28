import pytest

from meridian.lib.core.refs import Ref, parse_ref

_UUID = "0b9f3c1e-6c55-4a0e-9a8f-2d1b7f3e9c42"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("p123", Ref("spawn", "p123")),
        ("c9691", Ref("chat", "c9691")),
        ("@latest", Ref("selector", "@latest")),
        ("@last-failed", Ref("selector", "@last-failed")),
        ("@last-completed", Ref("selector", "@last-completed")),
        (_UUID, Ref("native", _UUID)),
        # Unicode digits are not ID digits: they fall through to native.
        ("p²", Ref("native", "p²")),
        ("p٣", Ref("native", "p٣")),
        ("c\u0661", Ref("native", "c\u0661")),
        (" p5 ", Ref("spawn", "p5")),
    ],
)
def test_parse_ref_shapes(raw: str, expected: Ref) -> None:
    assert parse_ref(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("@foo", "@last-completed, @last-failed, @latest"),
        ("", "A reference is required."),
        ("   ", "A reference is required."),
    ],
)
def test_parse_ref_errors(raw: str, message: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_ref(raw)
    assert message in str(exc.value)
