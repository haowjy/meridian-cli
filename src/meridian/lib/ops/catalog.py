"""Catalog discovery operations for models and skills."""

from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, model_serializer

from meridian.lib.catalog.model_aliases import run_mars_models_list_all
from meridian.lib.catalog.models import AliasEntry
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync


class ModelsListInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None
    all: bool = False
    show_superseded: bool = False


class CatalogModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: ModelId
    harness: HarnessId | None
    aliases: tuple[AliasEntry, ...] = ()
    name: str | None = None
    family: str | None = None
    provider: str | None = None
    cost_input: float | None = None
    cost_output: float | None = None
    context_limit: int | None = None
    output_limit: int | None = None
    capabilities: tuple[str, ...] = ()
    release_date: str | None = None
    cost_tier: str | None = None
    description: str | None = None
    pinned: bool = False

    def to_wire(self) -> dict[str, object]:
        """Compact JSON projection for model listings."""
        wire: dict[str, object] = {
            "model_id": str(self.model_id),
            "harness": str(self.harness) if self.harness is not None else None,
        }

        aliases = [alias.model_dump(exclude_none=True) for alias in self.aliases]
        if aliases:
            wire["aliases"] = aliases

        if self.name and self.name.strip():
            wire["name"] = self.name
        if self.family and self.family.strip():
            wire["family"] = self.family
        if self.provider and self.provider.strip():
            wire["provider"] = self.provider
        if self.cost_input is not None:
            wire["cost_input"] = self.cost_input
        if self.cost_output is not None:
            wire["cost_output"] = self.cost_output
        if self.context_limit is not None:
            wire["context_limit"] = self.context_limit
        if self.output_limit is not None:
            wire["output_limit"] = self.output_limit
        if self.capabilities:
            wire["capabilities"] = list(self.capabilities)
        if self.release_date and self.release_date.strip():
            wire["release_date"] = self.release_date
        if self.cost_tier and self.cost_tier.strip():
            wire["cost_tier"] = self.cost_tier
        if self.description and self.description.strip():
            wire["description"] = self.description
        if self.pinned:
            wire["pinned"] = True

        return wire

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        from meridian.lib.core.formatting import kv_block

        alias_names = ", ".join(alias.alias for alias in self.aliases) or None
        alias_details = ", ".join(_format_alias_detail(alias) for alias in self.aliases) or None
        capabilities = ", ".join(self.capabilities) or None
        pairs: list[tuple[str, str | None]] = [
            ("Model", str(self.model_id)),
            ("Harness", _display_harness(self.harness)),
            ("Name", self.name),
            ("Family", self.family),
            ("Provider", self.provider),
            ("Aliases", alias_names),
            ("Alias details", alias_details),
            ("Description", self.description),
            ("Capabilities", capabilities),
            ("Released", self.release_date),
            ("Cost", self.cost_tier),
            ("Cost input", _format_float(self.cost_input)),
            ("Cost output", _format_float(self.cost_output)),
            ("Context limit", _format_int(self.context_limit)),
            ("Output limit", _format_int(self.output_limit)),
        ]
        return kv_block(pairs)


class ModelsListOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    models: tuple[CatalogModel, ...]

    @model_serializer(mode="plain")
    def _serialize(self) -> dict[str, object]:
        return {"models": [model.to_wire() for model in self.models]}

    def format_text(self, ctx: FormatContext | None = None) -> str:
        """Columnar model table for text output mode."""
        if not self.models:
            return "(no models)"
        from meridian.lib.core.formatting import tabular

        header = ["MODEL", "HARNESS", "ALIAS", "PROVIDER", "COST", "RELEASED"]
        rows: list[list[str]] = []
        for model in self.models:
            rows.append(
                [
                    str(model.model_id),
                    _display_harness(model.harness),
                    ",".join(alias.alias for alias in model.aliases),
                    model.provider or "",
                    model.cost_tier or "",
                    model.release_date or "",
                ]
            )
        required_indices = {0, 1}
        keep_indices = [
            index
            for index in range(len(header))
            if index in required_indices or any(row[index] for row in rows)
        ]
        filtered_header = [header[index] for index in keep_indices]
        filtered_rows = [[row[index] for index in keep_indices] for row in rows]
        table = tabular([filtered_header, *filtered_rows])

        # Add description sub-lines
        table_lines = table.split("\n")
        result_lines: list[str] = []
        # First line is header
        if table_lines:
            result_lines.append(table_lines[0])
        # Remaining lines correspond to models
        for i, model in enumerate(self.models):
            line_index = i + 1
            if line_index < len(table_lines):
                result_lines.append(table_lines[line_index])
            if model.description:
                # Indent description under the model line
                result_lines.append(f"  {model.description}")
        return "\n".join(result_lines)


def _project_root(project_root: str | None) -> Path | None:
    explicit = Path(project_root).expanduser().resolve() if project_root is not None else None
    return resolve_project_root_resolution(explicit).project_root


def _format_float(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:g}"


def _format_int(value: int | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _format_alias_detail(alias: AliasEntry) -> str:
    return alias.alias


def _display_harness(harness: HarnessId | None) -> str:
    return str(harness) if harness is not None else "—"


def _parse_optional_str(value: object) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _parse_optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _parse_optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return int(float(normalized))
        except ValueError:
            return None
    return None


def _parse_capabilities(value: object) -> tuple[str, ...]:
    raw_values: list[object]
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, list):
        raw_values = cast("list[object]", value)
    elif isinstance(value, tuple):
        raw_values = list(cast("tuple[object, ...]", value))
    elif isinstance(value, set):
        raw_values = list(cast("set[object]", value))
    else:
        return ()

    capabilities: set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, str):
            continue
        normalized = raw.strip().lower()
        if normalized:
            capabilities.add(normalized)
    return tuple(sorted(capabilities))


def _parse_harness(value: object) -> HarnessId | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return HarnessId(normalized)
    except ValueError:
        return None


def _parse_matched_aliases(
    *,
    model_id: ModelId,
    harness: HarnessId | None,
    raw_aliases: object,
) -> tuple[AliasEntry, ...]:
    if not isinstance(raw_aliases, list):
        return ()

    aliases: list[AliasEntry] = []
    for raw_alias in cast("list[object]", raw_aliases):
        alias_name = _parse_optional_str(raw_alias)
        if alias_name is None:
            continue
        aliases.append(
            AliasEntry(
                alias=alias_name,
                model_id=model_id,
                resolved_harness=harness,
            )
        )
    return tuple(sorted(aliases, key=lambda alias: alias.alias))


def _mars_all_entry_to_catalog_model(entry: dict[str, object]) -> CatalogModel | None:
    model_id_value = _parse_optional_str(entry.get("id"))
    if model_id_value is None:
        return None

    model_id = ModelId(model_id_value)
    harness = _parse_harness(entry.get("harness"))
    cost_input = _parse_optional_float(entry.get("cost_input"))

    return CatalogModel(
        model_id=model_id,
        harness=harness,
        aliases=_parse_matched_aliases(
            model_id=model_id,
            harness=harness,
            raw_aliases=entry.get("matched_aliases"),
        ),
        name=_parse_optional_str(entry.get("name")),
        family=_parse_optional_str(entry.get("family")),
        provider=_parse_optional_str(entry.get("provider")),
        cost_input=cost_input,
        cost_output=_parse_optional_float(entry.get("cost_output")),
        context_limit=_parse_optional_int(entry.get("context_limit")),
        output_limit=_parse_optional_int(entry.get("output_limit")),
        capabilities=_parse_capabilities(entry.get("capabilities")),
        release_date=_parse_optional_str(entry.get("release_date")),
        cost_tier=_cost_tier(cost_input),
        description=_parse_optional_str(entry.get("description")),
        pinned=bool(entry.get("pinned")),
    )


def _cost_tier(cost_input: float | None) -> str | None:
    """Map input cost ($/M tokens) to a human-readable tier."""
    if cost_input is None:
        return None
    if cost_input < 1.0:
        return "$"
    if cost_input < 5.0:
        return "$$"
    extra_dollar_count = int((cost_input - 5.0) // 5.0)
    return "$" * (3 + extra_dollar_count)


def models_list_sync(payload: ModelsListInput) -> ModelsListOutput:
    root = _project_root(payload.project_root)
    mars_models = run_mars_models_list_all(project_root=root)
    if mars_models is None:
        return ModelsListOutput(models=())

    catalog_models = [
        model
        for entry in mars_models
        if (model := _mars_all_entry_to_catalog_model(entry)) is not None
    ]
    return ModelsListOutput(models=tuple(catalog_models))


models_list = async_from_sync(models_list_sync)
