"""Pure adapter selection-inspection contracts."""

from dataclasses import dataclass

import pytest

from meridian.lib.harness.bundle import (
    ExecutableSelectionInput,
    HarnessProjectionPorts,
    NativeSelectionInspection,
    NativeSelectionRole,
    NativeSelectionStatus,
)


@dataclass(frozen=True)
class _Spec:
    value: str


def _project(spec: _Spec, *, base_command: tuple[str, ...]) -> list[str]:
    return [*base_command, spec.value]


def test_unregistered_inspectors_are_unsupported_not_absent() -> None:
    ports = HarnessProjectionPorts[_Spec](subprocess_cli_args=_project)

    raw = ports.inspect_raw_selection(("--continue",), NativeSelectionRole.SOURCE)
    executable = ports.inspect_executable_selection(
        ExecutableSelectionInput(spec=_Spec("resume"), argv=None),
        NativeSelectionRole.SOURCE,
    )

    assert raw.status is NativeSelectionStatus.UNSUPPORTED
    assert executable.status is NativeSelectionStatus.UNSUPPORTED
    assert raw.status is not NativeSelectionStatus.ABSENT
    assert executable.status is not NativeSelectionStatus.ABSENT


def test_inspector_can_distinguish_absent_from_unknown_and_selected() -> None:
    def inspect_raw(
        args: tuple[str, ...], role: NativeSelectionRole
    ) -> NativeSelectionInspection:
        del role
        if "--uncertain" in args:
            return NativeSelectionInspection(NativeSelectionStatus.UNKNOWN)
        if "--session" in args:
            return NativeSelectionInspection(NativeSelectionStatus.SELECTED, "native-7")
        return NativeSelectionInspection(NativeSelectionStatus.ABSENT)

    ports = HarnessProjectionPorts[_Spec](
        subprocess_cli_args=_project,
        raw_selection=inspect_raw,
    )

    assert ports.inspect_raw_selection((), NativeSelectionRole.SOURCE).status is (
        NativeSelectionStatus.ABSENT
    )
    assert ports.inspect_raw_selection(("--uncertain",), NativeSelectionRole.SOURCE).status is (
        NativeSelectionStatus.UNKNOWN
    )
    selected = ports.inspect_raw_selection(("--session",), NativeSelectionRole.PENDING_FORK)
    assert selected == NativeSelectionInspection(NativeSelectionStatus.SELECTED, "native-7")


def test_selection_result_rejects_inconsistent_reference() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        NativeSelectionInspection(NativeSelectionStatus.SELECTED)
    with pytest.raises(ValueError, match="only selected"):
        NativeSelectionInspection(NativeSelectionStatus.ABSENT, "native-7")


def test_spec_only_executable_input_keeps_argv_absence_explicit() -> None:
    executable = ExecutableSelectionInput(spec=_Spec("fresh"), argv=None)

    assert executable.argv is None
