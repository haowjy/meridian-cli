import pytest

from meridian.cli.invocation import GlobalOptions, get_global_options, invocation_scope
from meridian.cli.output import OutputConfig


def test_global_options_require_an_invocation_scope() -> None:
    with pytest.raises(RuntimeError, match="outside an invocation scope"):
        get_global_options()


def test_invocation_scope_restores_the_enclosing_options() -> None:
    outer = GlobalOptions(output=OutputConfig(format="text"), harness="claude")
    inner = GlobalOptions(output=OutputConfig(format="json"), harness="codex")

    with invocation_scope(outer):
        assert get_global_options() is outer
        with invocation_scope(inner):
            assert get_global_options() is inner
        assert get_global_options() is outer

    with pytest.raises(RuntimeError, match="outside an invocation scope"):
        get_global_options()
