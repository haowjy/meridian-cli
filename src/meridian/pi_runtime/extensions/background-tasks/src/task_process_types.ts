import type { ChildProcess } from "node:child_process";

import type { BackgroundTaskRecord } from "./types";
import type { StoredTaskRecord } from "./task_constants";

export type RuntimeTask = {
  record: StoredTaskRecord;
  child: ChildProcess | null;
  completion: Promise<BackgroundTaskRecord>;
  resolveCompletion: (value: BackgroundTaskRecord) => void;
  logHandle: Awaited<ReturnType<typeof import("node:fs/promises").open>> | null;
  logHandleClosed: boolean;
  logWriteChain: Promise<void>;
};
