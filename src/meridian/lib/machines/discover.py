"""Find Meridian on another machine: one fixed script, sent to ``/bin/sh -s`` on stdin.

The user's login shell never parses it, and nothing the user typed is in it. SSH commands get
a short ``PATH``, so ``command -v`` finds little; the fixed places find a ``uv tool install``.
Only candidates that are absolute paths of ``[A-Za-z0-9._+/-]`` are run, because the chosen
path is later put on a command line that a shell of unknown dialect reads.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meridian.lib.machines.results import NotSent
from meridian.lib.machines.ssh import TIMED_OUT, SshProcess, failure_kind, failure_text
from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.state.user_paths import get_user_home
from meridian.lib.utils.json_shapes import json_object

# Found by `<path> machine run --check`: a build with no `machine run` fails it.
CHECK_WORD: Final = "meridian-machine-run"
SAFE_PATH_RE: Final = re.compile(r"/[A-Za-z0-9._+/-]+")
DISCOVERY_SECONDS: Final = 60.0

_SCRIPT: Final = f"""set -f
candidates() {{
  command -v "$1"
  for dir in "$HOME/.local/bin" /opt/homebrew/bin /usr/local/bin "$HOME/.cargo/bin"; do
    echo "$dir/$1"
  done
}}
candidates uv | while IFS= read -r uv; do
  case "$uv" in /*) ;; *) continue ;; esac
  [ -x "$uv" ] || continue
  case "$uv" in *[!A-Za-z0-9._+/-]*) echo "meridian-badpath $uv"; continue ;; esac
  echo "meridian-uv $uv"
  break
done
candidates meridian | while IFS= read -r path; do
  case "$path" in /*) ;; *) continue ;; esac
  [ -x "$path" ] || continue
  case "$path" in *[!A-Za-z0-9._+/-]*) echo "meridian-badpath $path"; continue ;; esac
  out=$("$path" machine run --check </dev/null 2>/dev/null)
  if [ "$out" = "{CHECK_WORD}" ]; then
    echo "meridian-found $path"
    break
  fi
  echo "meridian-old $path $("$path" --version </dev/null 2>/dev/null | head -n 1)"
done
exit 0
"""


@dataclass(frozen=True)
class Discovery:
    path: str | None
    """The first ``meridian`` that passed ``machine run --check``."""
    old: tuple[str, str] | None
    """The first one that didn't: its path and its version text."""
    uv: str | None
    bad_path: str | None
    """A candidate whose path has a character the rule refuses."""


def discover(target: str, label: str | None = None) -> Discovery | NotSent:
    """Run the discovery script on *target* with background SSH options."""

    label = label or target
    process = SshProcess(target, ("/bin/sh", "-s"))
    with suppress(OSError):
        process.write(_SCRIPT.encode("utf-8"))
    process.close_stdin()
    deadline = time.monotonic() + DISCOVERY_SECONDS
    lines: list[str] = []
    while True:
        line = process.read_line(deadline)
        if line is None:
            break
        if line is TIMED_OUT:
            process.kill()
            return NotSent(
                "unreachable", f"{target} did not answer within {DISCOVERY_SECONDS:.0f} s"
            )
        assert isinstance(line, bytes)
        lines.append(line.decode("utf-8", "replace").rstrip("\r\n"))
    exited = process.finish()
    if exited.status != 0:
        kind = failure_kind(exited.stderr) if exited.status == 255 else "failed"
        last = exited.last_line or f"ssh exited {exited.status}"
        return NotSent(kind, failure_text(kind, label, target, last))
    path = old = uv = bad = None
    for line in lines:
        marker, _, rest = line.partition(" ")
        if marker == "meridian-found" and path is None and SAFE_PATH_RE.fullmatch(rest):
            path = rest
        elif marker == "meridian-old" and old is None:
            old_path, _, version = rest.partition(" ")
            old = (old_path, version.strip())
        elif marker == "meridian-uv" and uv is None and SAFE_PATH_RE.fullmatch(rest):
            uv = rest
        elif marker == "meridian-badpath" and bad is None:
            bad = rest
    return Discovery(path, old, uv, bad)


def _cache_file(machine_id: str) -> Path:
    return get_user_home() / "cache" / "machines" / f"{machine_id}.json"


def read_cached_path(machine_id: str) -> str | None:
    """The cached path of Meridian on a machine, if it is a safe one; a missing or wrong cache
    is rediscovered, never needed for correctness."""

    try:
        document = json.loads(_cache_file(machine_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fields = json_object(document)
    path = fields.get("path") if fields is not None else None
    return path if isinstance(path, str) and SAFE_PATH_RE.fullmatch(path) else None


def write_cache(machine_id: str, *, path: str, version: str) -> None:
    file = _cache_file(machine_id)
    if read_cached_path(machine_id) == path:
        return
    file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(file, json.dumps({"path": path, "version": version}) + "\n")


def forget_cache(machine_id: str) -> None:
    _cache_file(machine_id).unlink(missing_ok=True)


__all__ = [
    "CHECK_WORD",
    "SAFE_PATH_RE",
    "Discovery",
    "discover",
    "forget_cache",
    "read_cached_path",
    "write_cache",
]
