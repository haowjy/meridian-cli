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

/** Read at most the requested bytes; task logs may be much larger than RAM. */
export async function readLogTail(filePath: string, maxBytes: number): Promise<string> {
  let file;
  try {
    file = await open(filePath, "r");
    const size = (await file.stat()).size;
    const length = Math.max(0, Math.min(size, Math.floor(maxBytes)));
    const buffer = Buffer.alloc(length);
    const { bytesRead } = await file.read(buffer, 0, length, Math.max(0, size - length));
    let start = 0;
    // A byte tail may begin inside a UTF-8 code point.
    if (size > length) while (start < bytesRead && (buffer[start]! & 0xc0) === 0x80) start += 1;
    return buffer.subarray(start, bytesRead).toString("utf-8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return "";
    throw error;
  } finally { await file?.close(); }
}
