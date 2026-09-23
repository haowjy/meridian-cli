"""Internal owner-driven attempt boundary; deliberately unwired to production.

An adapter factory must construct the owner around its actual connection before
constructing the coordinator. This is a trusted in-process topology, not a Python
security sandbox. Witnesses attest nothing without the owner's qualification code.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.state import session_store
from meridian.lib.state.session_authority import (
    AttemptBoundaries,
    BeginIntent,
    BoundaryAcceptance,
    BoundaryEvidence,
    BoundaryFact,
    BoundedCorrelation,
    NativeSessionKey,
    Refutation,
    Selection,
)


class EntryWitness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    key: NativeSessionKey
    order: int = Field(ge=0)
    correlation: BoundedCorrelation
    selection: Selection


class ExitWitness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    key: NativeSessionKey
    order: int = Field(ge=0)
    correlation: BoundedCorrelation
    terminal_rule: BoundedCorrelation


class RefutationWitness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    order: int = Field(ge=0)
    reason: Literal["identity_conflict", "same_boundary_conflict", "finality_refuted"]
    conflicting_key: NativeSessionKey | None = None
    causal_reference: BoundedCorrelation


class BoundaryOwner(ABC):
    """Adapter-owned connection and gated input, including injected context.

    Implementations validate primary process-birth and request correlation on
    their own connection, outside state locks. Do not accept child callbacks or
    copied scope labels as correlation. Missing qualification raises ValueError;
    transport/storage failures propagate. No raw task delivery API is public.
    """

    def __init__(self, context: BeginIntent) -> None:
        self._context = context
        self._coordinator: AttemptCoordinator | None = None

    @property
    def context(self) -> BeginIntent:
        return self._context

    @abstractmethod
    async def initialize_without_input(self) -> None: ...

    @abstractmethod
    async def observe_entry(self) -> EntryWitness: ...

    @abstractmethod
    async def close_and_observe_exit(self) -> ExitWitness: ...

    @abstractmethod
    def refutations(self) -> AsyncIterator[RefutationWitness]:
        """Stay alive until callback/connection drain, even after supersession."""
        ...

    async def deliver(self, task: str) -> None:
        if self._coordinator is None:
            raise ValueError("input unresolved: owner has no coordinator")
        # No await between confirmation and dispatch. The adapter must also
        # serialize its selection/switch dispatch with this delivery barrier.
        self._coordinator._confirm_input()
        await self._deliver(task)

    @abstractmethod
    async def _deliver(self, task: str) -> None: ...


class AttemptCoordinator:
    """Pull only from the bound owner; never accept caller-provided receipts.

    Composition runs drain_refutations through owner drain, not merely through
    commit_exit or successor Begin. Retain this coordinator while draining: a
    pending terminal fact/refutation must be retried before exposing its outcome.
    """

    def __init__(self, runtime_root: Path, owner: BoundaryOwner) -> None:
        if owner._coordinator is not None:
            raise ValueError("owner already has its coordinator")
        self._root = runtime_root
        self._owner = owner
        self._context = owner.context
        self._entry: BoundaryFact | None = None
        self._closed = False
        # Observations happen outside the admission lock. Keep one bounded slot
        # per producer so a failed write from one cannot overwrite the other.
        self._pending_terminal: BoundaryFact | None = None
        self._pending_refutation: Refutation | None = None
        self._admission_lock = asyncio.Lock()
        self._exit_observation: asyncio.Task[ExitWitness] | None = None
        self._draining_refutations = False
        owner._coordinator = self

    async def begin(self) -> None:
        session_store._commit_attempt(self._root, self._context)
        await self._owner.initialize_without_input()

    async def commit_entry(self) -> BoundaryAcceptance:
        self._entry = None
        self._retry_pending()
        witness = await self._owner.observe_entry()
        fact = self._fact(witness)
        result = session_store._commit_attempt(self._root, self._context, fact)
        self._entry = fact
        return result

    async def commit_exit(self) -> BoundaryAcceptance:
        self._closed = True
        retried = self._retry_pending()
        if retried is not None:
            return retried
        # Terminal observation is single-flight, but refutation draining remains
        # independent and can make progress while close/observe is suspended.
        if self._exit_observation is None:
            self._exit_observation = asyncio.create_task(
                self._owner.close_and_observe_exit()
            )
        observation = self._exit_observation
        try:
            witness = await observation
        except BaseException:
            if self._exit_observation is observation:
                self._exit_observation = None
            raise
        async with self._admission_lock:
            try:
                fact = self._fact(witness)
                if self._pending_terminal is None:
                    self._pending_terminal = fact
                elif self._pending_terminal != fact:
                    raise RuntimeError("multiple terminal observations for one owner")
                result = self._retry_pending()
            finally:
                if self._exit_observation is observation:
                    self._exit_observation = None
        assert result is not None
        return result

    async def drain_refutations(self) -> None:
        self._retry_pending()
        if self._draining_refutations:
            raise RuntimeError("refutation drain already active for this owner")
        self._draining_refutations = True
        try:
            async for witness in self._owner.refutations():
                refutation = Refutation(
                    run_id=self._context.run_id,
                    attempt_id=self._context.attempt_id,
                    transport_scope_id=self._context.transport_scope_id,
                    **witness.model_dump(),
                )
                async with self._admission_lock:
                    if self._pending_refutation is not None:
                        raise RuntimeError("refutation admission slot is occupied")
                    self._pending_refutation = refutation
                    self._retry_pending()
        finally:
            self._draining_refutations = False

    def boundaries(self) -> AttemptBoundaries:
        self._retry_pending()
        return session_store.get_native_attempt_boundaries(
            self._root, self._context.run_id, self._context.attempt_id
        )

    def _retry_pending(self) -> BoundaryAcceptance | None:
        if self._pending_refutation is None and self._pending_terminal is None:
            return None
        # Refutation is admitted first, so a pending contradiction can never be
        # obscured by a terminal confirmation for the same exit.
        result: BoundaryAcceptance | None = None
        if self._pending_refutation is not None:
            refutation = self._pending_refutation
            result = session_store._commit_attempt(self._root, self._context, refutation)
            self._pending_refutation = None
        if self._pending_terminal is not None:
            terminal = self._pending_terminal
            terminal_result = session_store._commit_attempt(self._root, self._context, terminal)
            self._pending_terminal = None
            if result is None:
                result = terminal_result
        return result

    def _fact(self, witness: EntryWitness | ExitWitness) -> BoundaryFact:
        return BoundaryFact(
            run_id=self._context.run_id,
            attempt_id=self._context.attempt_id,
            boundary="entry" if isinstance(witness, EntryWitness) else "exit",
            key=witness.key,
            evidence=BoundaryEvidence(
                transport_scope_id=self._context.transport_scope_id,
                **witness.model_dump(exclude={"key"}),
            ),
        )

    def _confirm_input(self) -> None:
        if self._entry is None or self._closed:
            raise ValueError("input unresolved: no open durable entry")
        # A missing/replaced/corrupt file or failed sync cannot leave an in-memory
        # gate authorizing input. The transaction rechecks Begin and Entry.
        session_store._commit_attempt(self._root, self._context, self._entry, for_input=True)
