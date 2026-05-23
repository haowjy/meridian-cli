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

/** In-chat follow-up when interactive `$` blocks the foreground slot. */
export const USER_FOREGROUND_BASH_HINT_TEXT =
  "Running in foreground — /ps · ctrl+b to background · & to detach";

/** In-chat follow-up when agent `bash` is tracked (foreground tool run). */
export const AGENT_FOREGROUND_BASH_HINT_TEXT = "Agent bash running — see /ps";

/** Pi sendMessage options: display-only hint (no agent turn). */
export const FOREGROUND_BASH_HINT_SEND_OPTIONS = {
  deliverAs: "followUp",
  triggerTurn: false,
  excludeFromContext: true,
} as const;

export type ForegroundBashHintKind = "user" | "agent";

type ForegroundBashHintDetails = {
  kind: ForegroundBashHintKind;
  taskId: string;
  /** Shown in TUI only; kept out of message `content` so convertToLlm omits hint text. */
  hintText: string;
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
  if (kind === "agent" && !isTrackedWaitPolicy(waitPolicy)) {
    return false;
  }

  const hintText =
    kind === "user" ? USER_FOREGROUND_BASH_HINT_TEXT : AGENT_FOREGROUND_BASH_HINT_TEXT;

  hintedTaskIds.add(taskId);
  safeSendMessage(
    pi,
    {
      customType: FOREGROUND_BASH_HINT_CUSTOM_TYPE,
      content: "",
      display: true,
      details: { kind, taskId, hintText },
    },
    FOREGROUND_BASH_HINT_SEND_OPTIONS,
  );
  return true;
}

function hintTextFromMessage(message: ForegroundBashHintMessage): string {
  const fromDetails = message.details?.hintText;
  if (typeof fromDetails === "string" && fromDetails.length > 0) {
    return fromDetails;
  }
  return typeof message.content === "string" ? message.content : "";
}

function renderHintLine(message: ForegroundBashHintMessage, theme: Theme): Text {
  return new Text(theme.fg("dim", hintTextFromMessage(message)), 0, 0);
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
