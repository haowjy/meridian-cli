from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from meridian.lib.core.types import HarnessId
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy


@dataclass(frozen=True)
class FakeBundleResult:
    model: str
    model_token: str
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    warnings: tuple[str, ...] = ()
    tools_allowed: tuple[str, ...] = ()
    tools_disallowed: tuple[str, ...] = ()
    tools_mcp: tuple[str, ...] = ()


def stub_bundle_request_and_resolve(
    monkeypatch: Any,
    *,
    model: str,
    harness: HarnessId,
    model_token: str | None = None,
    harness_model: str | None = None,
    execution_policy: ResolvedExecutionPolicy | None = None,
    provenance: dict[str, str] | None = None,
    warnings: tuple[str, ...] = (),
    tools_allowed: tuple[str, ...] = (),
    tools_disallowed: tuple[str, ...] = (),
    tools_mcp: tuple[str, ...] = (),
) -> list[bundle_adapter.BundleRequest]:
    captured_requests: list[bundle_adapter.BundleRequest] = []

    def _fake_request_and_resolve(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> FakeBundleResult:
        _ = harness_registry
        captured_requests.append(request)
        resolved_policy = execution_policy or ResolvedExecutionPolicy()
        resolved_provenance = provenance or {"model_source": "cli", "harness_source": "provider"}
        resolved_model_token = (
            model_token if model_token is not None else request.model_override or model
        )
        resolved_harness_model = harness_model if harness_model is not None else model
        return FakeBundleResult(
            model=model,
            model_token=resolved_model_token,
            harness=harness,
            harness_model=resolved_harness_model,
            execution_policy=resolved_policy,
            provenance=resolved_provenance,
            warnings=warnings,
            tools_allowed=tools_allowed,
            tools_disallowed=tools_disallowed,
            tools_mcp=tools_mcp,
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _fake_request_and_resolve)
    return captured_requests
