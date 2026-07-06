import asyncio
import importlib
import signal
from pathlib import Path
from typing import cast

import pytest

from meridian.lib.core.types import HarnessId, SpawnId, TransportId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.signals import (
    SignalCoordinator,
    SignalForwarder,
    force_kill_process,
    map_process_exit_code,
    signal_process_group,
    signal_to_exit_code,
)
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.streaming import spawn_manager as spawn_manager_module


class _FakeSignalProcess:
    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.sent: list[signal.Signals] = []
        self.killed = False

    def send_signal(self, signum: signal.Signals) -> None:
        self.sent.append(signum)

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_streaming_runner_signal_cancel_invokes_send_cancel_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_root = resolve_runtime_paths(tmp_path).root_dir
    run_streaming_spawn = importlib.import_module(
        "meridian.lib.launch.streaming_runner"
    ).run_streaming_spawn

    class _FakeControlSocketServer:
        def __init__(self, spawn_id: str, socket_path: Path, manager: object) -> None:
            _ = spawn_id, manager
            self.socket_path = socket_path

        async def start(self) -> None:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        async def stop(self) -> None:
            return None

    class _SignalDrivenConnection:
        send_cancel_calls = 0

        def __init__(self) -> None:
            self.state = "created"
            self._spawn_id = SpawnId("")
            self.capabilities = ConnectionCapabilities(
                mid_turn_injection="interrupt_restart",
                supports_steer=True,
                supports_cancel=True,
                runtime_model_switch=False,
                structured_reasoning=True,
            )

        @property
        def harness_id(self) -> HarnessId:
            return HarnessId.CODEX

        @property
        def spawn_id(self) -> SpawnId:
            return self._spawn_id

        @property
        def session_id(self) -> str | None:
            return None

        @property
        def subprocess_pid(self) -> int | None:
            return None

        @property
        def primary_event_scope(self) -> None:
            return None

        @property
        def resident_backend(self) -> None:
            return None

        async def start(self, config: ConnectionConfig, spec: object) -> None:
            _ = spec
            self._spawn_id = config.spawn_id
            self.state = "connected"

        async def stop(self) -> None:
            self.state = "stopped"

        def health(self) -> bool:
            return self.state == "connected"

        async def send_user_message(self, text: str) -> None:
            _ = text

        async def send_cancel(self) -> None:
            type(self).send_cancel_calls += 1
            self.state = "stopping"

        async def events(self):  # type: ignore[no-untyped-def]
            while True:
                await asyncio.sleep(3600)
                if False:
                    yield HarnessEvent(
                        event_type="noop",
                        payload={},
                        harness_id="codex",
                    )

    def _fake_install_signal_handlers(
        loop: asyncio.AbstractEventLoop,
        shutdown_event: asyncio.Event,
        received_signal: list[signal.Signals | None],
    ) -> None:
        _ = loop
        received_signal[0] = signal.SIGTERM
        shutdown_event.set()
        return None

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: _SignalDrivenConnection,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.streaming_runner._install_signal_handlers",
        _fake_install_signal_handlers,
    )

    outcome = await run_streaming_spawn(
        config=ConnectionConfig(
            spawn_id=SpawnId("p-signal"),
            harness_id=HarnessId.CODEX,
            prompt="hello",
            control_root=tmp_path,
            env_overrides={},
        ),
        spec=ResolvedLaunchSpec(
            prompt="hello",
            permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
        ),
        runtime_root=runtime_root,
        project_root=tmp_path,
        spawn_id=SpawnId("p-signal"),
    )

    assert outcome.status == "cancelled"
    assert _SignalDrivenConnection.send_cancel_calls == 1


def test_signal_forwarder_forwards_sigint_and_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    import meridian.lib.launch.signals as signals_module

    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 12345
            self.returncode: int | None = None

    sent_signals: list[signal.Signals] = []
    force_kill_called: list[bool] = []

    def fake_signal_process_group(
        process: asyncio.subprocess.Process,
        signum: signal.Signals,
    ) -> None:
        sent_signals.append(signum)

    def fake_force_kill_process(process: asyncio.subprocess.Process) -> None:
        force_kill_called.append(True)
        process.returncode = -9

    monkeypatch.setattr(signals_module, "signal_process_group", fake_signal_process_group)
    monkeypatch.setattr(signals_module, "force_kill_process", fake_force_kill_process)

    fake = FakeProcess()
    forwarder = SignalForwarder(cast("asyncio.subprocess.Process", fake))
    forwarder.forward_signal(signal.SIGINT)
    forwarder.forward_signal(signal.SIGTERM)

    # Two signals sent via signal_process_group, then force_kill_process called on second signal
    assert sent_signals == [signal.SIGINT, signal.SIGTERM]
    assert force_kill_called == [True]
    assert forwarder.received_signal == signal.SIGTERM
    assert signal_to_exit_code(signal.SIGINT) == 130
    assert signal_to_exit_code(signal.SIGTERM) == 143
    assert map_process_exit_code(raw_return_code=0, received_signal=signal.SIGTERM) == 143


def test_signal_process_group_degrades_to_process_signal_for_shared_pgid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.launch.signals as signals_module

    killpg_calls: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(signals_module, "IS_WINDOWS", False)
    monkeypatch.setattr(signals_module.os, "getpgid", lambda _pid: 999, raising=False)
    monkeypatch.setattr(
        signals_module.os,
        "killpg",
        lambda pgid, signum: killpg_calls.append((pgid, signum)),
        raising=False,
    )

    process = _FakeSignalProcess()
    signal_process_group(cast("asyncio.subprocess.Process", process), signal.SIGTERM)

    assert process.sent == [signal.SIGTERM]
    assert killpg_calls == []


def test_signal_process_group_uses_killpg_for_group_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.launch.signals as signals_module

    killpg_calls: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(signals_module, "IS_WINDOWS", False)
    monkeypatch.setattr(signals_module.os, "getpgid", lambda _pid: 12345, raising=False)
    monkeypatch.setattr(
        signals_module.os,
        "killpg",
        lambda pgid, signum: killpg_calls.append((pgid, signum)),
        raising=False,
    )

    process = _FakeSignalProcess()
    signal_process_group(cast("asyncio.subprocess.Process", process), signal.SIGTERM)

    assert process.sent == []
    assert killpg_calls == [(12345, signal.SIGTERM)]


def test_signal_process_group_on_windows_signals_process_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.launch.signals as signals_module

    monkeypatch.setattr(signals_module, "IS_WINDOWS", True)
    monkeypatch.setattr(
        signals_module.os,
        "getpgid",
        lambda _pid: pytest.fail("os.getpgid should not be called on Windows"),
        raising=False,
    )
    monkeypatch.setattr(
        signals_module.os,
        "killpg",
        lambda _pgid, _signum: pytest.fail("os.killpg should not be called on Windows"),
        raising=False,
    )

    process = _FakeSignalProcess()
    signal_process_group(cast("asyncio.subprocess.Process", process), signal.SIGTERM)

    assert process.sent == [signal.SIGTERM]


def test_force_kill_process_degrades_to_process_kill_for_shared_pgid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.launch.signals as signals_module

    killpg_calls: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(signals_module, "IS_WINDOWS", False)
    monkeypatch.setattr(signals_module.os, "getpgid", lambda _pid: 999, raising=False)
    monkeypatch.setattr(
        signals_module.os,
        "killpg",
        lambda pgid, signum: killpg_calls.append((pgid, signum)),
        raising=False,
    )

    process = _FakeSignalProcess()
    force_kill_process(cast("asyncio.subprocess.Process", process))

    assert process.killed is True
    assert killpg_calls == []


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX SIGKILL group-leader semantics")
def test_force_kill_process_uses_killpg_for_group_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.launch.signals as signals_module

    killpg_calls: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(signals_module, "IS_WINDOWS", False)
    monkeypatch.setattr(signals_module.os, "getpgid", lambda _pid: 12345, raising=False)
    monkeypatch.setattr(
        signals_module.os,
        "killpg",
        lambda pgid, signum: killpg_calls.append((pgid, signum)),
        raising=False,
    )

    process = _FakeSignalProcess()
    force_kill_process(cast("asyncio.subprocess.Process", process))

    assert process.killed is False
    assert killpg_calls == [(12345, signal.SIGKILL)]


def test_force_kill_process_on_windows_kills_process_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.launch.signals as signals_module

    monkeypatch.setattr(signals_module, "IS_WINDOWS", True)
    monkeypatch.setattr(
        signals_module.os,
        "getpgid",
        lambda _pid: pytest.fail("os.getpgid should not be called on Windows"),
        raising=False,
    )
    monkeypatch.setattr(
        signals_module.os,
        "killpg",
        lambda _pgid, _signum: pytest.fail("os.killpg should not be called on Windows"),
        raising=False,
    )

    process = _FakeSignalProcess()
    force_kill_process(cast("asyncio.subprocess.Process", process))

    assert process.killed is True


def test_signal_coordinator_dispatches_signal_to_all_active_forwarders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.launch.signals as signals_module

    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 12345
            self.returncode: int | None = None

    installed_handlers: dict[signal.Signals, object] = {}

    def fake_getsignal(_signum: signal.Signals) -> object:
        return signal.SIG_DFL

    def fake_signal(raw_signum: int, handler: object) -> object:
        signum = signal.Signals(raw_signum)
        previous = installed_handlers.get(signum, signal.SIG_DFL)
        installed_handlers[signum] = handler
        return previous

    sent_signals: list[signal.Signals] = []

    def fake_signal_process_group(
        process: asyncio.subprocess.Process,
        signum: signal.Signals,
    ) -> None:
        sent_signals.append(signum)

    def fake_force_kill_process(process: asyncio.subprocess.Process) -> None:
        process.returncode = -9

    monkeypatch.setattr(signals_module.signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signals_module.signal, "signal", fake_signal)
    monkeypatch.setattr(signals_module, "signal_process_group", fake_signal_process_group)
    monkeypatch.setattr(signals_module, "force_kill_process", fake_force_kill_process)

    coordinator = SignalCoordinator()
    monkeypatch.setattr(signals_module, "signal_coordinator", lambda: coordinator)

    first = SignalForwarder(cast("asyncio.subprocess.Process", FakeProcess()))
    second = SignalForwarder(cast("asyncio.subprocess.Process", FakeProcess()))

    with first, second:
        handler = installed_handlers.get(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM.value, None)

    assert sent_signals == [signal.SIGTERM, signal.SIGTERM]
