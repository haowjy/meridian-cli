# harness/connections/ — Context

**Pi injected prompts are acknowledged commands.** Every prompt carries an RPC `id`
and `streamingBehavior: "followUp"`; the field is valid whether Pi is busy or idle.
Injected prompts wait for the matching `response` before `send_user_message()` returns.
A rejected inject is annotated as control-action response evidence, persisted, and
reported to the injector without becoming a terminal event for the running spawn.
Initial-prompt rejection remains spawn-fatal.

`PiRpcConnection` owns one receive task from successful startup through stop.
Reception resolves prompt ACKs and observes protocol readiness independently of
`events()` consumption; an awaited completion nudge cannot block its own ACK.
The initial prompt has a 30-second first-protocol-event deadline. Follow-up
writes and ACKs have a 30-second deadline; failures after writing are delivery
uncertainty and must not replay the prompt. Stdout EOF gives the leader one
second to exit before failing and cleaning its process scope.

The handler is injected; `HarnessConnection` implementations must not embed policy.

### OpenCode Runtime Requests and Event Multiplex

OpenCode has no background reader: `events()` parses the SSE stream in the drain
loop's own task. When it sees `permission.asked` / `permission.v2.asked`, it builds
a `HarnessRequest`, registers the pending request and its liveness key synchronously,
then dispatches the injected handler in a background task (returning without yielding
the raw ask). The reply POST can stall, so the handler must not run inline on the
drain path; the background dispatch is bounded by
`_REQUEST_DISPATCH_TIMEOUT_SECONDS` and reports a dispatch failure through the handler
on timeout or error. `respond_request` replies over the version's HTTP endpoint
(`POST /session/{id}/permissions/{permissionID}` with `{"response": …}` for V1;
`POST /api/session/{id}/permission/{requestID}/reply` with `{"decision": …}` for V2) and
fails fast when the request id is unknown instead of falling back to the session id.

Connections add no launch-local model, prompt or permission override: all ride the bound
`OPENCODE_CONFIG_CONTENT` (see [OpenCode prompt, agent and model
delivery](../../.context/CONTEXT.md#opencode-prompt-agent-and-model-delivery)), and
Meridian's default config agent carries no model, so a native agent's model cannot
shadow the selection and there is no backend replacement. The V1 transport still
validates available providers and the effective config `model` before creating a
session; V2 runs no inspection. One startup deadline covers launch, readiness,
inspection and session creation; rejected configuration fails without black-box
fallback.
The existing lifecycle gate spans initial process publication through connected
state. Failure cleanup retains this gate even if its foreground wait expires;
stop/new start cannot cross unfinished publication or mutate old cleanup's fields.
Concurrent stop can wait for startup to settle; it never acknowledges a child that
has not yet been published.

Session creation nests `model={id,providerID}`; prompts use
`model={modelID,providerID}` with `variant` at the prompt top level. Later
HTTP injection observes committed native session agent/model/variant, including
an explicit default variant; absent session fields fall back to the last native
user message, never replayed launch settings. Unsent TUI-local changes are not
observable, and GET/POST is not atomic against simultaneous TUI submission.
Continue preserves native choice. Dry-run projects serve/inspection/bootstrap/
attach without native observations or a misleading black-box command.

**Ownership-transfer guard: `reap_on_ownership_transfer_failure()`.** When external
cancellation hits during adapter startup, dispatch, or manager registration, a
child process can be stranded between owners. `reap_on_ownership_transfer_failure()`
in `base.py` catches `BaseException`, shields cleanup from repeated cancellation
deliveries in a while-not-done loop, and bounds foreground cleanup to 30 seconds.
Durable `spawn_owned` process scopes and the reaper own any residue beyond that
bound. The rejected alternative (`with suppress` single-shot) did not survive
repeated cancellation.

