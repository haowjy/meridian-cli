# Adding a harness to Meridian

A harness integration supplies an adapter, projection, transport, event semantics,
and native transcript reader. Keep harness-specific behavior in `lib/harness/`;
shared launch and streaming code consume those ports. Pi is the worked example
below, using the current installed-runtime integration.

## Start with runtime evidence

Probe the real binary before designing the adapter. Use a cheap model and capture:

- `--version` and `--help`, including model, prompt, session, and permission flags;
- one successful turn and one provider/model failure;
- streaming/RPC acknowledgements, tool results, usage, and terminal events;
- native session storage, resume, fork, and session-switch behavior;
- ambient extension, skill, context-file, and config discovery;
- cancellation, EOF, and process-group cleanup.

Do not infer identity from cwd, timestamps, or the newest transcript. Determine how
an integration can assign an entry identity and obtain launch-correlated exit
identity. If it cannot prove an exit, return unresolved evidence.

## Adapter and registration

Read these contracts before implementing:

- [Harness intent](../src/meridian/lib/harness/AGENTS.md)
- [Harness architecture](../src/meridian/lib/harness/.context/CONTEXT.md)
- [Adapter API](../src/meridian/lib/harness/adapter.py)
- [Launch intent](../src/meridian/lib/launch/AGENTS.md)

`HARNESS_EXTENSION_TOUCHPOINTS` in
[`harness/__init__.py`](../src/meridian/lib/harness/__init__.py) lists the registration
seams. Add the harness ID and CLI routing, then register a `HarnessBundle` with its
adapter, spec class, extractor, transport map, projection ports, and semantics.
`HarnessRegistry.with_defaults()` includes the default adapter.

The adapter declares capabilities and a `HarnessContract`, resolves `SpawnParams`
into a launch spec, and projects composed content onto the harness's actual
instruction and user-turn channels. Declare every `SpawnParams` field in
`consumed_fields` or `explicitly_ignored_fields`; import-time accounting rejects
uncovered fields.

Use `env_defaults()` for launch-aware values that explicit child environment may
override, and reserve `env_overrides()` for forced adapter policy; agent/auth
directories and native transcript stores need explicit contracts, not blanket
config isolation.

### Native identity

Native-session adapters opt into the base template with `native_identity = True`.
Implement the primitives rather than overriding `plan_native_identity()` or
`finalize_native_identity()`:

| Port | Responsibility |
|---|---|
| `native_store_for_launch()` | Resolve the absolute store from child env/cwd and operation, without writes |
| `pin_native_store()` | Pin that store in the child environment |
| `assign_session_id()` | Retain a resume ID or assign an owned create/fork ID |
| `validate_intent()` | Reject unsupported sources or identity operations before exec |
| `refused_identity_flags` | Reject passthrough selectors that would contradict the bound identity |
| `continues_in_source_store` | Declare operations that retain the source namespace |
| `resolves_untracked_source` | Declare whether raw resume references can be resolved |
| `resolve_native_session_file()` | Resolve and validate the exact ID within its recorded store |
| `observe_after_exit()` | Return `PostExit` evidence; do not bind or persist it |

Launch owns entry binding, exit allocation, and invocation attribution. Transcript
reads use the recorded `(harness, native_store, id)`; ambiguity is an error, never a
reason to choose a replacement.

## Projection, transport, and facts

Projectors translate the resolved spec into argv/env or transport payloads. Split
launch modes only when their protocols differ. Declare `_PROJECTED_FIELDS` and
`_DELEGATED_FIELDS`, and run `check_projection_drift()` at module import.

Define passthrough policy per flag: model overrides can be last-wins, but identity,
transport mode, and managed extension selectors must not silently contradict
Meridian's launch. Keep interactive starting prompts in the harness's supported
prompt channel and apply the shared argument-size guard where prompts use argv.

A connection implements start, stop, event streaming, cancellation, and supported
control messages. Keep RPC acknowledgements independent of event consumption;
bound writes, acknowledgements, and shutdown waits. Treat uncertain prompt delivery
as uncertain, not an invitation to replay it.

Register event-name descriptors through the bundle's `HarnessSemantics`. Payload
resolvers classify outcomes that depend on content. Shared normalization dispatches
by harness first; never add harness event names to shared `semantics.py`. If a
stream multiplexes child sessions, expose `primary_event_scope` so child terminal
events cannot finish or fail the parent.

Extractors fold live attempt facts: report text, native reply IDs, and usage.
Count each usage increment once; absent or malformed operands stay unknown. A
native-turn fallback may read only replies named by this attempt's events from
its recorded store. Stderr is diagnostic, not identity or completion authority.

Bootstrap order is load-bearing: adapter bundle registration, projection drift
guards, extractor wiring, then cross-adapter field accounting.

## Pi: installed runtime and four extensions

Meridian resolves `MERIDIAN_PI_BINARY`, otherwise `pi` on `PATH`, and probes
`--version`/`--help` before launch. It does not bundle Pi or provide a wrapper
runtime. Both roles require stable Pi >=1.1.0 and <2, matching the managed
extensions' peer contract. Older, prerelease, unrecognized and newer-major
versions fail before model work. The compatibility probe additionally checks
different flag surfaces for native primary and spawned RPC roles; requirements live in
[`pi_runtime_resolver.py`](../src/meridian/lib/harness/pi_runtime_resolver.py).

Spawned completion requires Pi's 1.1 lifecycle contract: `agent_settled` with a
Boolean `aborted`, plus compaction start/end events. It is runtime-qualified on
Pi 1.1.0, which is also the pinned extension-development SDK. The version and
flag gates reject known unsupported runtimes early; actual settlement frames
remain validated fail-closed.

| Launch | Transport and discovery |
|---|---|
| Primary | Native Pi TUI, no `--mode rpc`; native ambient discovery remains enabled |
| Spawned | `pi --mode rpc`; suppress skills, context files, and prompt templates; suppress ambient extensions unless `load_all_pi_extensions = true` |

Both roles load the stable bundles listed below where enabled; `meridian-idle` loads only for interactive primaries:

- **managed-bash**, when `harness.pi.background_tasks.enabled` is true and
  `disable_managed_bash` is false: `bash`/`bash_manage`, `/ps*`, task records,
  process-group ownership, and reload/recovery.
- **meridian-spawn-watch**, when `harness.pi.spawn_watch.enabled` is true:
  canonical direct-child observation, `/spawn*`, and idle-turn result delivery.
- **session-boundary**, always: bounded native lifecycle evidence, correlated by
  launch nonce and Pi PID. Only a final readable `session_shutdown(reason=quit)`
  verifies exit identity; a switch, reload, stale context, or corrupt record does
  not authorize a last-seen-session fallback.
- **meridian-idle**, interactive primaries only: translates Pi idle/input events
  and live context facts into core idle decisions. It never loads in spawned RPC
  sessions.

Primary child notifications do not auto-stop the TUI. Spawned completion uses
quiescence: parent idle, reconciled transitive descendants finished, private work
resolved, and owed results consumed or causally delivered. Extensions write
durable files; RPC stdout stays reserved for the native transport.

Pi agent/auth state defaults to `~/.pi/agent`, honoring `PI_CODING_AGENT_DIR`.
Managed native sessions default to `~/.meridian/meridian-pi/sessions/`, honoring
`PI_CODING_AGENT_SESSION_DIR`; fresh spawns get a spawn-scoped subdirectory,
primaries use the flat root, and resumes retain their recorded store. Identity
projection passes `--session-dir` plus an assigned `--session-id` for create/fork,
or the exact `--session <file>` for resume. Fork also passes `--fork <source-file>`.

Interactive primaries also default `PI_CACHE_RETENTION=long` when the variable is
absent; spawned RPC sessions are untouched and an inherited or configured value
always wins. Set `PI_CACHE_RETENTION=short` to retain Pi's short-cache default.
This launch default is independent of `[idle]`: the idle extension reads the
effective variable and Pi's reported provider to schedule cache warning and
compaction for `anthropic` and `openai`. `openai` is scheduled against 30
minutes, because OpenAI's `24h` retention typically lasts about that long.
Unknown provider IDs remain push-only.

### Build and package

TypeScript sources live under `src/meridian/pi_runtime/extensions/`. Build all
four bundles before launch:

```bash
cd src/meridian/pi_runtime
pnpm install --frozen-lockfile
pnpm run verify:extensions
```

Output is `dist/extensions/<name>/index.js`. Projection prefers the package's
built bundles and falls back to `~/.meridian/pi/extensions/`; it does not copy
extensions into per-launch agent directories. A missing required bundle fails
with build guidance. The Python distribution includes the compiled JS as package
data, so end users do not build extensions during a launch.

See the [extension contributor guide](../src/meridian/pi_runtime/README.md),
[Pi integration contract](../src/meridian/lib/harness/.context/pi-integration.md),
and [delivery contract](../src/meridian/pi_runtime/.context/delivery-contract.md)
for ownership and crash semantics.

## Native transcripts and Mars

Implement transcript resolution, parsing, capture qualification, and witnesses for
the harness's native format. `session log`, export, search, and archives read native
transcripts, not a Meridian runner-history copy. Schema changes must update the
exact readers and their focused checks together.

Mars owns model aliases, listing evidence, and launch routing. Provide a harness
model namespace and launch route; do not route by model-name patterns or display
curation. For Pi, compare:

```bash
meridian mars models list --harness pi --all --live
pi --list-models
```

A listed model still needs a successful real launch to prove runtime eligibility.

## Verification

Verify the smallest useful seam first, then the full project gate:

1. Import/bootstrap, field accounting, argv/env projection, event semantics,
   usage folding, and exact native-identity validation.
2. Real cheap-model success and failure, cancellation, control injection,
   resume/fork, and readable `session log`.
3. Primary and spawned launch shapes separately. For Pi use the
   [manual gate](../tests/smoke/pi-manual.md) and relevant
   [quiescence scenarios](../tests/smoke/pi-rpc-quiescence.md).
4. Built wheel/sdist contents and an installed-package launch. Pi artifact
   checks live in `scripts/verify-pi-extension-artifacts.py`.
5. Before shipping harness/lifecycle changes, run the explicit
   `scripts/preflight.sh full`: Ruff, Pyright, locked Pi dependency install and
   bundle build, every Python test, and `uv build --no-sources`. Routine branch
   pushes run the 60-second fast gate instead; full/compatibility CI runs on
   dispatch/nightly. See [development setup](../DEVELOPMENT.md).

Automated fault fixtures protect destructive or hard-to-reproduce boundaries;
real-runtime smoke proves the CLI workflow. Report those evidence types separately.
