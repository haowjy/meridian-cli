// Only Pi is fake: import the shipped bundle and use its default registration API.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import extension from "../../dist/extensions/session-boundary/index.js";

const recordPath = process.env._MERIDIAN_PI_SESSION_BOUNDARY_PATH;
const nonce = process.env._MERIDIAN_PI_SESSION_BOUNDARY_NONCE;
const shape = process.argv[2];
assert.ok(shape === "quit" || shape === "restart");
const hooks = new Map();
extension({ on: (type, callback) => hooks.set(type, callback) });
assert.equal(process.env._MERIDIAN_PI_SESSION_BOUNDARY_PATH, undefined);
assert.equal(process.env._MERIDIAN_PI_SESSION_BOUNDARY_NONCE, undefined);
assert.deepEqual([...hooks.keys()].sort(), ["session_before_switch", "session_shutdown", "session_start"]);

const a = { session_id: "native-entry", session_file: "/native-store/1_native-entry.jsonl" };
const b = { session_id: "native-exit", session_file: "/native-store/2_native-exit.jsonl" };
let identity = a;
const context = { sessionManager: {
  getSessionId: () => identity.session_id,
  getSessionFile: () => identity.session_file,
} };
const emit = async (type, reason) => hooks.get(type)({ type, reason }, context);
const read = () => JSON.parse(readFileSync(recordPath, "utf8"));
await emit("session_start", "startup");
await emit("session_start", "startup"); // Duplicate startup must not advance revision.
assert.equal(read().revision, 1);
await emit("session_before_switch", "resume");
await emit("session_shutdown", "resume");
identity = b;
await emit("session_start", "resume");
await emit("session_shutdown", "quit");
const quit = {
  v: 1, launch_nonce: nonce, pid: process.pid, revision: 5,
  initial: a, current: b, quit: b, invalid_reason: null,
  last_event: { type: "session_shutdown", reason: "quit" },
};
assert.deepEqual(read(), quit);
if (shape === "restart") {
  await emit("session_start", "new");
  assert.deepEqual(read(), {
    ...quit, revision: 6, quit: null,
    last_event: { type: "session_start", reason: "new" },
  });
}
