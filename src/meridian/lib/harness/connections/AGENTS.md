# Harness connections

Use `reap_on_ownership_transfer_failure()` during startup and registration: repeated cancellation can otherwise strand a child between owners.

`send_user_message` raises `DeliveryRefused` (alive, nothing written; retried once), `TransportDead` (gone, nothing written; `not_ready(state, text)` in `base.py` picks between them) or any other exception (may have arrived; never resent). See `lib/core/delivery.py`. The hold's nudges take this path too. `backend_dead()` is the hold's one liveness read: gone for good, where `health()` also counts a stalled stream as unhealthy. `ResidentBackendControl` only carries awaiting state.

[Transport contracts](.context/CONTEXT.md)
