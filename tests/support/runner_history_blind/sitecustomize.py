"""Pytest subprocess bootstrap for ``--runner-history=off``."""

import sys

from patches import install_runner_history_blind


def _is_meridian_entrypoint() -> bool:
    argv0 = sys.argv[0]
    return argv0.rsplit("/", 1)[-1] == "meridian" or argv0.endswith("/meridian/__main__.py")


install_runner_history_blind(patch_writers=_is_meridian_entrypoint())
