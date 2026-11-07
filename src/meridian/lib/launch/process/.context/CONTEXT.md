# Primary process contracts

## Launch and identity

Managed attach observes the TUI's owned IDs live; black-box execution observes
artifacts after exit. An attach failure follows the harness's fallback policy;
the black-box fallback does not run the managed idle sensor. Identity assignment
binds before exec, and the first owned observation may expose a conflict. Later
observations are diagnostic and do not replace the key. Claude fullscreen
correlation is diagnostic, not evidence of a verified exit identity; Pi's
correlated final quit can supply one.

Process start must expose and persist a birth-checked identity before blocking on
terminal relay: managed-primary cancellation needs an identity to terminate the
scope. `cancel_wait()` must release the caller even if termination cannot confirm
exit. Managed attach bridges the synchronous wait through a daemon thread so a
stuck relay cannot hold event-loop shutdown.

## Terminal and signals

The terminal ownership window redirects the launcher's standard descriptors and
gives duplicates to the TUI. Relays retain their own duplicates so a timed-out
thread cannot write through a recycled descriptor. Programs opening `/dev/tty`
directly can still draw on the screen. Terminal checks must precede the window or
use its saved descriptors.

Native TUI and launcher share the shell's foreground process group. The native
SIGINT shield applies during the TUI lifecycle; SIGTSTP remains the shell's job.
The `launch_gate` guardian watches the runner-owned pipe and sends one TERM to the TUI root on runner death, never a
group signal or delayed KILL. Managed backend teardown has its own scope.

TERM and HUP request cancellation and report `128 + signal`; a repeat after the
grace escalates. A terminal close can deliver HUP twice in a burst, so immediate
repeat escalation would prevent finalization. The black-box receiver latches and
the main thread terminates and cancels the wait outside the signal handler. Managed
attach can call native `cancel_wait()` in its handler because it sets a plain
attribute; an Event or gate operation would take a lock. A signal during backend
startup is honored when the TUI starts, or returned as cancellation if startup fails,
so fallback cannot launch another TUI after termination was requested.

Finalization masks termination signals while deciding and writing the outcome;
a deferred signal observed before finalization counts as cancellation. A signal
arriving during the write itself may leave the stored result unchanged. On an
exception during black-box wait, stop the child and release the relay before
reraising, or the harness may outlive finalization with the terminal left raw.

After a terminal disappears, terminal setup and restoration can raise either
`OSError` or `termios.error`; the latter is not an `OSError` subclass. A relay
that loses stdin on `select` must keep draining the PTY master, or a full buffer
can block the child while the parent waits for it.

[Launch context](../../.context/CONTEXT.md) covers the shared composition and
identity contract.
