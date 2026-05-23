import type {
  ExtensionAPI,
  MessageRenderOptions,
  Theme,
} from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";

import {
  getForegroundUserBashTaskId,
  setOnAgentBashRunning,
  setOnForegroundBashChange,
} from "../bash_bridge";
import { safeSendMessage } from "./utils";

export const FOREGROUND_BASH_HINT_CUSTOM_TYPE = "meridian:foreground-bash-hint";

/** In-chat follow-up when interactive `$` blocks the foreground slot (display-only; not LLM context). */
export const FOREGROUND_BASH_HINT_TEXT =
  "/ps to manage tasks · ctrl+b to run in background";

export type ForegroundBashHintKind = "user" | "agent";

type ForegroundBashHintDetails = {
  kind: ForegroundBashHintKind;
  taskId: string;
};

type ForegroundBashHintMessage = {
  customType: string;
  content: string;
  details?: ForegroundBashHintDetails;
};

const hintedTaskIds = new Set<string>();

export function clearForegroundBashHintDedupe(): void {
  hintedTaskIds.clear();
}

function isTrackedWaitPolicy(waitPolicy: unknown): boolean {
  return waitPolicy !== "detached";
}

/** Post at most one hint per task id (tests and production). */
export function maybePostForegroundBashHint(
  pi: ExtensionAPI,
  taskId: string,
  kind: ForegroundBashHintKind,
  waitPolicy?: unknown,
): boolean {
  if (!taskId || hintedTaskIds.has(taskId)) {
    return false;
  }
  if (kind === "agent" || !isTrackedWaitPolicy(waitPolicy)) {
    return false;
  }

  hintedTaskIds.add(taskId);
  safeSendMessage(
    pi,
    {
      customType: FOREGROUND_BASH_HINT_CUSTOM_TYPE,
      content: FOREGROUND_BASH_HINT_TEXT,
      display: true,
      details: { kind, taskId },
    },
    { deliverAs: "followUp", triggerTurn: false },
  );
  return true;
}

function renderHintLine(message: ForegroundBashHintMessage, theme: Theme): Text {
  const text = typeof message.content === "string" ? message.content : "";
  return new Text(theme.fg("dim", text), 0, 0);
}

export function setupForegroundBashHint(pi: ExtensionAPI): void {
  if (typeof pi.registerMessageRenderer === "function") {
    pi.registerMessageRenderer<ForegroundBashHintDetails>(
      FOREGROUND_BASH_HINT_CUSTOM_TYPE,
      (message: ForegroundBashHintMessage, _options: MessageRenderOptions, theme: Theme) =>
        renderHintLine(message, theme),
    );
  }

  setOnForegroundBashChange(() => {
    const taskId = getForegroundUserBashTaskId();
    if (taskId != null) {
      maybePostForegroundBashHint(pi, taskId, "user");
    }
  });

  setOnAgentBashRunning((taskId, waitPolicy) => {
    maybePostForegroundBashHint(pi, taskId, "agent", waitPolicy);
  });

  pi.on("session_shutdown", async () => {
    clearForegroundBashHintDedupe();
  });
}

/** Clear listeners when extension unloads (tests). */
export function teardownForegroundBashHint(): void {
  setOnForegroundBashChange(null);
  setOnAgentBashRunning(null);
  clearForegroundBashHintDedupe();
}
