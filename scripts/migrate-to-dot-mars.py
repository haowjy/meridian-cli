#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["tomlkit>=0.12"]
# ///
"""Move a Mars 0.40 / Meridian 0.28 setup into the Mars 0.41 user level (~/.mars).

Run once, with Mars 0.41.0 or later installed (Meridian 0.29.0 or later bundles it):

    uv run https://raw.githubusercontent.com/haowjy/meridian-cli/main/scripts/migrate-to-dot-mars.py
    uv run .../migrate-to-dot-mars.py --apply

Without --apply it only prints the steps, with real paths. It moves:

  - a home project (~/mars.toml, ~/mars.local.toml, ~/mars.lock): `mars --root ~ unlink`s each
    target its lock records (~/.claude, ~/.agents, ...), carries both manifests (without
    `targets`, relative paths made absolute) into the user level's mars.toml and
    mars.local.toml, moves ~/.mars-src in, and deletes the home project and its old store;
  - Mars 0.40's config.toml and curated.toml into the user level: from $MARS_CONFIG_DIR, else
    $XDG_CONFIG_HOME/mars or ~/.config/mars on Linux, ~/Library/Application Support/mars on
    macOS (Mars 0.40 ignored XDG_CONFIG_HOME there). On macOS a ~/.config/mars file, which
    Mars never read, is moved only into a user level that has none;
  - Meridian 0.28's ~/.meridian/mars ($MERIDIAN_HOME/mars): its dependencies into mars.toml,
    its .mars-src items into the user level's, its [settings], [models], [agents] and [skills]
    into config.toml; then deletes it.

The user level is $MARS_HOME, else $MARS_CONFIG_DIR, else ~/.mars. Before changing anything the
run has Mars read the files it would write, and it stops when both sides hold one name (a
dependency, a .mars-src item, a setting, curated.toml) with different content. Then it runs
`mars --user sync`. A re-run skips what is done.

Backup: before changing anything, --apply copies every path it will move, rewrite or delete,
the user level folder included, into a new folder ~/.mars-migration-backup/<UTC time>/ (paths
under home in home/, any others in root/), including the harness config files whose hook and
MCP entries `unlink` removes. A folder ending in .partial did not finish; its run changed
nothing.

Roll back (to Mars 0.40 / Meridian 0.28) from the oldest complete backup of this migration:

    rm -rf ~/.mars                                     # the user level ($MARS_HOME if set)
    cp -a ~/.mars-migration-backup/<time>/home/. ~/
    cp -a ~/.mars-migration-backup/<time>/root/. /     # only if it has root/

The run prints these commands with its real paths at the end.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import Item

MIN_MARS = (0, 41, 0)
# What a user level holds besides what `mars --user sync` writes: in a ~/.mars shared with the
# home project's store, everything else is that store's.
USER_LEVEL_FILES = frozenset(
    {"mars.toml", "mars.local.toml", "mars.lock", "config.toml", "curated.toml", ".mars-src"}
)
DEPENDENCY_TABLES = ("dependencies", "local-dependencies")
# Tables whose entries carry a `path` relative to the manifest's folder.
PATH_TABLES = (*DEPENDENCY_TABLES, "overrides")
# What Meridian 0.28's root held that now belongs in the user config.toml.
CONFIG_SECTIONS = ("settings", "models", "agents", "skills")
# The files under home where Mars 0.40 and 0.41 merge a target's hook, MCP and plugin entries
# (`src/target/table.rs`, `mars-schema/src/hooks/targets.rs`, unchanged between them): `unlink`
# edits them, so the backup holds them.
ENTRY_FILES = {
    ".claude": (".claude/settings.local.json", ".mcp.json"),
    ".codex": (".codex/hooks.json", ".codex/config.toml"),
    ".opencode": (".opencode/opencode.json",),
    ".pi": (".pi/mcp.json",),
    ".agy": (".agy/hooks.json", ".agy/mcp_config.json", ".agy/plugin.json", ".agents/plugins.json"),
}
# Settings a user-level manifest must not have.
TARGET_KEYS = ("targets", "managed_root")


def real(path: Path) -> Path:
    """`path` with every symlink resolved, so two spellings of one folder compare equal."""
    return Path(os.path.realpath(path))


@dataclass(frozen=True)
class Paths:
    home: Path
    user: Path
    meridian_root: Path
    # Where Mars 0.40 read config.toml and curated.toml.
    old_config: Path
    # macOS: ~/.config/mars, which Mars never read; None elsewhere.
    unread_config: Path | None


def locate() -> Paths:
    if not Path.home().is_absolute():
        raise SystemExit("HOME is not an absolute path; set it and run again.")
    home = real(Path.home())

    def env_dir(name: str) -> Path | None:
        value = os.environ.get(name, "").strip()
        return real(Path(value).expanduser()) if value else None

    # Mars 0.41 reads Mars 0.40's MARS_CONFIG_DIR as MARS_HOME while MARS_HOME is unset.
    config_dir = env_dir("MARS_CONFIG_DIR")
    user = env_dir("MARS_HOME") or config_dir or home / ".mars"
    meridian_root = (env_dir("MERIDIAN_HOME") or home / ".meridian") / "mars"
    unread = None
    if config_dir is not None:
        old_config = config_dir
    elif sys.platform == "darwin":
        old_config = home / "Library" / "Application Support" / "mars"
        unread = home / ".config" / "mars"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        old_config = (real(Path(xdg)) if xdg.startswith("/") else home / ".config") / "mars"
    return Paths(home, user, meridian_root, old_config, unread)


# --- Mars ------------------------------------------------------------------------------------


def find_mars(explicit: str | None) -> Path | None:
    """Mars beside the `meridian` on PATH (the one it runs), else Mars on PATH."""
    if explicit:
        return Path(explicit)
    meridian = shutil.which("meridian")
    if meridian:
        beside = Path(meridian).resolve().parent / "mars"
        if os.access(beside, os.X_OK):
            return beside
    found = shutil.which("mars")
    return Path(found) if found else None


def mars_problem(mars: Path | None) -> str | None:
    if mars is None:
        return "no `mars` found beside `meridian` or on PATH; pass --mars PATH"
    try:
        out = subprocess.run(
            [str(mars), "--version"], capture_output=True, text=True, check=False, timeout=60
        ).stdout
    except OSError as exc:
        return f"{mars} does not run ({exc})"
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    version = tuple(int(part) for part in match.groups()) if match else None
    if version is None or version < MIN_MARS:
        found = ".".join(map(str, version)) if version else "unknown"
        return f"{mars} is Mars {found}; this needs 0.41.0 or later (upgrade Meridian)"
    return None


class StepError(Exception):
    pass


def run_mars(mars: Path, *args: str, show: bool = True, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        [str(mars), *args], capture_output=True, text=True, check=False, env=env
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise StepError(f"`{mars} {shlex.join(args)}` exited {result.returncode}:\n{output}")
    if show:
        for line in output.splitlines():
            print(f"      {line}")


def check_with_mars(paths: Paths, mars: Path, files: dict[str, str]) -> str | None:
    """Why Mars cannot use these user-level files, placed in a scratch user level; None when
    it can. `validate` reads mars.toml, mars.local.toml and config.toml and resolves what
    `sync` would; curated.toml is copied as is, its format unchanged since Mars 0.40."""
    if not {"mars.toml", "mars.local.toml", "config.toml"} & files.keys():
        return None
    with tempfile.TemporaryDirectory(prefix="migrate-to-dot-mars-") as scratch:
        user = Path(scratch) / "home" / ".mars"
        user.mkdir(parents=True)
        for name, text in files.items():
            (user / name).write_text(text, encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if key != "MARS_CONFIG_DIR"}
        env |= {"HOME": str(user.parent), "MARS_HOME": str(user)}
        # The source cache `sync` uses, so a dependency is not fetched again.
        if "MARS_CACHE_DIR" not in env:
            xdg = env.get("XDG_CACHE_HOME", "")
            cache = (
                paths.home / "Library" / "Caches"
                if sys.platform == "darwin"
                else Path(xdg)
                if xdg.startswith("/")
                else paths.home / ".cache"
            )
            env["MARS_CACHE_DIR"] = str(cache / "mars" / "cache")
        try:
            run_mars(mars, "--user", "validate", show=False, env=env)
        except StepError as exc:
            return str(exc).replace(str(user), "<user level>")
    return None


# --- files -----------------------------------------------------------------------------------


def exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def parse(path: Path, problems: list[str]) -> Any:
    text = read_text(path)
    if text is None:
        return None
    try:
        return tomlkit.parse(text)
    except Exception as exc:
        problems.append(f"{path} cannot be parsed ({exc}); fix it and run again.")
        return None


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.migrate-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif exists(path):
        path.unlink()


def same_tree(a: Path, b: Path) -> bool:
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    if a.is_dir() and b.is_dir():
        names = sorted(p.name for p in a.iterdir())
        if names != sorted(p.name for p in b.iterdir()):
            return False
        return all(same_tree(a / name, b / name) for name in names)
    if a.is_file() and b.is_file():
        return a.read_bytes() == b.read_bytes()
    return False


def src_items(folder: Path) -> list[Path]:
    """A `.mars-src/` folder's items, relative to it: `skills/<name>`, `agents/<name>.md`, …"""
    if not folder.is_dir():
        return []
    items: list[Path] = []
    for entry in sorted(folder.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            items += [Path(entry.name, child.name) for child in sorted(entry.iterdir())]
        else:
            items.append(Path(entry.name))
    return items


class Backup:
    """One run's copy of everything it changes. It counts only once complete: it is written
    as `<name>.partial/` and renamed to `<name>/` when every path is in."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.parent = home / ".mars-migration-backup"
        self.root: Path | None = None  # set once complete

    def write(self, paths: list[Path]) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name, count = stamp, 1
        while exists(self.parent / name) or exists(self.parent / f"{name}.partial"):
            count += 1
            name = f"{stamp}-{count}"
        partial = self.parent / f"{name}.partial"
        partial.mkdir(parents=True)
        try:
            for path in paths:
                self._save(path, partial)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        os.replace(partial, self.parent / name)
        self.root = self.parent / name

    def _save(self, path: Path, into: Path) -> None:
        if not exists(path):
            return
        if path.is_relative_to(self.home):
            dest = into / "home" / path.relative_to(self.home)
        else:
            dest = into / "root" / path.relative_to(path.anchor)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir() and not path.is_symlink():
            shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=True)
        elif not exists(dest):
            shutil.copy2(path, dest, follow_symlinks=False)

    def oldest(self) -> Path | None:
        complete = [
            path
            for path in self.parent.glob("*")
            if path.is_dir() and not path.name.endswith(".partial")
        ]
        return min(complete, default=None)


def restore_commands(backup: Path, home: Path, user: Path) -> list[str]:
    commands = [f"rm -rf {shlex.quote(str(user))}"]
    if (backup / "home").exists():
        commands.append(f"cp -a {shlex.quote(str(backup / 'home'))}/. {shlex.quote(str(home))}/")
    if (backup / "root").exists():
        commands.append(f"cp -a {shlex.quote(str(backup / 'root'))}/. /")
    return commands


# --- TOML ------------------------------------------------------------------------------------


def is_table(value: Any) -> bool:
    return isinstance(value, dict)


def plain(value: Any) -> Any:
    return value.unwrap() if isinstance(value, Item | tomlkit.TOMLDocument) else value


def portable(doc: Any, base: Path) -> Any:
    """`doc` as a user-level manifest: relative `path`s made absolute against `base`, and no
    `targets`."""
    for table in PATH_TABLES:
        entries: Any = doc.get(table)
        specs: list[Any] = list(entries.values()) if is_table(entries) else []
        for spec in specs:
            path = plain(spec.get("path")) if is_table(spec) else None
            if isinstance(path, str) and not Path(path).expanduser().is_absolute():
                spec["path"] = os.path.normpath(base / path)
    settings = doc.get("settings")
    if is_table(settings):
        for key in TARGET_KEYS:
            settings.pop(key, None)
        if not settings:
            del doc["settings"]
    return doc


@dataclass
class Merged:
    """One user-level file: what is there now, and the sources merged into it."""

    path: Path
    current: str | None
    doc: Any
    sources: list[Path] = field(default_factory=list[Path])

    def add(self, src: Any, label: Path, problems: list[str]) -> None:
        if src:
            self.sources.append(label)
            self._merge(self.doc, src, label, problems, "")

    def _merge(self, dst: Any, src: Any, label: Path, problems: list[str], where: str) -> None:
        for key, value in src.items():
            if key not in dst:
                dst[key] = value
            elif is_table(dst[key]) and is_table(value):
                self._merge(dst[key], value, label, problems, f"{where}{key}.")
            elif plain(dst[key]) != plain(value):
                earlier = [str(p) for p in [self.path, *self.sources] if p != label]
                problems.append(
                    f"`{where}{key}` is {plain(value)!r} in {label} but {plain(dst[key])!r} in "
                    f"{' or '.join(earlier)}; make them agree (or delete one) and run again."
                )

    def text(self) -> str | None:
        """The new content, when it differs from what is there."""
        new: str = self.doc.as_string()
        return new if new.strip() and new != self.current else None


def merged(path: Path, problems: list[str]) -> Merged:
    doc = parse(path, problems)
    return Merged(path, read_text(path), doc if doc is not None else tomlkit.document())


def read_locks(paths: list[Path], problems: list[str]) -> dict[str, tuple[list[str], bool]]:
    """Each target the locks record, as Mars's `LockFile::recorded_targets` reads them (item
    outputs, the target in a package-directory key `<kind>/<name>@<target>`, and the targets
    holding hook or MCP entries), with its outputs and whether it holds such entries. Mars
    reads a project's `mars.lock` with its store's `machine.lock` (local targets) merged in."""
    targets: dict[str, tuple[list[str], bool]] = {}
    for path in filter(exists, paths):
        try:
            lock = tomllib.loads(path.read_text(encoding="utf-8"))
            for key, item in lock.get("items", {}).items():
                if "@" in key:
                    targets.setdefault(key.rsplit("@", 1)[1], ([], False))
                for output in item.get("outputs", []):
                    target = targets.setdefault(output["target_root"], ([], False))
                    target[0].append(output["dest_path"])
            for target in lock.get("config_entries", {}):
                targets[target] = (targets.get(target, ([], False))[0], True)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"{path} cannot be read ({exc}); fix it and run again.")
        except (AttributeError, KeyError, TypeError) as exc:
            problems.append(f"{path} is not a Mars lock ({exc!r}); fix it and run again.")
    targets.pop(".mars", None)
    return targets


# --- the plan --------------------------------------------------------------------------------


@dataclass
class Plan:
    steps: list[tuple[str, Callable[[], None]]] = field(
        default_factory=list[tuple[str, Callable[[], None]]]
    )
    problems: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])
    # Copied into the backup before the first step.
    saves: list[Path] = field(default_factory=list[Path])
    # The user-level files the run writes, by name, which Mars must read first, and the files
    # each is built from.
    writes: dict[str, str] = field(default_factory=dict[str, str])
    origins: list[str] = field(default_factory=list[str])

    def add(self, text: str, run: Callable[[], None]) -> None:
        self.steps.append((text, run))

    def delete(self, path: Path, why: str = "") -> None:
        if exists(path):
            self.saves.append(path)
            self.add(f"delete {path}{why}", lambda: remove(path))


def build_plan(paths: Paths, mars: Path) -> Plan:
    """The steps left, from what is on disk now: a re-run after any step rebuilds it."""
    plan = Plan()
    home, user = paths.home, paths.user
    home_lock, home_store, home_src = home / "mars.lock", home / ".mars", home / ".mars-src"
    home_manifests = (home / "mars.toml", home / "mars.local.toml")
    has_home_project = exists(home_manifests[0]) or exists(home_lock)
    meridian_manifest = paths.meridian_root / "mars.toml"

    # 1. Unlink each target the home project's locks record. `unlink` also edits ~/mars.toml,
    # ~/mars.local.toml and the locks, which the backup holds as they are deleted later.
    locks = [home_lock, home_store / "machine.lock"]
    targets = read_locks(locks, plan.problems) if has_home_project else {}
    for target, (dests, entries) in sorted(targets.items()):
        removed = [home / target / dest for dest in sorted(dests)]
        plan.saves += removed
        if entries:
            plan.saves += [home / name for name in ENTRY_FILES.get(target, ())]
        plan.add(
            f"`{mars} --root {home} unlink {target}`: removes "
            + (", ".join(str(path) for path in removed if exists(path)) or "no files")
            + ("; and its hook/MCP entries in that harness's config files" if entries else ""),
            lambda target=target: run_mars(mars, "--root", str(home), "unlink", target, show=False),
        )

    # 2. The user level's manifests, config.toml and curated.toml.
    manifest = merged(user / "mars.toml", plan.problems)
    local = merged(user / "mars.local.toml", plan.problems)
    for into, source in zip((manifest, local), home_manifests, strict=True):
        doc = parse(source, plan.problems)
        if doc is not None:
            into.add(portable(doc, home), source, plan.problems)
    meridian = parse(meridian_manifest, plan.problems)
    deps, sections = tomlkit.document(), tomlkit.document()
    if meridian is not None:
        for into, keys in ((deps, DEPENDENCY_TABLES), (sections, CONFIG_SECTIONS)):
            for key in keys:
                if key in meridian:
                    into[key] = meridian[key]
        manifest.add(portable(deps, paths.meridian_root), meridian_manifest, plan.problems)

    config = merged(user / "config.toml", plan.problems)
    carried: list[Path] = []
    curated: Path | None = None
    for name in ("config.toml", "curated.toml"):
        source = paths.old_config / name
        stray = paths.unread_config / name if paths.unread_config else None
        if stray is not None and exists(stray):
            # Never read by Mars on macOS: it moves only into a user level that has none (or
            # already holds it, after a cut-short run), so a later run leaves it alone.
            if not exists(source) and (not exists(user / name) or _holds(user / name, stray)):
                source = stray
                plan.notes.append(
                    f"{stray} was never read by Mars on macOS, so its settings never applied; "
                    f"check they are still wanted in {user / name}."
                )
            else:
                plan.notes.append(f"{stray} is left in place: Mars never read it on macOS.")
        if not exists(source) or source == user / name:
            continue
        carried.append(source)
        if name == "config.toml":
            config.add(parse(source, plan.problems), source, plan.problems)
        elif not exists(user / name):
            curated = source
        elif read_text(source) != read_text(user / name):
            plan.problems.append(f"{source} and {user / name} differ; keep one and run again.")
    config.add(sections, meridian_manifest, plan.problems)

    for file in (manifest, local, config):
        text = file.text()
        if text is not None:
            plan.writes[file.path.name] = text
            plan.origins.append(f"{file.path.name} from {', '.join(map(str, file.sources))}")
            plan.saves.append(file.path)
            plan.add(
                f"write {file.path} from {', '.join(map(str, file.sources))}",
                lambda path=file.path, text=text: write_atomic(path, text),
            )
    if curated is not None:
        text = read_text(curated) or ""
        plan.writes["curated.toml"] = text
        plan.add(
            f"write {user / 'curated.toml'} from {curated}",
            lambda: write_atomic(user / "curated.toml", text),
        )

    # 3. `.mars-src/` items into the user level's.
    user_src = user / ".mars-src"
    taken: dict[Path, Path] = {}
    for folder in (home_src, paths.meridian_root / ".mars-src"):
        for rel in src_items(folder):
            source, dest = folder / rel, user_src / rel
            other = dest if exists(dest) else taken.get(rel)
            if other is not None and not same_tree(source, other):
                plan.problems.append(
                    f"{source} and {other} differ; keep one (delete or rename the other) and "
                    "run again."
                )
            elif other is None:
                taken[rel] = source
                plan.add(f"move {source} to {dest}", lambda s=source, d=dest: _move(s, d))

    # 4. Delete what was carried, the home project's own files last: while they exist, a
    # re-run still knows the store leftovers are the home project's.
    for source in carried:
        plan.delete(source, f", now in {user / source.name}")
    plan.delete(home_src)
    if has_home_project:
        if home_store == user:
            for path in sorted(user.iterdir()):
                if path.name not in USER_LEVEL_FILES:
                    plan.delete(path, " (the home project's old store)")
        else:
            plan.delete(home_store, " (the home project's old store)")
        for path in (home_lock, *reversed(home_manifests)):
            plan.delete(path)
    plan.delete(paths.meridian_root, " (Meridian 0.28 reads only it; the backup keeps it)")

    # 5. Install the user level; also when a run was cut short before its sync.
    moves = bool(plan.steps)
    has_items = bool(taken) or user_src.is_dir() or exists(user / "mars.toml") or plan.writes
    if has_items and (moves or not exists(user / "mars.lock")):
        plan.add(f"`{mars} --user sync`", lambda: run_mars(mars, "--user", "sync"))
    if moves:
        plan.saves.insert(0, user)
    return plan


def _holds(file: Path, part: Path) -> bool:
    """Whether `file` already holds everything in `part`."""
    if file.name != "config.toml":
        return read_text(file) == read_text(part)
    problems: list[str] = []
    trial = merged(file, problems)
    trial.add(parse(part, problems), part, problems)
    return not problems and trial.text() is None


def _move(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if exists(dest) and same_tree(source, dest):
        remove(source)  # a re-run after a move that was cut short
    else:
        shutil.move(source, dest)


def report_links(paths: Paths, gone: list[Path]) -> None:
    """Symlinks in ~/.local/bin whose target is missing (or in `gone`, for a dry run), with the
    user level's installed copy to point them at where there is one."""
    bin_dir = paths.home / ".local" / "bin"
    lines: list[str] = []
    for link in sorted(bin_dir.iterdir()) if bin_dir.is_dir() else []:
        target = real(link)
        if not link.is_symlink() or (
            target.exists() and not any(target.is_relative_to(path) for path in gone)
        ):
            continue
        parts = target.parts
        new = next(
            (
                paths.user.joinpath(*parts[i:])
                for i in range(len(parts) - 1)
                if parts[i] in ("skills", "agents")
                and (paths.user / parts[i] / parts[i + 1]).exists()
            ),
            None,
        )
        fix = f"`ln -sfn {shlex.quote(str(new))} {shlex.quote(str(link))}`" if new else "no copy"
        lines.append(f"{link} -> {target}: {fix}")
    if lines:
        print(f"\nSymlinks in {bin_dir} whose target {'will be' if gone else 'is'} gone:")
        print("\n".join(f"  {line}" for line in lines))


# --- main ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate-to-dot-mars.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="do the steps (default: print them)")
    parser.add_argument("--mars", metavar="PATH", help="the Mars 0.41+ binary to run")
    args = parser.parse_args(argv)

    paths = locate()
    mars = find_mars(args.mars)
    problem = mars_problem(mars)
    plan = build_plan(paths, mars or Path("mars"))
    if mars is not None and problem is None and plan.writes and not plan.problems:
        unreadable = check_with_mars(paths, mars, plan.writes)
        if unreadable:
            plan.problems.append(
                f"Mars cannot read the user level this run would write: {unreadable}\n"
                f"    It writes {'; '.join(plan.origins)}. Fix the source and run again."
            )

    print(f"User level: {paths.user}")
    print(f"Mars: {mars or 'not found'}{f' ({problem})' if problem else ''}")
    if plan.problems:
        print("\nNothing was changed. Fix these first:")
        print("\n".join(f"  - {line}" for line in plan.problems))
        return 1
    if not plan.steps:
        print("\nNothing to move: this setup is already on the Mars user level.")
        report_links(paths, [])
        return 0
    if args.apply and problem:
        print(f"\nNothing was changed: {problem}.")
        return 1

    backup = Backup(paths.home)
    saves = [
        path
        for path in dict.fromkeys(plan.saves)
        if not any(path != other and path.is_relative_to(other) for other in plan.saves)
    ]
    print(f"\n{'Steps' if args.apply else 'Steps (dry run; --apply does them)'}:")
    if saves:
        print(f"  0. copy into a new folder in {backup.parent}/: {', '.join(map(str, saves))}")
    for number, (text, _) in enumerate(plan.steps, 1):
        print(f"  {number}. {text}")
    for note in plan.notes:
        print(f"\nNote: {note}")
    if not args.apply:
        report_links(paths, [path for path in saves if not path.is_relative_to(paths.user)])
        return 0

    print()
    number = 0
    try:
        if saves:
            backup.write(saves)
            print(f"  0. done: {backup.root}")
        for number, (_, run) in enumerate(plan.steps, 1):
            run()
            print(f"  {number}. done")
    except (StepError, OSError, KeyboardInterrupt) as exc:
        print(f"\nStep {number} failed: {exc or type(exc).__name__}")
        if backup.root is None:
            print("Nothing was changed. Fix that and run again with --apply.")
        else:
            print(
                "Fix that and run again with --apply: finished steps are skipped. "
                f"{backup.root} holds the setup as it was before this run."
            )
        report_links(paths, [])
        return 1
    print("\nDone. Next: run `meridian mars sync` in each project that should get these items.")
    oldest = backup.oldest()
    if oldest is not None:
        print(f"To roll back to Mars 0.40 / Meridian 0.28, from the oldest backup, {oldest}:")
        print("\n".join(f"  {c}" for c in restore_commands(oldest, paths.home, paths.user)))
    report_links(paths, [])
    return 0


if __name__ == "__main__":
    sys.exit(main())
