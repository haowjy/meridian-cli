"""Exhaust the partial-key binding algebra without I/O."""

from itertools import product

from meridian.lib.core.native_identity import NativeKeyFields
from meridian.lib.state.native_binding import Bound, Conflict, Same, bind


def test_partial_binding_is_monotonic_and_conflicts_preserve_both_keys() -> None:
    for values in product((None, "", "first", "other"), repeat=6):
        prior = NativeKeyFields(*values[:3])
        attempted = NativeKeyFields(*values[3:])
        outcome = bind(prior, attempted)
        pairs = list(zip(prior.render().values(), attempted.render().values(), strict=True))
        conflicts = any(kept and candidate and kept != candidate for kept, candidate in pairs)
        if conflicts:
            assert isinstance(outcome, Conflict)
            assert outcome.kept == prior
            assert outcome.attempted == attempted
        else:
            assert isinstance(outcome, (Bound, Same))
            assert outcome.key == NativeKeyFields(*(kept or candidate for kept, candidate in pairs))
            assert isinstance(outcome, Same) == (outcome.key == prior)
            assert bind(outcome.key, prior) == Same(outcome.key)
