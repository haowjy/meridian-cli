from __future__ import annotations

import subprocess
from typing import Any

from meridian.lib.platform import IS_WINDOWS


def detached_popen_kwargs() -> dict[str, Any]:
    """``subprocess.Popen`` kwargs for a child that outlives its parent and its terminal.

    The caller still chooses the child's stdio: a child that keeps an inherited pipe
    holds its reader open until it exits.
    """

    if IS_WINDOWS:
        create_new_process_group = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0))
        return {"creationflags": create_new_process_group | detached_process}
    return {"start_new_session": True, "close_fds": True}


__all__ = ["detached_popen_kwargs"]
