"""Typed schema and validation for configured lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Annotated, get_args

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from meridian.lib.config.errors import config_validation_error, warn_unknown_model_keys
from meridian.lib.hooks.builtin import BUILTIN_HOOKS, BuiltinHook
from meridian.lib.hooks.types import (
    FailurePolicy,
    Hook,
    HookEventName,
    HookWhen,
    SpawnKind,
    SpawnStatus,
)
from meridian.lib.utils.json_shapes import json_list

_INTERVAL_PATTERN = re.compile(r"^\d+[smhd]$")
_HOOK_NAME_COMPONENT_PATTERN = re.compile(r"[^a-z0-9._-]+")
_HOOK_NAME_REPEATED_DASH_PATTERN = re.compile(r"-+")
_KNOWN_HOOK_EVENTS = frozenset(get_args(HookEventName))
_REPLACED_HOOK_EVENTS = {
    "spawn.start": "spawn.started",
    "spawn.running": "spawn.started",
    "spawn.finalized": "spawn.finished",
    "work.start": "work.activated",
    "work.started": "work.activated",
    "session.responded": "notify.your_turn",
    "session.cache_expiring": "notify.cache_expiring",
}
_KNOWN_FAILURE_POLICIES = frozenset(get_args(FailurePolicy))
_KNOWN_SPAWN_KINDS = frozenset(get_args(SpawnKind.__value__))
_KNOWN_SPAWN_STATUSES = frozenset(get_args(SpawnStatus))
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _normalized_string(value: object) -> str | object:
    return value.strip() if isinstance(value, str) else value


def _hook_event(value: object) -> str | object:
    normalized = _normalized_string(value)
    if not isinstance(normalized, str):
        return normalized
    if not normalized:
        raise ValueError("expected non-empty string")
    replacement = _REPLACED_HOOK_EVENTS.get(normalized)
    if replacement is not None:
        raise ValueError(f"{normalized!r} was removed; use {replacement!r}")
    if normalized not in _KNOWN_HOOK_EVENTS:
        valid = ", ".join(sorted(_KNOWN_HOOK_EVENTS))
        raise ValueError(f"expected one of [{valid}], got {normalized!r}")
    return normalized


def _hook_interval(value: object) -> str | object:
    normalized = _normalized_string(value)
    if not isinstance(normalized, str):
        return normalized
    if not normalized:
        raise ValueError("expected non-empty string")
    if not _INTERVAL_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"expected interval format '\\d+[smhd]', got {normalized!r}"
        )
    return normalized


def _failure_policy(value: object) -> str | object:
    normalized = _normalized_string(value)
    if not isinstance(normalized, str):
        return normalized
    if not normalized:
        raise ValueError("expected non-empty string")
    if normalized not in _KNOWN_FAILURE_POLICIES:
        valid = ", ".join(sorted(_KNOWN_FAILURE_POLICIES))
        raise ValueError(f"expected one of [{valid}], got {normalized!r}")
    return normalized


def _spawn_status(value: object) -> str | object:
    normalized = _normalized_string(value)
    if not isinstance(normalized, str):
        return normalized
    if not normalized:
        raise ValueError("expected non-empty string")
    return normalized


def _spawn_kind(value: object) -> str | object:
    return _normalized_string(value)


def _positive_timeout(value: object) -> object:
    if isinstance(value, int) and not isinstance(value, bool) and value <= 0:
        raise ValueError(f"expected int > 0, got {value!r}")
    return value


type HookEventSpec = Annotated[HookEventName, BeforeValidator(_hook_event)]
type HookIntervalSpec = Annotated[
    str,
    BeforeValidator(_hook_interval),
    StringConstraints(pattern=r"^\d+[smhd]$"),
]
type FailurePolicySpec = Annotated[FailurePolicy, BeforeValidator(_failure_policy)]
type SpawnKindSpec = Annotated[SpawnKind, BeforeValidator(_spawn_kind)]
type SpawnStatusSpec = Annotated[SpawnStatus, BeforeValidator(_spawn_status)]
type PositiveTimeoutSecs = Annotated[int, BeforeValidator(_positive_timeout), Field(gt=0)]


class HookWhenSpec(BaseModel):
    """Validated conditional fields from one hook row."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    status: tuple[SpawnStatusSpec, ...] | None = None
    agent: NonEmptyString | None = None
    kind: tuple[SpawnKindSpec, ...] | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _tuple_status(cls, value: object) -> object:
        values = json_list(value)
        if values is not None:
            for item in values:
                normalized = _normalized_string(item)
                if (
                    isinstance(normalized, str)
                    and normalized
                    and normalized not in _KNOWN_SPAWN_STATUSES
                ):
                    valid = ", ".join(sorted(_KNOWN_SPAWN_STATUSES))
                    raise ValueError(
                        f"expected values in [{valid}], got {normalized!r}"
                    )
        return tuple(values) if values is not None else value

    @field_validator("kind", mode="before")
    @classmethod
    def _tuple_kind(cls, value: object) -> object:
        values = json_list(value)
        if values is not None:
            for item in values:
                normalized = _normalized_string(item)
                if (
                    isinstance(normalized, str)
                    and normalized
                    and normalized not in _KNOWN_SPAWN_KINDS
                ):
                    valid = ", ".join(sorted(_KNOWN_SPAWN_KINDS))
                    raise ValueError(
                        f"expected values in [{valid}], got {normalized!r}"
                    )
        return tuple(values) if values is not None else value


class HookSpec(BaseModel):
    """Validated file representation of one configured hook."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    name: NonEmptyString | None = None
    builtin: NonEmptyString | None = None
    command: NonEmptyString | None = None
    event: HookEventSpec | None = None
    timeout_secs: PositiveTimeoutSecs | None = None
    interval: HookIntervalSpec | None = None
    enabled: bool = True
    priority: int = 0
    failure_policy: FailurePolicySpec | None = None
    require_serial: bool = False
    when: HookWhenSpec | None = None
    exclude: tuple[NonEmptyString, ...] = ()
    remote: NonEmptyString | None = None
    options: dict[str, object] = Field(default_factory=dict)

    @field_validator("exclude", mode="before")
    @classmethod
    def _tuple_exclude(cls, value: object) -> object:
        values = json_list(value)
        return tuple(values) if values is not None else value

def hook_row_values(raw_value: object, *, source: str) -> tuple[object, ...]:
    """Return raw hook rows while keeping hook-section shape validation here."""

    values = json_list(raw_value)
    if values is None:
        raise ValueError(
            f"Invalid value for '{source}': expected array[table], "
            f"got {type(raw_value).__name__} ({raw_value!r})."
        )
    return tuple(values)


def normalize_hook_row(raw_value: object, *, source: str) -> HookSpec:
    """Validate one hook row and emit its unknown-key diagnostics once."""

    warn_unknown_model_keys(raw_value, model=HookSpec, source=source)
    try:
        return HookSpec.model_validate(raw_value)
    except ValidationError as error:
        raise config_validation_error(error, source=source) from None


def _slugify_hook_name_component(raw: str) -> str:
    normalized = raw.strip().lower()
    normalized = _HOOK_NAME_COMPONENT_PATTERN.sub("-", normalized)
    normalized = _HOOK_NAME_REPEATED_DASH_PATTERN.sub("-", normalized)
    normalized = normalized.strip("-.")
    return normalized[:48].strip("-.")


def _synthesize_builtin_hook_name(builtin: BuiltinHook, options: Mapping[str, object]) -> str:
    """Name an unnamed row by its builtin and the options that pick its target."""

    identity = {
        key: options[key] for key in builtin.identity_options if options.get(key) is not None
    }
    if not identity:
        return builtin.name

    payload = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    slug_source = next(
        (value for value in identity.values() if isinstance(value, str) and value.strip()), ""
    )
    slug = _slugify_hook_name_component(slug_source) if slug_source else ""
    return f"{builtin.name}:{slug}-{digest}" if slug else f"{builtin.name}:{digest}"


def _parse_when(spec: HookWhenSpec | None) -> HookWhen | None:
    if spec is None:
        return None

    status = spec.status or None
    kind = spec.kind or None
    if status is None and spec.agent is None and kind is None:
        return None
    return HookWhen(
        status=status,
        agent=spec.agent,
        kind=kind,
    )


def _validate_when_events(
    when: HookWhen | None,
    events: tuple[HookEventName, ...],
    *,
    row_source: str,
) -> None:
    if when is None:
        return

    if when.status is not None:
        unsupported = next((event for event in events if event != "spawn.finished"), None)
        if unsupported is not None:
            raise ValueError(
                f"Invalid value for '{row_source}.when.status': condition is only available "
                f"for 'spawn.finished', got {unsupported!r}."
            )

    work_event = next((event for event in events if event.startswith("work.")), None)
    if work_event is None:
        return
    if when.agent is not None:
        raise ValueError(
            f"Invalid value for '{row_source}.when.agent': condition is unavailable for "
            f"{work_event!r}."
        )
    if when.kind is not None:
        raise ValueError(
            f"Invalid value for '{row_source}.when.kind': condition is unavailable for "
            f"{work_event!r}."
        )


def hooks_from_spec(
    spec: HookSpec,
    *,
    source: str,
    row_source: str,
) -> tuple[Hook, ...]:
    """Apply semantic validation and construct runtime hooks from one typed spec."""

    command = spec.command
    options = dict(spec.options)
    if spec.remote is not None:
        options.setdefault("remote", spec.remote)

    if command is not None and spec.builtin is not None:
        raise ValueError(
            f"Invalid hook config '{row_source}': 'command' and 'builtin' are mutually exclusive."
        )
    if command is None and spec.builtin is None:
        raise ValueError(
            f"Invalid hook config '{row_source}': expected either 'command' or 'builtin'."
        )

    builtin: BuiltinHook | None = None
    if spec.builtin is not None:
        builtin = BUILTIN_HOOKS.get(spec.builtin)
        if builtin is None:
            valid = ", ".join(sorted(BUILTIN_HOOKS))
            raise ValueError(
                f"Invalid value for '{row_source}.builtin': expected one of [{valid}], "
                f"got {spec.builtin!r}."
            )
        try:
            validated_options = builtin.options_model.model_validate(options)
        except ValidationError as error:
            raise config_validation_error(error, source=f"{row_source}.options") from None
        options = validated_options.model_dump(exclude_none=True)

    name = spec.name
    if name is None:
        if builtin is None:
            raise ValueError(f"Invalid hook config '{row_source}': 'name' is required.")
        name = _synthesize_builtin_hook_name(builtin, options)

    events: tuple[HookEventName, ...]
    if spec.event is not None:
        events = (spec.event,)
    elif builtin is not None:
        events = builtin.default_events
    else:
        raise ValueError(f"Invalid hook config '{row_source}': 'event' is required.")

    if builtin is not None and builtin.supported_events is not None:
        unsupported = tuple(event for event in events if event not in builtin.supported_events)
        if unsupported:
            supported = ", ".join(builtin.supported_events)
            raise ValueError(
                f"Invalid value for '{row_source}.event': builtin {builtin.name!r} supports only "
                f"[{supported}], got {unsupported[0]!r}."
            )

    interval = spec.interval or (builtin.default_interval if builtin is not None else None)

    when = _parse_when(spec.when)
    _validate_when_events(when, events, row_source=row_source)
    return tuple(
        Hook(
            name=name,
            event=event,
            source=source,
            command=command,
            builtin=spec.builtin,
            timeout_secs=spec.timeout_secs,
            interval=interval,
            enabled=spec.enabled,
            priority=spec.priority,
            failure_policy=spec.failure_policy,
            require_serial=spec.require_serial,
            when=when,
            exclude=spec.exclude,
            options=options,
            remote=spec.remote,
        )
        for event in events
    )
