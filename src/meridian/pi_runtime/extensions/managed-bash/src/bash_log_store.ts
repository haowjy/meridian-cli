import { mkdir, open, stat, writeFile } from "node:fs/promises";
import path from "node:path";

export type BashLogStream = "combined" | "stdout" | "stderr";

export type BashLogPaths = {
  combined: string;
  stdout: string;
  stderr: string;
};

export class BashLogStore {
  constructor(private readonly logsDir: string) {}

  async create(bashId: string): Promise<BashLogPaths> {
    await mkdir(this.logsDir, { recursive: true });
    const paths = this.pathsFor(bashId);
    await Promise.all([
      writeFile(paths.combined, "", "utf-8"),
      writeFile(paths.stdout, "", "utf-8"),
      writeFile(paths.stderr, "", "utf-8"),
    ]);
    return paths;
  }

  async append(paths: BashLogPaths, stream: Exclude<BashLogStream, "combined">, chunk: string): Promise<number> {
    await Promise.all([
      writeFile(paths.combined, chunk, { encoding: "utf-8", flag: "a" }),
      writeFile(paths[stream], chunk, { encoding: "utf-8", flag: "a" }),
    ]);
    return (await stat(paths.combined)).size;
  }

  async read(paths: BashLogPaths, stream: BashLogStream, maxBytes: number): Promise<string> {
    return readLogTail(paths[stream], maxBytes);
  }

  private pathsFor(bashId: string): BashLogPaths {
    return {
      combined: path.join(this.logsDir, `${bashId}.log`),
      stdout: path.join(this.logsDir, `${bashId}.stdout.log`),
      stderr: path.join(this.logsDir, `${bashId}.stderr.log`),
    };
  }
}

export type LogReadResult =
  | { kind: "available"; text: string; truncated: boolean }
  | { kind: "missing"; error: string }
  | { kind: "unreadable"; error: string };

/** Bounded result retrieval preserves availability: missing output is not empty output. */
export async function readLogResult(filePath: string, maxBytes: number): Promise<LogReadResult> {
  let file;
  try {
    file = await open(filePath, "r");
    const size = (await file.stat()).size;
    const length = Math.max(0, Math.min(size, Math.floor(maxBytes)));
    const buffer = Buffer.alloc(length);
    let bytesRead = 0;
    while (bytesRead < length) {
      const read = await file.read(buffer, bytesRead, length - bytesRead, Math.max(0, size - length) + bytesRead);
      if (read.bytesRead === 0) throw new Error(`Log changed while reading: ${filePath}`);
      bytesRead += read.bytesRead;
    }
    let start = 0;
    // A byte tail may begin inside a UTF-8 code point.
    if (size > length) while (start < bytesRead && (buffer[start]! & 0xc0) === 0x80) start += 1;
    return { kind: "available", text: buffer.subarray(start, bytesRead).toString("utf-8"), truncated: size > length };
  } catch (error) {
    const kind = (error as NodeJS.ErrnoException).code === "ENOENT" ? "missing" : "unreadable";
    return { kind, error: `Log ${kind}: ${filePath}: ${error instanceof Error ? error.message : String(error)}` };
  } finally { await file?.close(); }
}

/** Non-consuming UI inspection may treat an absent preview as empty. */
export async function readLogTail(filePath: string, maxBytes: number): Promise<string> {
  const result = await readLogResult(filePath, maxBytes);
  if (result.kind === "unreadable") throw new Error(result.error);
  return result.kind === "available" ? result.text : "";
}
