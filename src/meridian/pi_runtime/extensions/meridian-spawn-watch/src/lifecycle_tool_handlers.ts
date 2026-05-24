import type { ExtensionAPI } from "../../types";
import { isMeridianSpawnCommand } from "../../shared/meridian_spawn";
import type { LifecycleChildTracker } from "./lifecycle_child_tracker";
import type { ToolResultEvent } from "./lifecycle_types";
import {
  commandFromEvent,
  jobIdFrom,
  persistentFromEvent,
  waitPolicyFrom,
} from "./lifecycle_utils";

export function registerLifecycleToolHandlers(
  _pi: ExtensionAPI,
  tracker: LifecycleChildTracker,
): void {
  const handleToolResult = (event: ToolResultEvent): void => {
    const toolName = event.toolName ?? "";
    const command = commandFromEvent(event);
    const commandLooksLikeMeridianSpawn = isMeridianSpawnCommand(command);
    const resultJobId = jobIdFrom(event);
    const session = tracker.session;

    if (toolName === "background_task" && (event.details as { action?: string })?.action === "start") {
      const taskId = resultJobId;
      if (taskId && commandLooksLikeMeridianSpawn) {
        session.meridianSpawnWrapperJobs.add(taskId);
        tracker.addTrackedChild(taskId, waitPolicyFrom(event), "meridian_spawn", persistentFromEvent(event));
        tracker.setChildPid(taskId, event.details?.pid ?? null);
        tracker.handleObservedMeridianSpawnOutput(event, true, taskId);
        tracker.ensureChildStatusPoller();
      }
      return;
    }

    if (toolName === "bash") {
      if (event.details?.state === "running") {
        const jobId = resultJobId;
        if (jobId) {
          const kind = commandLooksLikeMeridianSpawn ? "meridian_spawn" : "bash";
          tracker.addTrackedChild(jobId, waitPolicyFrom(event), kind, persistentFromEvent(event));
          tracker.setChildPid(jobId, event.details?.pid ?? null);
          if (commandLooksLikeMeridianSpawn) {
            session.meridianSpawnWrapperJobs.add(jobId);
          }
        }
      }

      if (
        commandLooksLikeMeridianSpawn ||
        (resultJobId != null && session.meridianSpawnWrapperJobs.has(resultJobId))
      ) {
        tracker.handleObservedMeridianSpawnOutput(event, true, resultJobId);
        tracker.ensureChildStatusPoller();
      }

      if (event.details?.state === "exited") {
        const jobId = resultJobId;
        if (jobId && !session.meridianSpawnWrapperJobs.has(jobId)) {
          tracker.maybeRemoveChild(jobId);
        }
      }
      return;
    }

    if (
      toolName === "background_task" &&
      ["wait", "cancel"].includes(String((event.details as { action?: string })?.action ?? ""))
    ) {
      const taskId = resultJobId;
      const meridianAssociated = !!taskId && session.meridianSpawnWrapperJobs.has(taskId);
      tracker.handleObservedMeridianSpawnOutput(event, meridianAssociated, taskId);
      if (taskId && event.details?.found !== false) {
        const task = (event.details as { task?: { status?: string } })?.task;
        const status = task?.status ?? event.details?.job?.status;
        if (status && status !== "running" && !session.meridianSpawnWrapperJobs.has(taskId)) {
          tracker.maybeRemoveChild(taskId);
        }
      }
      tracker.ensureChildStatusPoller();
      return;
    }

    if (toolName === "background_task" && (event.details as { action?: string })?.action === "output") {
      const taskId = resultJobId;
      const meridianAssociated = !!taskId && session.meridianSpawnWrapperJobs.has(taskId);
      tracker.handleObservedMeridianSpawnOutput(event, meridianAssociated, taskId);
      tracker.ensureChildStatusPoller();
      return;
    }

    if (
      toolName === "background_task" &&
      (event.details as { action?: string })?.action === "list"
    ) {
      const jobs =
        (event.details as { tasks?: Array<Record<string, unknown>> })?.tasks ??
        event.details?.jobs;
      if (!Array.isArray(jobs)) {
        return;
      }
      let hasMeridianWrapper = false;
      for (const job of jobs) {
        const jobId = (job.task_id as string) ?? (job.job_id as string);
        if (!jobId) {
          continue;
        }
        const jobCommand = typeof job.command === "string" ? job.command : "";
        const isMeridianWrapper =
          isMeridianSpawnCommand(jobCommand) || session.meridianSpawnWrapperJobs.has(jobId);
        if (isMeridianWrapper) {
          hasMeridianWrapper = true;
        }
        if (job.status === "running") {
          tracker.addTrackedChild(
            jobId,
            job.wait_policy === "detached" ? "detached" : "tracked",
            isMeridianWrapper ? "meridian_spawn" : "bash",
            job.persistent === true,
          );
          if (isMeridianWrapper) {
            session.meridianSpawnWrapperJobs.add(jobId);
          }
        } else if (!session.meridianSpawnWrapperJobs.has(jobId)) {
          tracker.maybeRemoveChild(jobId);
        }
      }
      tracker.handleObservedMeridianSpawnOutput(event, hasMeridianWrapper, resultJobId);
      tracker.ensureChildStatusPoller();
    }
  };

  tracker.setToolResultHandler(handleToolResult);
}
