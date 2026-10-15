"""The shared reference grammar protects user refs from exact-row reads."""

import pytest

from meridian.lib.core.refs import make_run_id, parse_ref, reference_sort_key, run_number, spawn_of


@pytest.mark.parametrize(
    ("text", "kind", "spawn", "key", "number"),
    [
        ("p5", "spawn", "p5", None, None),
        (" p5 ", "spawn", "p5", None, None),
        ("p05", "spawn", "p05", None, None),
        ("p5.1", "run", "p5", "p5", 1),
        ("p5.01", "run", "p5", "p5", 1),
        ("p5.2", "run", "p5", "p5.2", 2),
        ("p5.010", "run", "p5", "p5.10", 10),
        ("c05", "chat", None, None, None),
        ("@latest", "alias", None, None, None),
        *[(value, "native", None, None, None) for value in
          ("P5", "p-x", "r5", "p5.0", "p5.-1", "p\uff15", "p5.2.3", "", "native-uuid")],
    ],
)
def test_reference_corpus(text, kind, spawn, key, number):
    ref = parse_ref(text)
    assert (ref.kind, ref.spawn_id, ref.run_id, ref.run) == (kind, spawn, key, number)


def test_exact_mechanism_keys_keep_legacy_rows_visible():
    assert parse_ref("p-x", row_key=True) == "p-x"
    assert parse_ref("p5.1", row_key=True) == "p5.1"
    assert spawn_of("p5.2") == "p5"
    assert spawn_of("p-x") == "p-x"
    assert sorted(["p-x", "r3", "p5.10", "p5.2", "p2"], key=reference_sort_key) == [
        "p2", "r3", "p5.2", "p5.10", "p-x",
    ]


def test_run_id_composition_and_numbering_are_canonical():
    assert make_run_id("p5", 1) == "p5"
    assert make_run_id("p5", 3) == "p5.3"
    assert run_number(make_run_id("p5", 1)) == 1
    assert run_number(make_run_id("p5", 3)) == 3
    with pytest.raises(ValueError, match="at least 1"):
        make_run_id("p5", 0)
