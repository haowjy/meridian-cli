// Only Pi is fake: exercise the shipped bundle with per-runner, per-call ctxs.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import extension from "../../dist/extensions/session-boundary/index.js";

const recordPath = process.env._MERIDIAN_PI_SESSION_BOUNDARY_PATH;
const nonce = process.env._MERIDIAN_PI_SESSION_BOUNDARY_NONCE;
const shape = process.argv[2];
assert.ok(["quit", "restart", "eof", "exit", "eof-race"].includes(shape));
const staleMessage = "This extension ctx is stale after session replacement or reload. Do not use a captured pi or command ctx after ctx.newSession(), ctx.fork(), ctx.switchSession(), or ctx.reload().";
function runner(identity) {
  let active = true;
  const hooks = new Map();
  extension({ on: (type, callback) => hooks.set(type, callback) });
  assert.equal(process.env._MERIDIAN_PI_SESSION_BOUNDARY_PATH, undefined);
  assert.equal(process.env._MERIDIAN_PI_SESSION_BOUNDARY_NONCE, undefined);
  assert.deepEqual([...hooks.keys()].sort(), ["session_before_switch", "session_shutdown", "session_start"]);
  const context = () => ({
    get sessionManager() {
      if (!active) throw new Error(staleMessage);
      return {
        getSessionId: () => identity.session_id,
        getSessionFile: () => identity.session_file,
      };
    },
  });
  return {
    context,
    invalidate: () => { active = false; },
    // Pi runner.emit creates a fresh ctx, even if that runner is already stale.
    emit: (type, reason) => hooks.get(type)({ type, reason }, context()),
  };
}
const a = { session_id: "native-entry", session_file: "/native-store/1_native-entry.jsonl" };
const b = { session_id: "native-exit", session_file: "/native-store/2_native-exit.jsonl" };
const oldRunner = runner(a);
const captured = oldRunner.context();
const read = () => JSON.parse(readFileSync(recordPath, "utf8"));
await oldRunner.emit("session_start", "startup");
await oldRunner.emit("session_start", "startup");
assert.equal(read().revision, 1);
await oldRunner.emit("session_before_switch", "new");
await oldRunner.emit("session_shutdown", "new");
assert.equal(read().quit, null);
assert.equal(read().invalid_reason, null);
oldRunner.invalidate();
assert.throws(() => captured.sessionManager, { message: staleMessage });

let expected = {
  v: 2, launch_nonce: nonce, pid: process.pid, revision: 4,
  initial: a, current: a, quit: null, invalid_reason: null,
  last_event: { type: "session_shutdown", reason: "quit" },
};
if (shape === "eof-race") {
  // Real Pi 0.87.1: EOF can dispose A after invalidation but before B starts.
  await oldRunner.emit("session_shutdown", "quit");
} else {
  const nextRunner = runner(b);
  assert.equal(nextRunner.context().sessionManager.getSessionId(), b.session_id);
  await nextRunner.emit("session_start", "new");
  expected = { ...expected, current: b, last_event: { type: "session_start", reason: "new" } };
  if (shape !== "exit") {
    // EOF after new_session responds invokes runtime.dispose -> shutdown/quit.
    await nextRunner.emit("session_shutdown", "quit");
    expected = { ...expected, revision: 5, quit: b,
      last_event: { type: "session_shutdown", reason: "quit" } };
    assert.deepEqual(read(), expected);
  }
  if (shape === "restart") {
    await nextRunner.emit("session_start", "new");
    expected = { ...expected, revision: 6, quit: null,
      last_event: { type: "session_start", reason: "new" } };
  }
  // Abrupt process exit supplies no lifecycle callback, hence no verified exit.
}
assert.deepEqual(read(), expected);
