#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["tomlkit>=0.12"]
# ///
"""Move a Mars 0.40 / Meridian 0.28 setup into the Mars 0.41 user level (~/.mars).

Run once, after installing Mars 0.41.0 or later (Meridian 0.29 bundles it):

    uv run https://raw.githubusercontent.com/haowjy/meridian-cli/main/scripts/migrate-to-dot-mars.py
    uv run .../migrate-to-dot-mars.py --apply

Without --apply it only prints the steps, with real paths. It moves:

  - a home project (~/mars.toml, ~/mars.lock): unlinks what it installed into ~/.claude,
    ~/.agents and the other harness folders, carries its dependencies and settings (not
    `targets`) into the user level's mars.toml, moves ~/.mars-src in, and deletes the home
    project and its old store in ~/.mars;
  - Mars 0.40's config.toml and curated.toml ($XDG_CONFIG_HOME/mars or ~/.config/mars on
    Linux, ~/Library/Application Support/mars on macOS) into the user level;
  - Meridian 0.28's ~/.meridian/mars: dependencies into mars.toml, .mars-src items into the
    user level's, [settings]/[models]/[agents]/[skills] into config.toml; then deletes it;

then runs `mars --user sync`. The user level is $MARS_HOME when set, else ~/.mars. A name both
sides define with different content stops the run before anything changes. A re-run skips
what is done.

Backup: before changing anything, --apply copies every path it will move, rewrite or delete,
the user level folder included, into ~/.mars-migration-backup/<UTC timestamp>/, mirroring
paths under home in home/ (paths outside home go in root/, mirroring /).

Roll back (to Mars 0.40 / Meridian 0.28) from that backup:

    rm -rf ~/.mars                                  # or $MARS_HOME: the user level the run made
    cp -a ~/.mars-migration-backup/<stamp>/home/. ~/
    cp -a ~/.mars-migration-backup/<stamp>/root/. /   # only if the run made root/

The run prints these commands with its real paths at the end. When a run was cut short and
run again, use the first run's backup (the oldest): it holds the setup as it was before.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
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
# What Meridian 0.28's root held that now belongs in the user config.toml.
CONFIG_SECTIONS = ("settings", "models", "agents", "skills")
# Settings a user-level mars.toml must not have.
TARGET_KEYS = ("targets", "managed_root")


@dataclass(frozen=True)
class Paths:
    home: Path
    user: Path
    meridian_root: Path
    # Where Mars 0.40 kept config.toml and curated.toml; None when that is the user level.
    old_config: Path | None
    # macOS: ~/.config/mars, which Mars 0.40 never read there.
    old_config_unread: bool


def locate() -> Paths:
    home = Path.home()
    if not home.is_absolute():
        raise SystemExit("HOME is not an absolute path; set it and run again.")

    def env_dir(name: str) -> Path | None:
        value = os.environ.get(name, "").strip()
        if not value:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else Path.cwd() / path

    # Mars 0.41 reads Mars 0.40's MARS_CONFIG_DIR as MARS_HOME while MARS_HOME is unset.
    config_dir = env_dir("MARS_CONFIG_DIR")
    user = env_dir("MARS_HOME") or config_dir or home / ".mars"
    meridian_root = (env_dir("MERIDIAN_HOME") or home / ".meridian") / "mars"

    unread = False
    if config_dir is not None:
        old_config = config_dir
    elif sys.platform == "darwin":
        old_config = home / "Library" / "Application Support" / "mars"
        dot_config = home / ".config" / "mars"
        if not _has_config(old_config) and _has_config(dot_config):
            old_config, unread = dot_config, True
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"
        old_config = base / "mars"
    if old_config == user:
        old_config = None
    return Paths(home, user, meridian_root, old_config, unread)


def _has_config(folder: Path) -> bool:
    return (folder / "config.toml").exists() or (folder / "curated.toml").exists()


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


def mars_version(mars: Path) -> tuple[int, ...] | None:
    try:
        out = subprocess.run(
            [str(mars), "--version"], capture_output=True, text=True, check=False, timeout=60
        ).stdout
    except OSError:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(part) for part in match.groups()) if match else None


def run_mars(mars: Path, *args: str, show: bool = True) -> None:
    result = subprocess.run([str(mars), *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise StepError(
            f"`{mars} {' '.join(args)}` exited {result.returncode}:\n"
            f"{result.stdout}{result.stderr}".rstrip()
        )
    if show:
        for line in (result.stdout + result.stderr).strip().splitlines():
            print(f"      {line}")


# --- files -----------------------------------------------------------------------------------


class StepError(Exception):
    pass


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
    elif path.exists() or path.is_symlink():
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
    def __init__(self, home: Path, root: Path) -> None:
        self.home = home
        self.root = root

    def mirror(self, path: Path) -> Path:
        if path.is_relative_to(self.home):
            return self.root / "home" / path.relative_to(self.home)
        return self.root / "root" / path.relative_to(path.anchor)

    def save(self, path: Path) -> None:
        if not (path.exists() or path.is_symlink()):
            return
        dest = self.mirror(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir() and not path.is_symlink():
            shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=True)
        elif not (dest.exists() or dest.is_symlink()):
            shutil.copy2(path, dest, follow_symlinks=False)

    def restore_commands(self, user: Path) -> list[str]:
        commands = [f"rm -rf {user}"]
        if (self.root / "home").exists():
            commands.append(f"cp -a {self.root / 'home'}/. {self.home}/")
        if (self.root / "root").exists():
            commands.append(f"cp -a {self.root / 'root'}/. /")
        return commands


# --- TOML merging ----------------------------------------------------------------------------


def is_table(value: Any) -> bool:
    return isinstance(value, dict)


def plain(value: Any) -> Any:
    return value.unwrap() if isinstance(value, Item | tomlkit.TOMLDocument) else value


def merge(dst: Any, src: Any, where: str, labels: tuple[Path, Path], problems: list[str]) -> None:
    """Add `src`'s keys to `dst`; a key both hold with different values is a conflict."""
    for key, value in src.items():
        if key not in dst:
            dst[key] = value
        elif is_table(dst[key]) and is_table(value):
            merge(dst[key], value, f"{where}{key}.", labels, problems)
        elif plain(dst[key]) != plain(value):
            problems.append(
                f"`{where}{key}` is {plain(dst[key])!r} in {labels[0]} but "
                f"{plain(value)!r} in {labels[1]}; make them agree (or delete one) and run again."
            )


def absolute_spec(spec: Any, base: Path) -> Any:
    """A dependency entry with its relative `path` made absolute against `base`."""
    if not is_table(spec):
        return spec
    path = plain(spec.get("path"))
    if isinstance(path, str) and not Path(path).expanduser().is_absolute():
        spec["path"] = os.path.normpath(base / path)
    return spec


def merge_dependencies(
    dst: Any,
    src: Any,
    base: Path,
    dst_base: Path,
    labels: tuple[Path, Path],
    origins: dict[tuple[str, str], Path],
    problems: list[str],
) -> list[str]:
    """Carry `src`'s dependency tables into `dst`; returns the names added.

    `labels`: the file `dst` was read from and the one `src` was; an entry an earlier call
    added is named by its own file, kept in `origins`.
    """
    added: list[str] = []
    for table in DEPENDENCY_TABLES:
        entries = src.get(table)
        if not is_table(entries):
            continue
        for name, spec in entries.items():
            spec = absolute_spec(spec, base)
            have = dst.get(table, {}).get(name)
            if have is None:
                if table not in dst:
                    dst[table] = tomlkit.table(is_super_table=True)
                dst[table][name] = spec
                added.append(name)
                origins[(table, name)] = labels[1]
            elif absolute_spec(plain(have), dst_base) != plain(spec):
                where = origins.get((table, name), labels[0])
                problems.append(
                    f"dependency `{name}` differs between {where} and {labels[1]}; "
                    "make them agree (or remove one) and run again."
                )
    return added


# --- the plan --------------------------------------------------------------------------------


@dataclass
class Step:
    text: str
    run: Callable[[], None]


@dataclass
class Plan:
    paths: Paths
    mars: Path | None
    steps: list[Step] = field(default_factory=list[Step])
    problems: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])
    # Copied into the backup before the first step.
    saves: list[Path] = field(default_factory=list[Path])
    # (removed path, the user level's copy of it): for the symlinks report.
    replaced: list[tuple[Path, Path | None]] = field(default_factory=list[tuple[Path, Path | None]])

    def add(self, text: str, run: Callable[[], None]) -> None:
        self.steps.append(Step(text, run))


def build_plan(paths: Paths, mars: Path | None) -> Plan:
    plan = Plan(paths, mars)
    home, user = paths.home, paths.user
    mars_cmd = str(mars or "mars")
    home_manifest, home_lock = home / "mars.toml", home / "mars.lock"
    home_store, home_src = home / ".mars", home / ".mars-src"
    has_home_project = home_manifest.exists() or home_lock.exists()
    meridian_manifest = paths.meridian_root / "mars.toml"

    # 1. Unlink the home project's targets, from what its lock records.
    if home_lock.exists():
        try:
            lock = tomllib.loads(home_lock.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            plan.problems.append(f"{home_lock} cannot be read ({exc}); fix it and run again.")
            lock = {}
        outputs: dict[str, list[str]] = {}
        for item in lock.get("items", {}).values():
            for output in item.get("outputs", []):
                if output["target_root"] != ".mars":
                    outputs.setdefault(output["target_root"], []).append(output["dest_path"])
        for target, dests in sorted(outputs.items()):
            removed = [home / target / dest for dest in sorted(dests)]
            plan.saves += removed
            plan.replaced += [
                (path, user / dest) for path, dest in zip(removed, sorted(dests), strict=True)
            ]
            plan.add(
                f"`{mars_cmd} --root {home} unlink {target}`: removes "
                + ", ".join(str(path) for path in removed if path.exists() or path.is_symlink()),
                lambda target=target: run_mars(
                    mars_cmd_path(plan), "--root", str(home), "unlink", target, show=False
                ),
            )

    # 2. Dependencies (and the home project's own settings) into the user level's mars.toml.
    user_manifest = user / "mars.toml"
    manifest_text = read_text(user_manifest)
    manifest = parse(user_manifest, plan.problems) or tomlkit.document()
    carried: list[str] = []
    origins: dict[tuple[str, str], Path] = {}
    old = parse(home_manifest, plan.problems)
    if old is not None:
        carried += merge_dependencies(
            manifest, old, home, user, (user_manifest, home_manifest), origins, plan.problems
        )
        for table in DEPENDENCY_TABLES:
            old.pop(table, None)
        settings = old.get("settings")
        if is_table(settings):
            for key in TARGET_KEYS:
                settings.pop(key, None)
            if not settings:
                del old["settings"]
        merge(manifest, old, "", (user_manifest, home_manifest), plan.problems)
    meridian = parse(meridian_manifest, plan.problems)
    if meridian is not None:
        carried += merge_dependencies(
            manifest,
            meridian,
            paths.meridian_root,
            user,
            (user_manifest, meridian_manifest),
            origins,
            plan.problems,
        )
    new_manifest: str = manifest.as_string()
    if new_manifest.strip() and new_manifest != manifest_text:
        what = f"dependencies {', '.join(carried)}" if carried else "settings"
        plan.add(
            f"write {user_manifest}, adding {what} from {home_manifest} / {meridian_manifest}",
            lambda: write_atomic(user_manifest, new_manifest),
        )
    if home_manifest.exists():
        plan.saves.append(home_manifest)

    # 3. `.mars-src/` items into the user level's.
    user_src = user / ".mars-src"
    taken: dict[Path, Path] = {}
    for folder in (home_src, paths.meridian_root / ".mars-src"):
        for rel in src_items(folder):
            source, dest = folder / rel, user_src / rel
            plan.replaced.append((source, user / rel))
            other = dest if dest.exists() or dest.is_symlink() else taken.get(rel)
            if other is not None and not same_tree(source, other):
                plan.problems.append(
                    f"{source} and {other} differ; keep one (delete or rename the other) and "
                    "run again."
                )
                continue
            taken.setdefault(rel, source)
            if other is None:
                plan.add(f"move {source} to {dest}", lambda s=source, d=dest: _move(s, d))
            else:
                plan.add(f"delete {source}: the same as {other}", lambda s=source: remove(s))
    if home_src.exists():
        plan.saves.append(home_src)
        plan.add(f"delete {home_src}, now empty", lambda: remove(home_src))

    # 4. Settings into the user level's config.toml, curated.toml beside it.
    user_config = user / "config.toml"
    config_text = read_text(user_config)
    config = parse(user_config, plan.problems)
    config_label = user_config
    old_config = paths.old_config
    sources: list[tuple[Path, Any]] = []
    if old_config is not None:
        old_file = old_config / "config.toml"
        doc = parse(old_file, plan.problems)
        if doc is not None:
            plan.saves.append(old_file)
            if config is None:
                config, config_label = doc, old_file
            else:
                sources.append((old_file, doc))
    if meridian is not None:
        sections = tomlkit.document()
        for key in CONFIG_SECTIONS:
            if key in meridian:
                sections[key] = meridian[key]
        if sections:
            sources.append((meridian_manifest, sections))
    config = config or tomlkit.document()
    for label, doc in sources:
        merge(config, doc, "", (config_label, label), plan.problems)
    new_config: str = config.as_string()
    if new_config.strip() and new_config != config_text:
        merged = [config_label] if config_label != user_config else []
        merged += [label for label, _ in sources]
        plan.add(
            f"write {user_config} with the settings of {', '.join(map(str, merged))}",
            lambda: write_atomic(user_config, new_config),
        )
    if old_config is not None:
        old_curated, user_curated = old_config / "curated.toml", user / "curated.toml"
        if old_curated.exists():
            plan.saves.append(old_curated)
            if not user_curated.exists():
                plan.add(
                    f"move {old_curated} to {user_curated}",
                    lambda: _move(old_curated, user_curated),
                )
            elif same_tree(old_curated, user_curated):
                plan.add(
                    f"delete {old_curated}: the same as {user_curated}", lambda: remove(old_curated)
                )
            else:
                plan.problems.append(
                    f"{old_curated} and {user_curated} differ; keep one and run again."
                )
        if (old_config / "config.toml").exists():
            plan.add(
                f"delete {old_config / 'config.toml'}, now in {user_config}",
                lambda: remove(old_config / "config.toml"),
            )
        if paths.old_config_unread:
            plan.notes.append(
                f"{old_config} was never read by Mars on macOS, so its settings never applied; "
                f"check they are still wanted in {user_config}."
            )

    # 5. Delete the home project and its old store, then Meridian 0.28's root.
    if has_home_project:
        if home_store == user:
            leftovers = (
                sorted(p for p in user.iterdir() if p.name not in USER_LEVEL_FILES)
                if user.is_dir()
                else []
            )
            if leftovers:
                plan.add(
                    f"delete the home project's old store in {user}/: "
                    + ", ".join(p.name for p in leftovers),
                    lambda: _remove_all(leftovers),
                )
        elif home_store.exists():
            plan.saves.append(home_store)
            plan.add(
                f"delete the home project's old store {home_store}", lambda: remove(home_store)
            )
        for path in (home_lock, home_manifest):
            if path.exists():
                plan.saves.append(path)
                plan.add(f"delete {path}", lambda path=path: remove(path))
    if paths.meridian_root.exists():
        plan.saves.append(paths.meridian_root)
        plan.replaced.append((paths.meridian_root, None))
        plan.add(
            f"delete {paths.meridian_root} (Meridian 0.28 reads only it; the backup keeps it)",
            lambda: remove(paths.meridian_root),
        )

    # 6. Install the user level.
    # 6. Install the user level; also when a run was cut short before its sync.
    moves = bool(plan.steps)
    has_items = bool(taken) or user_src.is_dir() or user_manifest.exists() or carried
    if has_items and (moves or not (user / "mars.lock").exists()):
        plan.add(
            f"`{mars_cmd} --user sync`", lambda: run_mars(mars_cmd_path(plan), "--user", "sync")
        )
    if moves and user.exists():
        plan.saves.insert(0, user)
    return plan


def mars_cmd_path(plan: Plan) -> Path:
    if plan.mars is None:
        raise StepError("no Mars found")
    return plan.mars


def _remove_all(paths: list[Path]) -> None:
    for path in paths:
        remove(path)


def _move(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (dest.exists() or dest.is_symlink()) and same_tree(source, dest):
        remove(source)  # a re-run after a move that was cut short
    else:
        shutil.move(source, dest)


def broken_links(plan: Plan, applied: bool) -> list[str]:
    """Symlinks in ~/.local/bin into a path the run removes, with the user level's copy."""
    bin_dir = plan.paths.home / ".local" / "bin"
    lines: list[str] = []
    if not bin_dir.is_dir():
        return lines
    for link in sorted(bin_dir.iterdir()):
        if not link.is_symlink():
            continue
        target = Path(os.path.normpath(link.parent / os.readlink(link)))
        if applied and target.exists():
            continue
        for removed, copy in plan.replaced:
            if target != removed and removed not in target.parents:
                continue
            if copy is None:
                lines.append(f"{link} -> {target}: its target goes away")
            else:
                new = copy / target.relative_to(removed)
                lines.append(f"{link} -> {target}: repoint it with `ln -sfn {new} {link}`")
            break
    return lines


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
    version = mars_version(mars) if mars else None
    mars_problem = None
    if mars is None:
        mars_problem = "no `mars` found beside `meridian` or on PATH; pass --mars PATH"
    elif version is None or version < MIN_MARS:
        found = ".".join(map(str, version)) if version else "unknown"
        mars_problem = f"{mars} is Mars {found}; this needs 0.41.0 or later (upgrade Meridian)"

    plan = build_plan(paths, mars)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = Backup(paths.home, paths.home / ".mars-migration-backup" / stamp)

    print(f"User level: {paths.user}")
    print(f"Mars: {mars or 'not found'}{f' ({mars_problem})' if mars_problem else ''}")
    if plan.problems:
        print("\nNothing was changed. Fix these first:")
        for problem in plan.problems:
            print(f"  - {problem}")
        return 1
    if not plan.steps:
        print("\nNothing to move: this setup is already on the Mars user level.")
        return 0
    if plan.saves:
        print(f"Backup: {backup.root}")
    print(f"\n{'Steps' if args.apply else 'Steps (dry run; --apply does them)'}:")
    if plan.saves:
        print("  0. copy into the backup: " + ", ".join(map(str, dict.fromkeys(plan.saves))))
    for number, step in enumerate(plan.steps, 1):
        print(f"  {number}. {step.text}")
    if args.apply:
        if mars_problem:
            print(f"\nNothing was changed: {mars_problem}.")
            return 1
        print()
        number = 0
        try:
            for path in dict.fromkeys(plan.saves):
                backup.save(path)
            if plan.saves:
                print("  0. done")
            for number, step in enumerate(plan.steps, 1):
                step.run()
                print(f"  {number}. done")
        except (StepError, OSError) as exc:
            print(f"\nStep {number} failed: {exc}")
            print(
                "Fix that and run again with --apply: finished steps are skipped. "
                f"{backup.root} holds a copy of everything as it was before this run."
            )
            return 1
    for note in plan.notes:
        print(f"\nNote: {note}")
    links = broken_links(plan, args.apply)
    if links:
        print("\nSymlinks in ~/.local/bin pointing at a removed path:")
        for line in links:
            print(f"  {line}")
    if args.apply:
        print("\nDone. Next: run `meridian mars sync` in each project that should get these items.")
        if backup.root.exists():
            print(f"Backup: {backup.root}. To roll back:")
            print("\n".join(f"  {command}" for command in backup.restore_commands(paths.user)))
            earlier = sorted(p for p in backup.root.parent.iterdir() if p.name < backup.root.name)
            if earlier:
                print(
                    f"Earlier runs left {', '.join(map(str, earlier))}: after a run that was cut "
                    f"short, roll back from the oldest instead, {earlier[0]}, which holds the "
                    "setup as it was before the first run."
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
