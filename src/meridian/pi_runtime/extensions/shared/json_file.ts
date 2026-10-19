import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

/** Missing private state is empty. Present unreadable or invalid JSON is unknown. */
export async function readPrivateJson(filePath: string): Promise<unknown | undefined> {
  try { return JSON.parse(await readFile(filePath, "utf8")) as unknown; }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
}

export async function writeJsonAtomic(filePath: string, data: unknown): Promise<void> {
  await mkdir(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.tmp-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  await writeFile(tmpPath, `${JSON.stringify(data, null, 2)}\n`, "utf-8");
  await rename(tmpPath, filePath);
}
