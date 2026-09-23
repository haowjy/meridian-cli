"""Scripted in-memory owner: state topology evidence, never Pi qualification.

Boundary scripts originate on an explicit fake connection/primary owner. The
synchronous helpers below retain those owner instances between test actions; they
never call private persistence. Disk-only replay fixtures remain ordinary facts.
"""

import asyncio
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.state import session_authority as authority
from meridian.lib.state.attempt_coordinator import (
    AttemptCoordinator,
    BoundaryOwner,
    EntryWitness,
    ExitWitness,
    RefutationWitness,
)


@dataclass(frozen=True)
class Observation:
    owner: object
    connection: object
    witness: EntryWitness | ExitWitness | RefutationWitness
    primary: bool = True


class ScriptedOwner(BoundaryOwner):
    def __init__(self, context: authority.BeginIntent):
        super().__init__(context)
        self.connection = object()
        self.script: deque[Observation] = deque()
        self.refutation_queue: asyncio.Queue[Observation | None] = asyncio.Queue()
        self.delivered: list[str] = []
        self.events: list[str] = []
        self.delay_exit = False
        self.exit_waiting = asyncio.Event()
        self.release_exit = asyncio.Event()
        self.refutation_waiting = asyncio.Event()

    async def initialize_without_input(self):
        self.events.append("initialize")

    def observation(self, witness, **updates):
        return Observation(self, self.connection, witness, **updates)

    def _qualify(self, observation):
        if (
            observation.owner is not self
            or observation.connection is not self.connection
            or not observation.primary
        ):
            raise ValueError("observation not owned by this primary connection")
        return observation.witness

    async def observe_entry(self):
        self.events.append("observe_entry")
        witness = self._qualify(self.script.popleft())
        if not isinstance(witness, EntryWitness):
            raise ValueError("entry unresolved: no qualified entry")
        return witness

    async def close_and_observe_exit(self):
        self.events.append("close_and_observe_exit")
        if self.delay_exit:
            self.exit_waiting.set()
            await self.release_exit.wait()
        witness = self._qualify(self.script.popleft())
        if not isinstance(witness, ExitWitness):
            raise ValueError("exit unresolved: missing terminal qualification")
        return witness

    async def refutations(self):
        self.refutation_waiting.set()
        while (observation := await self.refutation_queue.get()) is not None:
            witness = self._qualify(observation)
            if not isinstance(witness, RefutationWitness):
                raise ValueError("not a refutation")
            yield witness

    async def _deliver(self, task):
        self.events.append("deliver")
        self.delivered.append(task)


def key(name: str, native_id: str = "native-1") -> authority.NativeSessionKey:
    return authority.NativeSessionKey(
        harness="pi", store="/native/store", native_session_id=f"{name}:{native_id}"
    )


def receipt(
    run_id,
    attempt_id,
    boundary,
    native_key,
    *,
    order=1,
    operation="fresh",
    source_key=None,
    scope_id=None,
):
    """A disk fact fixture, converted to an owner script by observe() below."""
    selection = None
    if boundary == "entry":
        if operation == "fresh":
            selection = authority.CreatedSelection(creation_request="fake-create")
        elif operation == "resume":
            selection = authority.ResumeSelection(source=source_key)
        else:
            selection = authority.ForkSelection(source=source_key, ancestry_request="fake-fork")
    return authority.BoundaryFact(
        run_id=run_id,
        attempt_id=attempt_id,
        boundary=boundary,
        key=native_key,
        evidence=authority.BoundaryEvidence(
            transport_scope_id=scope_id or f"transport:{attempt_id}",
            order=order,
            correlation="fake-request",
            selection=selection,
            terminal_rule="scripted-only:v1" if boundary == "exit" else None,
        ),
    )


# The registry belongs only to synchronous filesystem test scaffolding. Production
# has no owner lookup by scope; it receives an adapter-created instance directly.
_attempts: dict[tuple[Path, str, str], tuple[ScriptedOwner, AttemptCoordinator]] = {}


def prepare(root, run_id, attempt_id, **kwargs):
    kwargs.setdefault("transport_scope_id", f"transport:{attempt_id}")
    kwargs.setdefault("operation", "fresh")
    kwargs.setdefault("harness", "pi")
    kwargs.setdefault("store", "/native/store")
    context = authority.BeginIntent(run_id=run_id, attempt_id=attempt_id, **kwargs)
    owner = ScriptedOwner(context)
    coordinator = AttemptCoordinator(root, owner)
    _attempts[root, run_id, attempt_id] = owner, coordinator
    return owner, coordinator


def begin(root, run_id, attempt_id, **kwargs):
    identity = root, run_id, attempt_id
    if identity not in _attempts:
        prepare(root, run_id, attempt_id, **kwargs)
    owner, coordinator = _attempts[identity]
    if kwargs and any(getattr(owner.context, k) != v for k, v in kwargs.items()):
        raise ValueError("attempt begin context is immutable")
    asyncio.run(coordinator.begin())


def observe(root, fact):
    identity = root, fact.run_id, fact.attempt_id
    if identity not in _attempts:
        raise ValueError("unknown or unstarted fake owner")
    owner, coordinator = _attempts[identity]
    values = fact.evidence.model_dump(exclude={"transport_scope_id", "selection", "terminal_rule"})
    witness = (
        EntryWitness(key=fact.key, **values, selection=fact.evidence.selection)
        if fact.boundary == "entry"
        else ExitWitness(key=fact.key, **values, terminal_rule=fact.evidence.terminal_rule)
    )
    primary = fact.evidence.transport_scope_id == owner.context.transport_scope_id
    owner.script.append(owner.observation(witness, primary=primary))
    return asyncio.run(
        coordinator.commit_entry() if fact.boundary == "entry" else coordinator.commit_exit()
    )


def refute(root, fact):
    owner, coordinator = _attempts[root, fact.run_id, fact.attempt_id]
    witness = RefutationWitness(
        **fact.model_dump(
            exclude={"v", "event", "action", "run_id", "attempt_id", "transport_scope_id"}
        )
    )
    primary = fact.transport_scope_id == owner.context.transport_scope_id
    owner.refutation_queue.put_nowait(owner.observation(witness, primary=primary))
    owner.refutation_queue.put_nowait(None)
    asyncio.run(coordinator.drain_refutations())
    return authority.BoundaryAcceptance(None, coordinator.boundaries().exit_invalidated)
