import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
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

  async append(paths: BashLogPaths, stream: Exclude<BashLogStream, "combined">, chunk: string): Promise<number | null> {
    await Promise.all([
      writeFile(paths.combined, chunk, { encoding: "utf-8", flag: "a" }),
      writeFile(paths[stream], chunk, { encoding: "utf-8", flag: "a" }),
    ]);
    try {
      return (await stat(paths.combined)).size;
    } catch {
      return null;
    }
  }

  async read(paths: BashLogPaths, stream: BashLogStream, maxBytes: number): Promise<string> {
    try {
      const content = await readFile(paths[stream], "utf-8");
      return content.slice(Math.max(0, content.length - maxBytes));
    } catch {
      return "";
    }
  }

  private pathsFor(bashId: string): BashLogPaths {
    return {
      combined: path.join(this.logsDir, `${bashId}.log`),
      stdout: path.join(this.logsDir, `${bashId}.stdout.log`),
      stderr: path.join(this.logsDir, `${bashId}.stderr.log`),
    };
  }
}
