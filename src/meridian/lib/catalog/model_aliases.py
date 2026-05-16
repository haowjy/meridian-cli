"""Alias parsing and merge helpers for the model catalog.

Model aliases are resolved exclusively via mars packages.
Meridian has ZERO builtin alias definitions — all aliases come from
mars dependency packages (via .mars/models-merged.json) and consumer
config (via mars.toml [models]).
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.catalog.model_policy import pattern_fallback_harness
from meridian.lib.core.types import HarnessId, ModelId

logger = logging.getLogger(__name__)


class RunnablePath(BaseModel):
    """One runnable harness route for a model, as reported by Mars."""

    model_config = ConfigDict(frozen=True)

    harness: str
    harness_model_id: str
    provider: str = ""


class AliasEntry(BaseModel):
    """Alias entry for model lookup."""

    model_config = ConfigDict(frozen=True)

    alias: str
    model_id: ModelId
    resolved_harness: HarnessId | None = Field(default=None, exclude=True)
    description: str | None = Field(default=None, exclude=True)
    default_effort: str | None = Field(default=None, exclude=True)
    default_autocompact: int | None = Field(default=None, exclude=True)
    default_autocompact_pct: int | None = Field(default=None, exclude=True)
    harness_candidates: tuple[str, ...] = Field(default=(), exclude=True)
    runnable_paths: tuple[RunnablePath, ...] = Field(default=(), exclude=True)

    @property
    def harness(self) -> HarnessId:
        if self.resolved_harness is not None:
            return self.resolved_harness
        return pattern_fallback_harness(str(self.model_id))

    @property
    def mars_provided_harness(self) -> HarnessId | None:
        return self.resolved_harness

    def harness_model_id_for(self, harness: str) -> str | None:
        """Return the harness-specific model ID for a given harness, or None."""
        for path in self.runnable_paths:
            if path.harness == harness:
                return path.harness_model_id
        return None

    def format_text(self, ctx: object | None = None) -> str:
        _ = ctx
        from meridian.lib.core.formatting import kv_block

        pairs: list[tuple[str, str | None]] = [
            ("Model", str(self.model_id)),
            ("Harness", str(self.harness)),
            ("Alias", self.alias or None),
        ]
        return kv_block(pairs)


def entry(
    *,
    alias: str,
    model_id: str,
    harness: str | None = None,
    description: str | None = None,
    default_effort: str | None = None,
    default_autocompact: int | None = None,
    default_autocompact_pct: int | None = None,
    harness_candidates: tuple[str, ...] = (),
    runnable_paths: tuple[RunnablePath, ...] = (),
) -> AliasEntry:
    resolved_harness: HarnessId | None = None
    if harness:
        with contextlib.suppress(ValueError):
            resolved_harness = HarnessId(harness)
    return AliasEntry(
        alias=alias,
        model_id=ModelId(model_id),
        resolved_harness=resolved_harness,
        description=description,
        default_effort=default_effort,
        default_autocompact=default_autocompact,
        default_autocompact_pct=default_autocompact_pct,
        harness_candidates=harness_candidates,
        runnable_paths=runnable_paths,
    )


def _coerce_optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _coerce_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return int(normalized)
        except ValueError:
            return None
    return None


def parse_harness_candidates(raw_candidates: object) -> tuple[str, ...]:
    if not isinstance(raw_candidates, list):
        return ()
    return tuple(
        candidate.strip()
        for candidate in cast("list[object]", raw_candidates)
        if isinstance(candidate, str) and candidate.strip()
    )


def parse_runnable_paths(raw_paths: object) -> tuple[RunnablePath, ...]:
    if not isinstance(raw_paths, list):
        return ()

    runnable_paths: list[RunnablePath] = []
    for path in cast("list[object]", raw_paths):
        if not isinstance(path, dict):
            continue
        typed_path = cast("dict[str, object]", path)
        harness = _coerce_optional_string(typed_path.get("harness"))
        harness_model_id = _coerce_optional_string(typed_path.get("harness_model_id"))
        if harness is None or harness_model_id is None:
            continue
        mars_provider = _coerce_optional_string(typed_path.get("mars_provider"))
        provider = mars_provider or _coerce_optional_string(typed_path.get("provider"))
        runnable_paths.append(
            RunnablePath(
                harness=harness,
                harness_model_id=harness_model_id,
                provider=provider or "",
            )
        )
    return tuple(runnable_paths)


def _normalize_project_root_key(project_root: Path | None) -> str:
    """Normalize a project root path into a stable cache key string.

    Returns empty string for None, otherwise the resolved posix path.
    """
    if project_root is None:
        return ""
    return project_root.resolve().as_posix()


def _resolve_cache_key(name: str, project_root: Path | None) -> tuple[str, str]:
    """Build a cache key for mars models resolve queries."""
    return (name.strip(), _normalize_project_root_key(project_root))


def _list_cache_key(project_root: Path | None) -> str:
    """Build a cache key for mars models list queries."""
    return _normalize_project_root_key(project_root)


def _list_all_cache_key(project_root: Path | None) -> str:
    """Build a cache key for mars models list --all queries."""
    return _normalize_project_root_key(project_root)


_SENTINEL = object()


@dataclass
class MarsResultCache:
    """Operation-scoped cache for mars subprocess results.

    Created at the top of a launch operation and threaded through
    resolve_model, load_merged_aliases, etc. Prevents redundant
    mars subprocess calls within a single CLI invocation.

    Safety: scoped to one operation. Transient None results are cached within
    the operation. Not module-global.
    """

    _resolve: dict[tuple[str, str], dict[str, object] | None] = field(
        default_factory=lambda: cast("dict[tuple[str, str], dict[str, object] | None]", {})
    )
    _list: dict[str, list[dict[str, object]] | None] = field(
        default_factory=lambda: cast("dict[str, list[dict[str, object]] | None]", {})
    )
    _list_all: dict[str, list[dict[str, object]] | None] = field(
        default_factory=lambda: cast("dict[str, list[dict[str, object]] | None]", {})
    )

    def get_resolve(
        self,
        name: str,
        project_root: Path | None,
    ) -> dict[str, object] | None | object:
        """Return cached resolve result, or _SENTINEL if not cached."""
        key = _resolve_cache_key(name, project_root)
        return self._resolve.get(key, _SENTINEL)

    def put_resolve(
        self,
        name: str,
        project_root: Path | None,
        result: dict[str, object] | None,
    ) -> None:
        key = _resolve_cache_key(name, project_root)
        self._resolve[key] = result

    def get_list(self, project_root: Path | None) -> list[dict[str, object]] | None | object:
        """Return cached list result, or _SENTINEL if not cached."""
        key = _list_cache_key(project_root)
        return self._list.get(key, _SENTINEL)

    def put_list(
        self,
        project_root: Path | None,
        result: list[dict[str, object]] | None,
    ) -> None:
        key = _list_cache_key(project_root)
        self._list[key] = result

    def get_list_all(self, project_root: Path | None) -> list[dict[str, object]] | None | object:
        """Return cached list-all result, or _SENTINEL if not cached."""
        key = _list_all_cache_key(project_root)
        return self._list_all.get(key, _SENTINEL)

    def put_list_all(
        self,
        project_root: Path | None,
        result: list[dict[str, object]] | None,
    ) -> None:
        key = _list_all_cache_key(project_root)
        self._list_all[key] = result


# ---------------------------------------------------------------------------
# Mars integration
# ---------------------------------------------------------------------------


def _resolve_mars_binary() -> str | None:
    """Find the mars binary, preferring the one from the same install environment."""
    scripts_dir = Path(sys.executable).parent
    for name in ("mars", "mars.exe"):
        candidate = scripts_dir / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("mars")


def _run_mars_models_list(project_root: Path | None = None) -> list[dict[str, object]] | None:
    """Call ``mars models list --json`` and return the alias entries.

    Returns *None* when the mars binary is unavailable or the command fails,
    so the caller can fall back to reading the cached merged file.
    """
    mars_bin = _resolve_mars_binary()
    if mars_bin is None:
        return None

    cmd = [mars_bin, "models", "list", "--json"]
    if project_root is not None:
        cmd.extend(["--root", str(project_root)])

    try:
        # mars may do a cold models.dev fetch in ensure_fresh(Auto); mars caps each HTTP
        # phase at 15s (connect + recv-response + recv-body), so worst-case cold fetch is
        # ~45s. 60s leaves a small headroom for first-boot DNS, slow disks, and startup.
        # Use the same timeout as run_mars_models_resolve since both paths can refresh.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        logger.debug("mars binary not available or timed out", exc_info=True)
        return None

    if result.returncode != 0:
        logger.debug("mars models list failed (rc=%d): %s", result.returncode, result.stderr)
        return None

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        logger.debug("mars models list returned invalid JSON")
        return None

    aliases = payload.get("aliases")
    if not isinstance(aliases, list):
        return None
    return cast("list[dict[str, object]]", aliases)


def run_mars_models_list_all(project_root: Path | None = None) -> list[dict[str, object]] | None:
    """Call ``mars models list --all --json`` and return the model entries.

    Returns *None* when the mars binary is unavailable or the command fails.
    """
    mars_bin = _resolve_mars_binary()
    if mars_bin is None:
        return None

    cmd = [mars_bin, "models", "list", "--all", "--json"]
    if project_root is not None:
        cmd.extend(["--root", str(project_root)])

    try:
        # mars may do a cold models.dev fetch in ensure_fresh(Auto); mars caps each HTTP
        # phase at 15s (connect + recv-response + recv-body), so worst-case cold fetch is
        # ~45s. 60s leaves a small headroom for first-boot DNS, slow disks, and startup.
        # Keep timeout aligned with other mars model list/resolve paths.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        logger.debug("mars binary not available or timed out", exc_info=True)
        return None

    if result.returncode != 0:
        logger.debug("mars models list --all failed (rc=%d): %s", result.returncode, result.stderr)
        return None

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        logger.debug("mars models list --all returned invalid JSON")
        return None

    models = payload.get("models")
    if not isinstance(models, list):
        return None
    return cast("list[dict[str, object]]", models)


def _extract_mars_error_message(raw_output: str) -> str | None:
    """Extract a readable error message from mars stdout/stderr text."""
    output = raw_output.strip()
    if not output:
        return None

    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return output

    if isinstance(payload, dict):
        typed_payload = cast("dict[str, object]", payload)
        error = typed_payload.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
        message = typed_payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    return output


def _is_unknown_alias_error(message: str | None) -> bool:
    if message is None:
        return False
    return "unknown alias" in message.lower()


def run_mars_models_resolve(
    name: str,
    project_root: Path | None = None,
) -> dict[str, object] | None:
    """Call ``mars models resolve <name> --json`` and return the resolved entry.

    Returns ``None`` when the alias is unknown (mars exit code 1).
    Raises ``RuntimeError`` when mars is unavailable or broken - mars is
    always bundled with meridian, so absence is a hard error.
    """
    mars_bin = _resolve_mars_binary()
    if mars_bin is None:
        raise RuntimeError(
            "Mars binary not found. Mars is required for model resolution. "
            "Run 'meridian doctor' to diagnose."
        )
    cmd = [mars_bin, "models", "resolve", name, "--json"]
    if project_root is not None:
        cmd.extend(["--root", str(project_root)])
    try:
        # mars may do a cold models.dev fetch in ensure_fresh(Auto); mars caps each HTTP
        # phase at 15s (connect + recv-response + recv-body), so worst-case cold fetch is
        # ~45s. 60s leaves a small headroom for first-boot DNS, slow disks, and startup.
        # Use the same timeout as _run_mars_models_list since both paths can refresh.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Mars binary not found. Mars is required for model resolution. "
            "Run 'meridian doctor' to diagnose."
        ) from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("mars models resolve failed: %s", exc, exc_info=True)
        raise RuntimeError(
            "Mars model resolution failed. Run 'meridian doctor' to diagnose."
        ) from exc
    if result.returncode != 0:
        stderr_message = _extract_mars_error_message(result.stderr)
        stdout_message = _extract_mars_error_message(result.stdout)
        error_message = stderr_message or stdout_message
        logger.debug(
            "mars models resolve '%s' exited %d: stderr=%r stdout=%r",
            name,
            result.returncode,
            result.stderr,
            result.stdout,
        )
        if _is_unknown_alias_error(error_message):
            return None

        if error_message:
            raise RuntimeError(f"Mars model resolution failed: {error_message}")
        raise RuntimeError(
            f"Mars model resolution failed: mars exited with status {result.returncode}."
        )
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        logger.debug("mars models resolve returned invalid JSON for '%s'", name)
        return None
    if not isinstance(payload, dict):
        logger.debug("mars models resolve returned non-object JSON for '%s'", name)
        return None
    return cast("dict[str, object]", payload)


def cached_mars_models_resolve(
    name: str,
    project_root: Path | None = None,
    *,
    cache: MarsResultCache | None = None,
) -> dict[str, object] | None:
    """Resolve a model through mars, using cache if provided."""
    if cache is not None:
        cached = cache.get_resolve(name, project_root)
        if cached is not _SENTINEL:
            return cast("dict[str, object] | None", cached)
    result = run_mars_models_resolve(name, project_root)
    if cache is not None:
        cache.put_resolve(name, project_root, result)
    return result


def cached_mars_models_list(
    project_root: Path | None = None,
    *,
    cache: MarsResultCache | None = None,
) -> list[dict[str, object]] | None:
    """List models through mars, using cache if provided."""
    if cache is not None:
        cached = cache.get_list(project_root)
        if cached is not _SENTINEL:
            return cast("list[dict[str, object]] | None", cached)
    result = _run_mars_models_list(project_root)
    if cache is not None:
        cache.put_list(project_root, result)
    return result


def cached_mars_models_list_all(
    project_root: Path | None = None,
    *,
    cache: MarsResultCache | None = None,
) -> list[dict[str, object]] | None:
    """List all models through mars, using cache if provided."""
    if cache is not None:
        cached = cache.get_list_all(project_root)
        if cached is not _SENTINEL:
            return cast("list[dict[str, object]] | None", cached)
    result = run_mars_models_list_all(project_root)
    if cache is not None:
        cache.put_list_all(project_root, result)
    return result


def _read_mars_merged_file(project_root: Path | None = None) -> dict[str, object]:
    """Read ``.mars/models-merged.json`` directly (dep-only aliases).

    Falls back to empty dict if the file doesn't exist or is invalid.
    """
    if project_root is None:
        return {}

    merged_path = project_root / ".mars" / "models-merged.json"
    if merged_path.is_file():
        try:
            raw = json.loads(merged_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return cast("dict[str, object]", raw)
        except (OSError, json.JSONDecodeError, ValueError):
            logger.debug("Failed to read %s", merged_path, exc_info=True)
    return {}


def _mars_list_to_entries(aliases_list: list[dict[str, object]]) -> list[AliasEntry]:
    """Convert mars ``models list --json`` alias entries to :class:`AliasEntry` objects."""
    entries: list[AliasEntry] = []
    for item in aliases_list:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue

        resolved_model = item.get("model_id") or item.get("resolved_model")
        harness = item.get("harness")
        description = item.get("description")
        default_effort = item.get("default_effort")
        default_autocompact = item.get("autocompact")
        default_autocompact_pct = item.get("autocompact_pct")
        harness_candidates = parse_harness_candidates(item.get("harness_candidates"))
        runnable_paths = parse_runnable_paths(item.get("runnable_paths"))

        # Skip aliases that didn't resolve to a concrete model ID
        if not isinstance(resolved_model, str) or not resolved_model.strip():
            continue

        entries.append(
            entry(
                alias=name.strip(),
                model_id=resolved_model.strip(),
                harness=str(harness) if isinstance(harness, str) else None,
                description=str(description) if isinstance(description, str) else None,
                default_effort=_coerce_optional_string(default_effort),
                default_autocompact=_coerce_optional_int(default_autocompact),
                default_autocompact_pct=_coerce_optional_int(default_autocompact_pct),
                harness_candidates=harness_candidates,
                runnable_paths=runnable_paths,
            )
        )

    return entries


def _mars_merged_to_entries(merged: dict[str, object]) -> list[AliasEntry]:
    """Convert ``.mars/models-merged.json`` raw data to :class:`AliasEntry` objects.

    For pinned aliases, the model ID is stored directly.  For auto-resolve aliases,
    only the harness and description are available — the model ID is unknown
    without the models cache, so these are skipped.
    """
    entries: list[AliasEntry] = []
    for alias_name, alias_data in merged.items():
        if not isinstance(alias_data, dict):
            continue
        typed_data = cast("dict[str, object]", alias_data)

        # Pinned alias: has a "model" key
        model_id = typed_data.get("model")
        if isinstance(model_id, str) and model_id.strip():
            harness = typed_data.get("harness")
            description = typed_data.get("description")
            default_effort = typed_data.get("default_effort")
            default_autocompact = typed_data.get("autocompact")
            default_autocompact_pct = typed_data.get("autocompact_pct")
            entries.append(
                entry(
                    alias=alias_name,
                    model_id=model_id.strip(),
                    harness=str(harness) if isinstance(harness, str) else None,
                    description=str(description) if isinstance(description, str) else None,
                    default_effort=_coerce_optional_string(default_effort),
                    default_autocompact=_coerce_optional_int(default_autocompact),
                    default_autocompact_pct=_coerce_optional_int(default_autocompact_pct),
                )
            )
        # Auto-resolve aliases without the cache can't be resolved here
        # — they need mars models list which runs auto-resolve against the cache

    return entries


def load_mars_aliases(
    project_root: Path | None = None,
    *,
    cache: MarsResultCache | None = None,
) -> list[AliasEntry]:
    """Load model aliases from mars.

    Prefers ``mars models list --json`` for the full resolved view
    (includes consumer overrides + auto-resolution).  Falls back to
    reading ``.mars/models-merged.json`` if the mars binary isn't available.
    """
    # Try mars CLI first — it returns fully resolved aliases
    mars_list = cached_mars_models_list(project_root, cache=cache)
    if mars_list is not None:
        entries = _mars_list_to_entries(mars_list)
        if entries:
            return sorted(entries, key=lambda e: e.alias)

    # Fallback: read the cached dependency file directly
    merged = _read_mars_merged_file(project_root)
    if merged:
        entries = _mars_merged_to_entries(merged)
        if entries:
            return sorted(entries, key=lambda e: e.alias)

    return []


def load_mars_descriptions(project_root: Path | None = None) -> dict[str, str]:
    """Load model descriptions from mars aliases.

    Returns a dict keyed by model_id with description values.
    """
    descriptions: dict[str, str] = {}

    # Try mars CLI first
    mars_list = _run_mars_models_list(project_root)
    if mars_list is not None:
        for item in mars_list:
            resolved_model = item.get("model_id") or item.get("resolved_model")
            description = item.get("description")
            if (
                isinstance(resolved_model, str)
                and resolved_model.strip()
                and isinstance(description, str)
                and description.strip()
            ):
                descriptions[resolved_model.strip()] = description.strip()
        return descriptions

    # Fallback: read merged file
    merged = _read_mars_merged_file(project_root)
    for alias_data in merged.values():
        if not isinstance(alias_data, dict):
            continue
        typed_data = cast("dict[str, object]", alias_data)
        model_id = typed_data.get("model")
        description = typed_data.get("description")
        if (
            isinstance(model_id, str)
            and model_id.strip()
            and isinstance(description, str)
            and description.strip()
        ):
            descriptions[model_id.strip()] = description.strip()

    return descriptions


__all__ = [
    "AliasEntry",
    "MarsResultCache",
    "RunnablePath",
    "cached_mars_models_list",
    "cached_mars_models_list_all",
    "cached_mars_models_resolve",
    "load_mars_aliases",
    "load_mars_descriptions",
    "parse_harness_candidates",
    "parse_runnable_paths",
    "run_mars_models_list_all",
    "run_mars_models_resolve",
]
