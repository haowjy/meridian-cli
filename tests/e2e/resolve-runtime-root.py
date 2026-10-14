"""Print the user-home runtime root for a project at $SMOKE_REPO.

Usage in E2E smoke guides::

    export RUNTIME_ROOT="$(uv run python tests/e2e/resolve-runtime-root.py)"
"""

import os
from pathlib import Path

from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.project_identity import ensure_project_identity
from meridian.lib.state.user_paths import get_project_home

state_dir = resolve_project_paths(Path(os.environ["SMOKE_REPO"])).root_dir
identity = ensure_project_identity(state_dir)
assert identity.id is not None
print(get_project_home(identity.id))
