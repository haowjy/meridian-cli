import type {
  ExtensionAPI,
  MessageRenderOptions,
  Theme,
} from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";

import { getForegroundUserBashTaskId, onForegroundBashChange } from "../bash_bridge";
import { safeSendMessage } from "./utils";

export const FOREGROUND_BASH_HINT_CUSTOM_TYPE = "meridian:foreground-bash-hint";

/** In-chat follow-up when interactive `$` blocks the foreground slot. */
export const FOREGROUND_BASH_HINT_TEXT =
  "/ps to manage tasks · /ps:b to run in background";

/** @deprecated Use {@link FOREGROUND_BASH_HINT_TEXT}. */
export const USER_FOREGROUND_BASH_HINT_TEXT = FOREGROUND_BASH_HINT_TEXT;

/** Pi sendMessage options: display-only hint (no agent turn). No followUp — that delays until after bash ends. */
export const FOREGROUND_BASH_HINT_SEND_OPTIONS = {
  triggerTurn: false,
  excludeFromContext: true,
} as const;

type ForegroundBashHintDetails = {
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
let foregroundChangeUnsubscribe: (() => void) | null = null;

export function clearForegroundBashHintDedupe(): void {
  hintedTaskIds.clear();
}

/** Post at most one hint per task id when foreground `$` starts (tests and production). */
export function maybePostForegroundBashHint(pi: ExtensionAPI, taskId: string): boolean {
  if (!taskId || hintedTaskIds.has(taskId)) {
    return false;
  }
  if (getForegroundUserBashTaskId() !== taskId) {
    return false;
  }

  hintedTaskIds.add(taskId);
  safeSendMessage(
    pi,
    {
      customType: FOREGROUND_BASH_HINT_CUSTOM_TYPE,
      content: "",
      display: true,
      details: { taskId, hintText: FOREGROUND_BASH_HINT_TEXT },
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

  foregroundChangeUnsubscribe?.();
  foregroundChangeUnsubscribe = onForegroundBashChange(() => {
    const taskId = getForegroundUserBashTaskId();
    if (taskId != null) {
      maybePostForegroundBashHint(pi, taskId);
    }
  });

  pi.on("session_shutdown", async () => {
    clearForegroundBashHintDedupe();
  });
}

/** Clear listeners when extension unloads (tests). */
export function teardownForegroundBashHint(): void {
  foregroundChangeUnsubscribe?.();
  foregroundChangeUnsubscribe = null;
  clearForegroundBashHintDedupe();
}
