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
    RootModel,
    StringConstraints,
    ValidationError,
    field_validator,
)

from meridian.lib.config.errors import config_validation_error
from meridian.lib.hooks.builtin_registry import (
    BUILTIN_HOOK_REGISTRY,
    get_default_events,
    validate_builtin_options,
)
from meridian.lib.hooks.types import (
    FailurePolicy,
    Hook,
    HookEventName,
    HookWhen,
    SpawnStatus,
)
from meridian.lib.utils.json_shapes import json_list

_INTERVAL_PATTERN = re.compile(r"^\d+[smhd]$")
_HOOK_NAME_COMPONENT_PATTERN = re.compile(r"[^a-z0-9._-]+")
_HOOK_NAME_REPEATED_DASH_PATTERN = re.compile(r"-+")
_KNOWN_HOOK_EVENTS = frozenset(get_args(HookEventName))
_KNOWN_FAILURE_POLICIES = frozenset(get_args(FailurePolicy))
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
type SpawnStatusSpec = Annotated[SpawnStatus, BeforeValidator(_spawn_status)]
type PositiveTimeoutSecs = Annotated[int, BeforeValidator(_positive_timeout), Field(gt=0)]


class HookWhenSpec(BaseModel):
    """Validated conditional fields from one hook row."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    status: tuple[SpawnStatusSpec, ...] | None = None
    agent: NonEmptyString | None = None

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

    @property
    def display_name(self) -> str:
        """Return the label used by ``config show`` summaries."""

        return self.name or self.event or self.builtin or "hook"


class HookConfigSection(RootModel[tuple[HookSpec, ...]]):
    """Validated representation of one ``[[hooks]]`` array."""

    model_config = ConfigDict(frozen=True, strict=True)

    @field_validator("root", mode="before")
    @classmethod
    def _tuple_rows(cls, value: object) -> object:
        values = json_list(value)
        if values is None:
            raise ValueError(
                "expected array[table], "
                f"got {type(value).__name__} ({value!r})"
            )
        return tuple(values)


def normalize_hooks_array(raw_value: object, *, source: str) -> tuple[HookSpec, ...]:
    """Parse hooks without re-emitting config-loader unknown-key diagnostics."""

    try:
        return HookConfigSection.model_validate(raw_value).root
    except ValidationError as error:
        raise config_validation_error(error, source=source) from None


def _slugify_hook_name_component(raw: str) -> str:
    normalized = raw.strip().lower()
    normalized = _HOOK_NAME_COMPONENT_PATTERN.sub("-", normalized)
    normalized = _HOOK_NAME_REPEATED_DASH_PATTERN.sub("-", normalized)
    normalized = normalized.strip("-.")
    return normalized[:48].strip("-.")


def _synthesize_builtin_hook_name(
    *,
    builtin: str,
    remote: str | None,
    options: Mapping[str, object],
) -> str:
    identity: dict[str, object] = dict(options)
    if remote is not None:
        identity["remote"] = remote
    if not identity:
        return builtin

    payload = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    slug_source = ""
    for candidate_key in ("remote", "path"):
        candidate = identity.get(candidate_key)
        if isinstance(candidate, str) and candidate.strip():
            slug_source = candidate
            break
    slug = _slugify_hook_name_component(slug_source) if slug_source else ""
    return f"{builtin}:{slug}-{digest}" if slug else f"{builtin}:{digest}"


def _parse_when(spec: HookWhenSpec | None) -> HookWhen | None:
    if spec is None:
        return None

    if spec.status is None and spec.agent is None:
        return None
    return HookWhen(
        status=spec.status,
        agent=spec.agent,
    )


def hooks_from_spec(
    spec: HookSpec,
    *,
    source: str,
    row_source: str,
    auto_registered: bool = False,
) -> tuple[Hook, ...]:
    """Apply semantic validation and construct runtime hooks from one typed spec."""

    command = spec.command
    builtin = spec.builtin
    options = dict(spec.options)
    if spec.remote is not None:
        options.setdefault("remote", spec.remote)

    if command is not None and builtin is not None:
        raise ValueError(
            f"Invalid hook config '{row_source}': 'command' and 'builtin' are mutually exclusive."
        )
    if command is None and builtin is None:
        raise ValueError(
            f"Invalid hook config '{row_source}': expected either 'command' or 'builtin'."
        )

    name = spec.name
    if name is None:
        if builtin is not None:
            name = _synthesize_builtin_hook_name(
                builtin=builtin,
                remote=spec.remote,
                options=options,
            )
        else:
            raise ValueError(f"Invalid hook config '{row_source}': 'name' is required.")

    if builtin is not None and builtin not in BUILTIN_HOOK_REGISTRY:
        valid = ", ".join(sorted(BUILTIN_HOOK_REGISTRY.keys()))
        raise ValueError(
            f"Invalid value for '{row_source}.builtin': expected one of [{valid}], got {builtin!r}."
        )
    if builtin is not None:
        try:
            validate_builtin_options(builtin, options)
        except KeyError:
            valid = ", ".join(sorted(BUILTIN_HOOK_REGISTRY.keys()))
            raise ValueError(
                f"Invalid value for '{row_source}.builtin': expected one of [{valid}], "
                f"got {builtin!r}."
            ) from None
        except ValueError as exc:
            raise ValueError(f"Invalid hook config '{row_source}': {exc}") from exc

    default_events = get_default_events(builtin) if builtin is not None else ()
    events: tuple[HookEventName, ...]
    if spec.event is not None:
        events = (spec.event,)
    elif default_events:
        events = default_events
    else:
        raise ValueError(f"Invalid hook config '{row_source}': 'event' is required.")

    default_interval = BUILTIN_HOOK_REGISTRY[builtin].interval if builtin is not None else None
    interval = spec.interval or default_interval

    when = _parse_when(spec.when)
    return tuple(
        Hook(
            name=name,
            event=event,
            source=source,
            command=command,
            builtin=builtin,
            timeout_secs=spec.timeout_secs,
            interval=interval,
            enabled=spec.enabled,
            priority=spec.priority,
            failure_policy=spec.failure_policy,
            require_serial=spec.require_serial,
            when=when,
            exclude=spec.exclude,
            options=options,
            auto_registered=auto_registered,
            remote=spec.remote,
        )
        for event in events
    )
