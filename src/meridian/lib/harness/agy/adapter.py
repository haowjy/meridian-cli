"""The ``agy`` launch adapter, registered with launch ports only.

Meridian never runs ``agy``: its plugin hosts the session (``harness/plugin_hosted.py``), so
there is no extractor, connection or runner. The adapter builds the command Meridian would
print and, as an ``AgyReader``, reads the sessions ``agy`` stores.
"""

from __future__ import annotations

from typing import ClassVar

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import (
    BaseHarnessAdapter,
    BootstrapMode,
    HarnessCapabilities,
    HarnessContract,
    SpawnParams,
)
from meridian.lib.harness.agy.transcript import AgyReader
from meridian.lib.harness.bundle import (
    HarnessProjectionPorts,
    LaunchPorts,
    register_harness,
)
from meridian.lib.harness.semantics import HarnessSemantics
from meridian.lib.launch.launch_types import ResolvedLaunchSpec


class AgyAdapter(AgyReader, BaseHarnessAdapter[ResolvedLaunchSpec]):
    BASE_COMMAND: ClassVar[tuple[str, ...]] = ("agy",)
    _CONSUMED_FIELDS: ClassVar[frozenset[str]] = frozenset({"model", "extra_args"})

    @property
    def id(self) -> HarnessId:
        return HarnessId.AGY

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities()

    @property
    def contract(self) -> HarnessContract:
        return HarnessContract(
            capabilities=self.capabilities,
            transport_ids=(),
            launch_spec_cls="ResolvedLaunchSpec",
            bootstrap_mode=BootstrapMode.SUBPROCESS_ONLY,
        )

    @property
    def consumed_fields(self) -> frozenset[str]:
        return self._CONSUMED_FIELDS

    @property
    def explicitly_ignored_fields(self) -> frozenset[str]:
        return frozenset(SpawnParams.model_fields) - self._CONSUMED_FIELDS

    def resolve_launch_spec(self, run: SpawnParams) -> ResolvedLaunchSpec:
        return ResolvedLaunchSpec(
            harness=HarnessId.AGY,
            model=str(run.model).strip() if run.model else None,
            extra_args=run.extra_args,
        )


def project_agy_spec_to_cli_args(
    spec: ResolvedLaunchSpec, *, base_command: tuple[str, ...]
) -> list[str]:
    command = list(base_command)
    if spec.model:
        command += ["--model", spec.model]
    command.extend(spec.extra_args)
    return command


register_harness(
    LaunchPorts(
        harness_id=HarnessId.AGY,
        adapter=AgyAdapter(),
        spec_cls=ResolvedLaunchSpec,
        projections=HarnessProjectionPorts(
            subprocess_cli_args=project_agy_spec_to_cli_args,
            permission_report=lambda spec: (),
        ),
        # No events reach Meridian from ``agy``: the plugin writes the row.
        semantics=HarnessSemantics(events={}),
    )
)
