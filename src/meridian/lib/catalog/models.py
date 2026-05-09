"""Model resolution compatibility exports for catalog helpers."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from meridian.lib.catalog.model_aliases import (
    AliasEntry,
    MarsResultCache,
    cached_mars_models_list_all,
    cached_mars_models_resolve,
    load_mars_aliases,
    run_mars_models_list_all,
    run_mars_models_resolve,
)
from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.core.types import HarnessId, ModelId


def load_merged_aliases(
    project_root: Path | None = None,
    *,
    cache: MarsResultCache | None = None,
) -> list[AliasEntry]:
    """Load model aliases from mars packages."""
    resolved_root = resolve_project_root(project_root) if project_root is not None else None
    return load_mars_aliases(resolved_root, cache=cache)


def resolve_model(
    name_or_alias: str,
    project_root: Path | None = None,
    *,
    cache: MarsResultCache | None = None,
) -> AliasEntry:
    """Resolve alias to model id, or pass through a direct model identifier.

    Resolution: mars resolve -> lazy exact-ID guard -> raw model ID passthrough.
    Mars is always present (bundled with meridian).
    """

    normalized = name_or_alias.strip()
    if not normalized:
        raise ValueError("Model identifier must not be empty.")

    def exact_id_alias_entry(model: dict[str, object]) -> AliasEntry:
        harness: object = model.get("harness")
        resolved_harness: HarnessId | None = None
        if isinstance(harness, str) and harness.strip():
            with suppress(ValueError):
                resolved_harness = HarnessId(harness.strip())

        description = model.get("description")
        return AliasEntry(
            alias="",
            model_id=ModelId(normalized),
            resolved_harness=resolved_harness,
            description=description.strip() if isinstance(description, str) else None,
        )

    def find_exact_id_match() -> dict[str, object] | None:
        models = (
            cached_mars_models_list_all(project_root, cache=cache)
            if cache is not None
            else run_mars_models_list_all(project_root)
        )
        for model in models or []:
            model_id = model.get("id")
            if not isinstance(model_id, str) or model_id.strip() != normalized:
                continue
            return model
        return None

    def mars_alias_entry(
        mars_result: dict[str, object],
        resolved_model_id: str,
    ) -> AliasEntry:
        harness = mars_result.get("harness")
        resolved_harness: HarnessId | None = None
        if isinstance(harness, str) and harness.strip():
            with suppress(ValueError):
                resolved_harness = HarnessId(harness.strip())

        raw_default_effort = mars_result.get("default_effort")
        raw_default_autocompact = mars_result.get("autocompact")
        raw_default_autocompact_pct = mars_result.get("autocompact_pct")
        default_effort = (
            raw_default_effort.strip()
            if isinstance(raw_default_effort, str) and raw_default_effort.strip()
            else None
        )
        default_autocompact = (
            raw_default_autocompact
            if isinstance(raw_default_autocompact, int)
            and not isinstance(raw_default_autocompact, bool)
            else None
        )
        default_autocompact_pct = (
            raw_default_autocompact_pct
            if isinstance(raw_default_autocompact_pct, int)
            and not isinstance(raw_default_autocompact_pct, bool)
            else None
        )

        return AliasEntry(
            alias=str(mars_result.get("name", "") or ""),
            model_id=ModelId(resolved_model_id),
            resolved_harness=resolved_harness,
            description=str(mars_result.get("description", "") or "") or None,
            default_effort=default_effort,
            default_autocompact=default_autocompact,
            default_autocompact_pct=default_autocompact_pct,
        )

    # Step 1: Try mars resolve (alias + harness in one call) before the
    # expensive all-models exact-ID guard.
    mars_result = (
        cached_mars_models_resolve(normalized, project_root, cache=cache)
        if cache is not None
        else run_mars_models_resolve(normalized, project_root)
    )
    if mars_result is not None:
        model_id = mars_result.get("model_id")
        if isinstance(model_id, str) and model_id.strip():
            resolved_model_id = model_id.strip()

            if resolved_model_id == normalized:
                return mars_alias_entry(mars_result, resolved_model_id)

            # mars can prefix-match literal IDs (for example gpt-5.4 ->
            # gpt-5.4-mini). Only pay for all-model discovery when mars
            # resolved to a different ID and the literal exact-ID guard matters.
            exact_id_match = find_exact_id_match()
            if exact_id_match is not None:
                return exact_id_alias_entry(exact_id_match)

            return mars_alias_entry(mars_result, resolved_model_id)

    # Step 2: Raw model ID passthrough for harness fallback elsewhere. Do not
    # call the expensive all-model exact-ID guard when mars cannot resolve the
    # input; the guard only exists for mars prefix-match collisions where mars
    # returned a different model ID than the literal input.
    return AliasEntry(alias="", model_id=ModelId(normalized), resolved_harness=None)


__all__ = ["AliasEntry", "MarsResultCache", "ModelId", "resolve_model"]
