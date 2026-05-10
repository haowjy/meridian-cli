# lib/streaming/

Async runtime layer between harness connections and the rest of the system. Owns
the drain loop, control socket, heartbeat, and event fan-out for every live spawn.
Pure mechanism — no spawn policy lives here.

## Entry Points

- `spawn_manager.py` — `SpawnManager`: the central async registry. Start here.
- `drain_policy.py` — `DrainPolicy`, `SingleTurnDrainPolicy`, `PersistentDrainPolicy`
- `control_socket.py` — `ControlSocketServer`: per-spawn inject endpoint
- `heartbeat.py` — `heartbeat_loop`: sentinel file touch loop
- `event_observers.py` — `EventObserverRegistry`, `EventObserver`, `CallbackObserver`
- `inject_lock.py` — per-spawn asyncio lock registry for inject serialization
- `types.py` — `InjectResult`, `ControlMessage`

## Depth

See [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Drain loop ordering contract (persist → observe → fan-out)
- DrainOutcome priority rules
- Teardown paths and invariants
- Control socket inject flow
- Heartbeat/reaper contract

## Related

- `../state/history.py` — `HarnessHistoryWriter` (persistence target for drain loop)
- `../harness/connections/` — `HarnessConnection` protocol (event source)
- `../state/reaper.py` — uses heartbeat sentinel to detect orphaned spawns
