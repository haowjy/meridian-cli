"""Typed schema and validation for configured lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import TypeIs

from meridian.lib.hooks.builtin_registry import (
    BUILTIN_HOOK_REGISTRY,
    get_default_events,
    validate_builtin_options,
)
from meridian.lib.hooks.types import (
    EVENT_CLASS,
    FailurePolicy,
    Hook,
    HookEventName,
    HookWhen,
    SpawnStatus,
)
from meridian.lib.utils.json_shapes import is_json_object, json_list

logger = logging.getLogger(__name__)

_INTERVAL_PATTERN = re.compile(r"^\d+[smhd]$")
_HOOK_NAME_COMPONENT_PATTERN = re.compile(r"[^a-z0-9._-]+")
_HOOK_NAME_REPEATED_DASH_PATTERN = re.compile(r"-+")
_KNOWN_FAILURE_POLICIES = frozenset({"fail", "warn", "ignore"})
_KNOWN_SPAWN_STATUSES = frozenset({"success", "failure", "cancelled", "timeout", "skipped"})
_ALLOWED_HOOK_KEYS = frozenset(
    {
        "name",
        "builtin",
        "command",
        "event",
        "events",
        "timeout_secs",
        "interval",
        "enabled",
        "priority",
        "failure_policy",
        "require_serial",
        "when",
        "exclude",
        "repo",
        "remote",
        "options",
    }
)
_STRING_FIELDS = frozenset(
    {
        "name",
        "event",
        "command",
        "builtin",
        "interval",
        "failure_policy",
        "repo",
        "remote",
    }
)


class HookWhenSpec(BaseModel):
    """Validated conditional fields from one hook row."""

    model_config = ConfigDict(frozen=True, strict=True)

    status: tuple[str, ...] | None = None
    agent: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _tuple_status(cls, value: object) -> object:
        values = json_list(value)
        return tuple(values) if values is not None else value


class HookSpec(BaseModel):
    """Validated file representation of one configured hook."""

    model_config = ConfigDict(frozen=True, strict=True)

    name: str | None = None
    builtin: str | None = None
    command: str | None = None
    event: str | None = None
    timeout_secs: int | None = None
    interval: str | None = None
    enabled: bool = True
    priority: int = 0
    failure_policy: str | None = None
    require_serial: bool = False
    when: HookWhenSpec | None = None
    exclude: tuple[str, ...] = ()
    remote: str | None = None
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


def _normalize_required_string(raw: str, *, source: str) -> str:
    normalized = raw.strip()
    if not normalized:
        raise ValueError(f"Invalid value for '{source}': expected non-empty string.")
    return normalized


def _parse_toml_list(*, raw_value: object, source: str) -> tuple[str, ...]:
    values = json_list(raw_value)
    if values is None:
        raise ValueError(
            f"Invalid value for '{source}': expected array[str], "
            f"got {type(raw_value).__name__} ({raw_value!r})."
        )

    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(
                f"Invalid value for '{source}': expected array[str], "
                f"got {type(item).__name__} ({item!r})."
            )
        compact = item.strip()
        if not compact:
            raise ValueError(f"Invalid value for '{source}': expected non-empty entries.")
        normalized.append(compact)
    return tuple(normalized)


def _normalize_when(raw_value: object, *, source: str) -> HookWhenSpec:
    if not is_json_object(raw_value):
        raise ValueError(
            f"Invalid value for '{source}': expected table, "
            f"got {type(raw_value).__name__} ({raw_value!r})."
        )

    status: tuple[str, ...] | None = None
    agent: str | None = None
    fields_set: set[str] = set()
    for key, value in raw_value.items():
        field_source = f"{source}.{key}"
        if key == "status":
            status = _parse_toml_list(raw_value=value, source=field_source)
            fields_set.add(key)
            continue
        if key == "agent":
            if not isinstance(value, str):
                raise ValueError(
                    f"Invalid value for '{field_source}': expected str, got "
                    f"{type(value).__name__} ({value!r})."
                )
            agent = _normalize_required_string(value, source=field_source)
            fields_set.add(key)
            continue
        logger.warning("Ignoring unknown Meridian config key '%s.%s'.", source, key)

    return HookWhenSpec.model_construct(
        _fields_set=fields_set,
        status=status,
        agent=agent,
    )


def normalize_hooks_array(raw_value: object, *, source: str) -> tuple[HookSpec, ...]:
    """Validate and normalize one TOML hooks array into typed specifications."""

    values = json_list(raw_value)
    if values is None:
        raise ValueError(
            f"Invalid value for '{source}': expected array[table], "
            f"got {type(raw_value).__name__} ({raw_value!r})."
        )

    specs: list[HookSpec] = []
    for index, item in enumerate(values, start=1):
        row_source = f"{source}[{index}]"
        if not is_json_object(item):
            raise ValueError(
                f"Invalid value for '{row_source}': expected table, "
                f"got {type(item).__name__} ({item!r})."
            )

        strings: dict[str, str] = {}
        enabled = True
        priority = 0
        timeout_secs: int | None = None
        require_serial = False
        exclude: tuple[str, ...] = ()
        options: dict[str, object] = {}
        when: HookWhenSpec | None = None
        fields_set: set[str] = set()

        for key, value in item.items():
            if key not in _ALLOWED_HOOK_KEYS:
                logger.warning("Ignoring unknown Meridian config key '%s.%s'.", row_source, key)
                continue

            field_source = f"{row_source}.{key}"
            if key in _STRING_FIELDS:
                if not isinstance(value, str):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected str, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                strings[key] = _normalize_required_string(value, source=field_source)
                fields_set.add(key)
                continue

            if key == "enabled":
                if not isinstance(value, bool):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected bool, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                enabled = value
                fields_set.add(key)
                continue

            if key == "require_serial":
                if not isinstance(value, bool):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected bool, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                require_serial = value
                fields_set.add(key)
                continue

            if key in {"priority", "timeout_secs"}:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected int, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                if key == "priority":
                    priority = value
                else:
                    timeout_secs = value
                fields_set.add(key)
                continue

            if key == "exclude":
                exclude = _parse_toml_list(raw_value=value, source=field_source)
                fields_set.add(key)
                continue

            if key == "options":
                if not is_json_object(value):
                    raise ValueError(
                        f"Invalid value for '{field_source}': expected table, got "
                        f"{type(value).__name__} ({value!r})."
                    )
                options = dict(value)
                fields_set.add(key)
                continue

            if key == "when":
                when = _normalize_when(value, source=field_source)
                fields_set.add(key)
                continue

            logger.warning("Ignoring unknown Meridian config key '%s.%s'.", row_source, key)

        specs.append(
            HookSpec.model_construct(
                _fields_set=fields_set,
                name=strings.get("name"),
                builtin=strings.get("builtin"),
                command=strings.get("command"),
                event=strings.get("event"),
                timeout_secs=timeout_secs,
                interval=strings.get("interval"),
                enabled=enabled,
                priority=priority,
                failure_policy=strings.get("failure_policy"),
                require_serial=require_serial,
                when=when,
                exclude=exclude,
                remote=strings.get("remote"),
                options=options,
            )
        )
    return tuple(specs)


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


def _is_hook_event(value: str) -> TypeIs[HookEventName]:
    return value in EVENT_CLASS


def _parse_event(raw: str, *, source: str) -> HookEventName:
    if not _is_hook_event(raw):
        valid = ", ".join(sorted(EVENT_CLASS.keys()))
        raise ValueError(f"Invalid value for '{source}': expected one of [{valid}], got {raw!r}.")
    return raw


def _parse_interval(raw: str | None, *, source: str) -> str | None:
    if raw is None:
        return None
    if not _INTERVAL_PATTERN.fullmatch(raw):
        raise ValueError(
            f"Invalid value for '{source}': expected interval format '\\d+[smhd]', got {raw!r}."
        )
    return raw


def _is_spawn_status(value: str) -> TypeIs[SpawnStatus]:
    return value in _KNOWN_SPAWN_STATUSES


def _parse_when(spec: HookWhenSpec | None, *, source: str) -> HookWhen | None:
    if spec is None:
        return None

    statuses: list[SpawnStatus] | None = None
    if spec.status is not None:
        statuses = []
        for value in spec.status:
            if not _is_spawn_status(value):
                valid = ", ".join(sorted(_KNOWN_SPAWN_STATUSES))
                raise ValueError(
                    f"Invalid value for '{source}.status': expected values in [{valid}], "
                    f"got {value!r}."
                )
            statuses.append(value)

    if statuses is None and spec.agent is None:
        return None
    return HookWhen(
        status=tuple(statuses) if statuses is not None else None,
        agent=spec.agent,
    )


def _is_failure_policy(value: str) -> TypeIs[FailurePolicy]:
    return value in _KNOWN_FAILURE_POLICIES


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
        events = (_parse_event(spec.event, source=f"{row_source}.event"),)
    elif default_events:
        events = tuple(
            _parse_event(event, source=f"{row_source}.event") for event in default_events
        )
    else:
        raise ValueError(f"Invalid hook config '{row_source}': 'event' is required.")

    default_interval = BUILTIN_HOOK_REGISTRY[builtin].interval if builtin is not None else None
    interval = _parse_interval(
        spec.interval or default_interval,
        source=f"{row_source}.interval",
    )
    if spec.timeout_secs is not None and spec.timeout_secs <= 0:
        raise ValueError(
            f"Invalid value for '{row_source}.timeout_secs': expected int > 0, "
            f"got {spec.timeout_secs!r}."
        )

    failure_policy: FailurePolicy | None = None
    if spec.failure_policy is not None:
        if not _is_failure_policy(spec.failure_policy):
            valid = ", ".join(sorted(_KNOWN_FAILURE_POLICIES))
            raise ValueError(
                f"Invalid value for '{row_source}.failure_policy': expected one of [{valid}], "
                f"got {spec.failure_policy!r}."
            )
        failure_policy = spec.failure_policy

    when = _parse_when(spec.when, source=f"{row_source}.when")
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
            failure_policy=failure_policy,
            require_serial=spec.require_serial,
            when=when,
            exclude=spec.exclude,
            options=options,
            auto_registered=auto_registered,
            remote=spec.remote,
        )
        for event in events
    )
