"""JSON shape helper contracts."""

from meridian.lib.utils.json_shapes import loads_json_object


def test_loads_json_object_rejects_oversized_integer() -> None:
    text = '{"model":' + "1" * 5000 + "}"

    assert loads_json_object(text) is None
