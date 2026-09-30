"""Raw Mars model catalog listing operation."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_serializer

from meridian.lib.catalog.model_aliases import run_mars_models_catalog
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.types import ModelId
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import async_from_sync


class ModelsListInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: str | None = None


class CatalogModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: ModelId
    provider: str | None = None
    cost_input: float | None = None
    cost_output: float | None = None
    cost_cache_read: float | None = None
    cost_cache_write: float | None = None
    cost_reasoning: float | None = None
    context_limit: int | None = None
    output_limit: int | None = None
    release_date: str | None = None
    cost_tier: str | None = None
    description: str | None = None

    def to_wire(self) -> dict[str, object]:
        """Compact JSON projection for model listings."""
        wire: dict[str, object] = {"model_id": str(self.model_id)}
        if self.provider and self.provider.strip():
            wire["provider"] = self.provider
        if self.cost_input is not None:
            wire["cost_input"] = self.cost_input
        if self.cost_output is not None:
            wire["cost_output"] = self.cost_output
        if self.cost_cache_read is not None:
            wire["cost_cache_read"] = self.cost_cache_read
        if self.cost_cache_write is not None:
            wire["cost_cache_write"] = self.cost_cache_write
        if self.cost_reasoning is not None:
            wire["cost_reasoning"] = self.cost_reasoning
        if self.context_limit is not None:
            wire["context_limit"] = self.context_limit
        if self.output_limit is not None:
            wire["output_limit"] = self.output_limit
        if self.release_date and self.release_date.strip():
            wire["release_date"] = self.release_date
        if self.cost_tier and self.cost_tier.strip():
            wire["cost_tier"] = self.cost_tier
        if self.description and self.description.strip():
            wire["description"] = self.description
        return wire

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        from meridian.lib.core.formatting import kv_block

        pairs: list[tuple[str, str | None]] = [
            ("Model", str(self.model_id)),
            ("Provider", self.provider),
            ("Description", self.description),
            ("Released", self.release_date),
            ("Cost", self.cost_tier),
            ("Cost input", _format_float(self.cost_input)),
            ("Cost output", _format_float(self.cost_output)),
            ("Cost cache read", _format_float(self.cost_cache_read)),
            ("Cost cache write", _format_float(self.cost_cache_write)),
            ("Cost reasoning", _format_float(self.cost_reasoning)),
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

        header = ["MODEL", "PROVIDER", "COST", "RELEASED"]
        rows: list[list[str]] = []
        for model in self.models:
            rows.append(
                [
                    str(model.model_id),
                    model.provider or "",
                    model.cost_tier or "",
                    model.release_date or "",
                ]
            )
        required_indices = {0}
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


def _mars_catalog_entry_to_model(entry: dict[str, object]) -> CatalogModel | None:
    model_id_value = _parse_optional_str(entry.get("id"))
    if model_id_value is None:
        return None

    cost_input = _parse_optional_float(entry.get("cost_input"))

    return CatalogModel(
        model_id=ModelId(model_id_value),
        provider=_parse_optional_str(entry.get("provider")),
        cost_input=cost_input,
        cost_output=_parse_optional_float(entry.get("cost_output")),
        cost_cache_read=_parse_optional_float(entry.get("cost_cache_read")),
        cost_cache_write=_parse_optional_float(entry.get("cost_cache_write")),
        cost_reasoning=_parse_optional_float(entry.get("cost_reasoning")),
        context_limit=_parse_optional_int(entry.get("context_window")),
        output_limit=_parse_optional_int(entry.get("max_output")),
        release_date=_parse_optional_str(entry.get("release_date")),
        cost_tier=_cost_tier(cost_input),
        description=_parse_optional_str(entry.get("description")),
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
    mars_catalog = run_mars_models_catalog(project_root=root)
    if mars_catalog is None:
        return ModelsListOutput(models=())

    catalog_models = [
        model
        for entry in mars_catalog
        if (model := _mars_catalog_entry_to_model(entry)) is not None
    ]
    return ModelsListOutput(models=tuple(catalog_models))


models_list = async_from_sync(models_list_sync)
