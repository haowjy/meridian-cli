"""Typed schema and validation for configured lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, get_args

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
    EVENTS,
    Hook,
    HookEventName,
    HookWhen,
    HookWhenField,
    SpawnKind,
    SpawnStatus,
)
from meridian.lib.utils.json_shapes import json_list, json_object

_INTERVAL_PATTERN = re.compile(r"^\d+[smhd]$")
_HOOK_NAME_COMPONENT_PATTERN = re.compile(r"[^a-z0-9._-]+")
_HOOK_NAME_REPEATED_DASH_PATTERN = re.compile(r"-+")
_KNOWN_HOOK_EVENTS = frozenset(get_args(HookEventName))
_KNOWN_SPAWN_KINDS = frozenset(get_args(SpawnKind))
_KNOWN_SPAWN_STATUSES = frozenset(get_args(SpawnStatus))
_WHEN_FIELDS: tuple[HookWhenField, ...] = ("status", "agent", "kind")
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


@dataclass(frozen=True)
class HookConfigDeprecationWindow:
    """Config breaks accepted until this window changes to error."""

    mode: Literal["warn", "error"]
    event_replacements: tuple[tuple[str, HookEventName], ...]
    newly_live_events: frozenset[str]
    ignored_fields: frozenset[str]
    removed_status_values: frozenset[str]
    impossible_explicit_when: bool


# Flip this table's mode to "error" in the next minor release. The metadata
# below is also the complete inventory of compatibility behavior to remove.
NEXT_MINOR_HOOK_CONFIG_BREAKS = HookConfigDeprecationWindow(
    mode="warn",
    event_replacements=(
        ("spawn.start", "spawn.started"),
        ("spawn.running", "spawn.started"),
        ("spawn.finalized", "spawn.finished"),
        ("work.start", "work.activated"),
        ("work.started", "work.activated"),
        ("session.responded", "notify.your_turn"),
        ("session.cache_expiring", "notify.cache_expiring"),
    ),
    newly_live_events=frozenset(("spawn.start", "work.start")),
    ignored_fields=frozenset(("failure_policy",)),
    removed_status_values=frozenset(("skipped",)),
    impossible_explicit_when=True,
)


@dataclass(frozen=True)
class _DeprecationIssue:
    source: str
    warning: str
    error: str


@dataclass(frozen=True)
class NormalizedHookRow:
    """Typed row plus compatibility metadata retained through registration."""

    spec: HookSpec
    configured_event: str | None = None
    deprecations: tuple[_DeprecationIssue, ...] = ()


@dataclass(frozen=True)
class HookRowLoad:
    """Runtime registrations and at most one warning for one config row."""

    hooks: tuple[Hook, ...]
    warning: str | None = None


def _normalized_string(value: object) -> str | object:
    return value.strip() if isinstance(value, str) else value


def _hook_event(value: object) -> str | object:
    normalized = _normalized_string(value)
    if not isinstance(normalized, str):
        return normalized
    if not normalized:
        raise ValueError("expected non-empty string")
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
        raise ValueError(f"expected interval format '\\d+[smhd]', got {normalized!r}")
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
type SpawnKindSpec = Annotated[SpawnKind, BeforeValidator(_spawn_kind)]
type SpawnStatusSpec = Annotated[SpawnStatus, BeforeValidator(_spawn_status)]
type PositiveTimeoutSecs = Annotated[int, BeforeValidator(_positive_timeout), Field(gt=0)]


def _closed_tuple(allowed: frozenset[str]) -> Callable[[object], object]:
    def validate(value: object) -> object:
        values = json_list(value)
        if values is not None:
            for item in values:
                normalized = _normalized_string(item)
                if isinstance(normalized, str) and normalized and normalized not in allowed:
                    valid = ", ".join(sorted(allowed))
                    raise ValueError(f"expected values in [{valid}], got {normalized!r}")
        return tuple(values) if values is not None else value

    return validate


type SpawnKindTupleSpec = Annotated[
    tuple[SpawnKindSpec, ...], BeforeValidator(_closed_tuple(_KNOWN_SPAWN_KINDS))
]
type SpawnStatusTupleSpec = Annotated[
    tuple[SpawnStatusSpec, ...], BeforeValidator(_closed_tuple(_KNOWN_SPAWN_STATUSES))
]


class HookWhenSpec(BaseModel):
    """Validated conditional fields from one hook row."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    status: SpawnStatusTupleSpec | None = None
    agent: NonEmptyString | None = None
    kind: SpawnKindTupleSpec | None = None


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


def _prepare_deprecated_row(
    raw_value: object,
    *,
    source: str,
) -> tuple[object, str | None, tuple[_DeprecationIssue, ...]]:
    row = json_object(raw_value)
    if row is None:
        return raw_value, None, ()

    prepared = dict(row)
    configured_event: str | None = None
    issues: list[_DeprecationIssue] = []
    replacements = dict(NEXT_MINOR_HOOK_CONFIG_BREAKS.event_replacements)

    raw_event = prepared.get("event")
    normalized_event = _normalized_string(raw_event)
    if isinstance(normalized_event, str) and normalized_event in replacements:
        replacement = replacements[normalized_event]
        configured_event = normalized_event
        prepared["event"] = replacement
        now_runs = (
            " This row will now run; it never fired under the deprecated name."
            if normalized_event in NEXT_MINOR_HOOK_CONFIG_BREAKS.newly_live_events
            else ""
        )
        issues.append(
            _DeprecationIssue(
                source=f"{source}.event",
                warning=(
                    f"`{normalized_event}` is deprecated; use `{replacement}`."
                    f"{now_runs} It will be rejected in the next minor release."
                ),
                error=f"`{normalized_event}` was removed; use `{replacement}`",
            )
        )

    for field in NEXT_MINOR_HOOK_CONFIG_BREAKS.ignored_fields:
        if field not in prepared:
            continue
        prepared.pop(field)
        issues.append(
            _DeprecationIssue(
                source=f"{source}.{field}",
                warning=(
                    f"`{field}` is deprecated; it has no effect and will be rejected "
                    "in the next minor release."
                ),
                error=f"`{field}` was removed; hook failures always continue",
            )
        )

    raw_when = json_object(prepared.get("when"))
    if raw_when is not None:
        prepared_when = dict(raw_when)
        raw_statuses = prepared_when.get("status")
        statuses = json_list(raw_statuses)
        if statuses is not None:
            retained_statuses: list[object] = []
            for status in statuses:
                normalized_status = _normalized_string(status)
                if (
                    isinstance(normalized_status, str)
                    and normalized_status in NEXT_MINOR_HOOK_CONFIG_BREAKS.removed_status_values
                ):
                    continue
                retained_statuses.append(status)
            if len(retained_statuses) != len(statuses):
                prepared_when["status"] = retained_statuses
                removed = ", ".join(
                    f'"{value}"'
                    for value in sorted(NEXT_MINOR_HOOK_CONFIG_BREAKS.removed_status_values)
                )
                issues.append(
                    _DeprecationIssue(
                        source=f"{source}.when.status",
                        warning=(
                            f"`when.status = [{removed}]` is deprecated and has been dropped "
                            "because it never matched. It will be rejected in the next minor "
                            "release."
                        ),
                        error=f"when.status values [{removed}] were removed",
                    )
                )
        prepared["when"] = prepared_when

    return prepared, configured_event, tuple(issues)


def normalize_hook_row(raw_value: object, *, source: str) -> NormalizedHookRow:
    """Validate one hook row and retain its compatibility-window metadata."""

    prepared, configured_event, deprecations = _prepare_deprecated_row(
        raw_value,
        source=source,
    )
    warn_unknown_model_keys(prepared, model=HookSpec, source=source)
    try:
        spec = HookSpec.model_validate(prepared)
    except ValidationError as error:
        raise config_validation_error(error, source=source) from None
    return NormalizedHookRow(
        spec=spec,
        configured_event=configured_event,
        deprecations=deprecations,
    )


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


def _unsupported_when_field(
    when: HookWhen | None,
    event: HookEventName,
) -> HookWhenField | None:
    if when is None:
        return None
    for field in _WHEN_FIELDS:
        if getattr(when, field) is not None and field not in EVENTS[event].when_fields:
            return field
    return None


def _impossible_when_issue(
    when: HookWhen | None,
    event: HookEventName,
    *,
    row_source: str,
) -> _DeprecationIssue | None:
    field = _unsupported_when_field(when, event)
    if field is None:
        return None

    if field == "status":
        warning = (
            "`when.status` condition is only available for `spawn.finished`, "
            f"got `{event}`; the condition can never match and the row is skipped. "
            "It will be rejected in the next minor release."
        )
        error = f"condition is only available for 'spawn.finished', got {event!r}"
    else:
        warning = (
            f"`when.{field}` condition is unavailable for `{event}`; the condition can "
            "never match and the row is skipped. It will be rejected in the next minor "
            "release."
        )
        error = f"condition is unavailable for {event!r}"
    return _DeprecationIssue(
        source=f"{row_source}.when.{field}",
        warning=warning,
        error=error,
    )


def _deprecation_warning(
    *,
    hook_name: str,
    issues: tuple[_DeprecationIssue, ...],
) -> str | None:
    if not issues:
        return None
    if NEXT_MINOR_HOOK_CONFIG_BREAKS.mode == "error":
        issue = issues[0]
        raise ValueError(f"Invalid value for '{issue.source}': {issue.error}.")
    return f"hook {hook_name!r}: " + " ".join(issue.warning for issue in issues)


def hooks_from_spec(
    normalized: NormalizedHookRow,
    *,
    source: str,
    row_source: str,
) -> HookRowLoad:
    """Apply semantic validation and construct runtime hooks from one typed spec."""

    spec = normalized.spec
    deprecations = list(normalized.deprecations)
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

    when = _parse_when(spec.when)
    events: tuple[HookEventName, ...]
    enabled_events: frozenset[HookEventName]
    if spec.event is not None:
        events = (spec.event,)
        if NEXT_MINOR_HOOK_CONFIG_BREAKS.impossible_explicit_when:
            impossible = _impossible_when_issue(
                when,
                spec.event,
                row_source=row_source,
            )
            if impossible is not None:
                deprecations.append(impossible)
                return HookRowLoad(
                    hooks=(),
                    warning=_deprecation_warning(
                        hook_name=name,
                        issues=tuple(deprecations),
                    ),
                )
        enabled_events = frozenset(events)
    elif builtin is not None:
        events = builtin.default_events
        enabled_events = frozenset(
            event for event in events if _unsupported_when_field(when, event) is None
        )
        if not enabled_events:
            raise ValueError(
                f"Invalid hook config '{row_source}': row had no 'event', and its 'when' "
                "conditions cannot match any builtin default event; set 'event' explicitly "
                "to an event that supplies those fields."
            )
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

    warning = _deprecation_warning(
        hook_name=name,
        issues=tuple(deprecations),
    )
    return HookRowLoad(
        hooks=tuple(
            Hook(
                name=name,
                event=event,
                source=source,
                command=command,
                builtin=spec.builtin,
                timeout_secs=spec.timeout_secs,
                interval=interval,
                enabled=spec.enabled and event in enabled_events,
                priority=spec.priority,
                require_serial=spec.require_serial,
                when=when,
                exclude=spec.exclude,
                options=options,
                remote=spec.remote,
                configured_event=normalized.configured_event,
                deprecated=warning is not None,
            )
            for event in events
        ),
        warning=warning,
    )
