import { chmodSync, lstatSync, mkdirSync, unlinkSync } from "node:fs";
import net from "node:net";
import path from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Path computed by the launcher (`control_socket_path`), so the hash lives in one language.
const CONTROL_SOCKET_ENV = "_MERIDIAN_PI_CONTROL_SOCKET";
// Set by the launcher when it passes a launch prompt: bind only after that prompt starts.
const LAUNCH_PROMPT_ENV = "_MERIDIAN_PI_LAUNCH_PROMPT";
// Same limit and read timeout as the Python ControlSocketServer.
const MAX_REQUEST_BYTES = 16 * 1024 * 1024;
const REQUEST_READ_TIMEOUT_MS = 10_000;

type Response = { ok: true; delivery: "posted" } | { ok: false; error: string };

function prepareSocketDirectory(directory: string): void {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const info = lstatSync(directory);
  if (!info.isDirectory() || info.uid !== process.getuid?.()) {
    throw new Error(`socket directory is not owned by the current user: ${directory}`);
  }
  chmodSync(directory, 0o700);
}

function unlinkIfPresent(file: string): void {
  try {
    unlinkSync(file);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
}

// fatal rejects invalid UTF-8 like the Python server; ignoreBOM keeps a BOM in the
// output, so JSON.parse rejects it too.
const UTF8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

function decodeRequest(raw: Buffer): { text: string } | { error: string } {
  if (raw.length === 0) return { error: "empty request" };
  let payload: unknown;
  try {
    payload = JSON.parse(UTF8.decode(raw));
  } catch {
    return { error: "invalid JSON request" };
  }
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    return { error: "request must be a JSON object" };
  }
  const { type, text } = payload as { type?: unknown; text?: unknown };
  if (typeof type !== "string") return { error: "missing request type" };
  if (type !== "user_message") return { error: `unsupported request type: ${type}` };
  if (typeof text !== "string") return { error: "user_message requires text" };
  return { text };
}

export class MessageListener {
  private server: net.Server | null = null;
  private readonly connections = new Set<net.Socket>();
  private compacting = false;

  constructor(
    private readonly pi: ExtensionAPI,
    private readonly socketPath: string,
  ) {}

  setCompacting(value: boolean): void {
    this.compacting = value;
  }

  // onError runs once if the server fails to listen or accept.
  start(onError: (error: Error) => void): void {
    this.stop();
    prepareSocketDirectory(path.dirname(this.socketPath));
    // A crash exit skips session_shutdown and leaves the file behind.
    unlinkIfPresent(this.socketPath);
    const server = net.createServer((socket) => this.serve(socket));
    let reported = false;
    server.on("error", (error) => {
      if (reported || this.server !== server) return;
      reported = true;
      onError(error);
    });
    server.listen(this.socketPath);
    // An open listener must not keep Pi alive past its own exit.
    server.unref();
    this.server = server;
  }

  stop(): void {
    const server = this.server;
    this.server = null;
    this.compacting = false;
    for (const socket of this.connections) socket.destroy();
    this.connections.clear();
    // Closing a unix server unlinks its path.
    server?.close();
  }

  private serve(socket: net.Socket): void {
    this.connections.add(socket);
    socket.unref();
    socket.on("close", () => this.connections.delete(socket));
    socket.on("error", () => {});

    const chunks: Buffer[] = [];
    let size = 0;
    let finished = false;
    const timer = setTimeout(
      () => finish({ ok: false, error: "no request received" }),
      REQUEST_READ_TIMEOUT_MS,
    );
    const finish = (response: Response): void => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      socket.end(`${JSON.stringify(response)}\n`);
    };
    const handle = (raw: Buffer): void => {
      const request = decodeRequest(raw);
      finish("error" in request ? { ok: false, error: request.error } : this.deliver(request.text));
    };

    socket.on("data", (chunk: Buffer) => {
      if (finished) return;
      const newline = chunk.indexOf(0x0a);
      chunks.push(newline === -1 ? chunk : chunk.subarray(0, newline));
      size += chunks[chunks.length - 1].length;
      if (size > MAX_REQUEST_BYTES) {
        finish({
          ok: false,
          error: `message is over the ${MAX_REQUEST_BYTES / (1024 * 1024)} MiB limit`,
        });
      } else if (newline !== -1) {
        handle(Buffer.concat(chunks));
      }
    });
    // A client that closes after its last byte without a newline still sent a request.
    socket.on("end", () => {
      if (!finished) handle(Buffer.concat(chunks));
    });
    socket.on("close", () => clearTimeout(timer));
  }

  private deliver(text: string): Response {
    if (this.compacting) {
      // Pi rejects a prompt during a manual /compact, and only as an extension error in
      // the transcript, so refuse here where the sender can see it. Auto-compaction
      // runs inside the active run and queues a steer, so it is not refused.
      return { ok: false, error: "Pi is compacting its context; send the message again once it finishes" };
    }
    // deliverAs is read only while a run is streaming, so "steer" also starts a turn
    // when idle. Checking isIdle() first would race a run that starts before Pi's
    // async prompt() re-checks. sendUserMessage returns nothing: a later rejection is
    // shown only as an extension error in the TUI, so "posted" is all this can promise.
    this.pi.sendUserMessage(text, { deliverAs: "steer" });
    return { ok: true, delivery: "posted" };
  }
}

export default function meridianMessageExtension(pi: ExtensionAPI): void {
  const socketPath = process.env[CONTROL_SOCKET_ENV]?.trim();
  if (!socketPath) return;
  const listener = new MessageListener(pi, socketPath);

  // The factory starts nothing: it also runs for invocations that never open a session.
  // /new, /resume, /fork and /reload replace the runtime, so each rebinds at session_start.
  // The first startup with a launch prompt binds at agent_start instead: a message that
  // started a run first would make Pi reject the launch prompt, which carries the task.
  let awaitingLaunchPrompt = false;
  let notify: (message: string, type: "warning") => void = () => {};
  const bind = (): void => {
    awaitingLaunchPrompt = false;
    const off = (message: string): void => notify(`Meridian messaging is off: ${message}`, "warning");
    try {
      listener.start((error) => off(error.message));
    } catch (error) {
      off((error as Error).message);
    }
  };

  pi.on("session_start", (event, ctx) => {
    if (ctx.mode !== "tui") return;
    notify = (message, type) => ctx.ui.notify(message, type);
    if (event.reason === "startup" && process.env[LAUNCH_PROMPT_ENV]) {
      awaitingLaunchPrompt = true;
    } else {
      bind();
    }
  });
  pi.on("agent_start", () => {
    if (awaitingLaunchPrompt) bind();
  });
  pi.on("session_before_compact", (event) => {
    if (event.reason === "manual") listener.setCompacting(true);
  });
  pi.on("session_compact", () => listener.setCompacting(false));
  pi.on("session_compact_failed", () => listener.setCompacting(false));
  pi.on("session_shutdown", () => {
    awaitingLaunchPrompt = false;
    listener.stop();
  });
}
