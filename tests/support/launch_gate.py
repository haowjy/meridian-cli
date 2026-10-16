"""Descriptor placement for isolated gate protocol probes."""

import os
from unittest.mock import patch

from meridian.lib.platform.launch_gate import LaunchGate


def gate_at_fd(target: int) -> LaunchGate:
    read_fd, write_fd = os.pipe()
    if write_fd == target:
        moved = os.dup(write_fd)
        os.close(write_fd)
        write_fd = moved
    if read_fd != target:
        os.dup2(read_fd, target, inheritable=False)
        os.close(read_fd)
    with patch("meridian.lib.platform.launch_gate.os.pipe", return_value=(target, write_fd)):
        return LaunchGate()
