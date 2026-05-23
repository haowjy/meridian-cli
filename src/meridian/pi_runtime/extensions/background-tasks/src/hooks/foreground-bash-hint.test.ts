import { afterEach, describe, expect, it, vi } from "vitest";

import { setForegroundUserBashTaskId } from "../bash_bridge";
import {
  clearForegroundBashHintDedupe,
  FOREGROUND_BASH_HINT_CUSTOM_TYPE,
  FOREGROUND_BASH_HINT_SEND_OPTIONS,
  FOREGROUND_BASH_HINT_TEXT,
  maybePostForegroundBashHint,
  teardownForegroundBashHint,
} from "./foreground-bash-hint";

describe("maybePostForegroundBashHint", () => {
  afterEach(() => {
    teardownForegroundBashHint();
    setForegroundUserBashTaskId(null);
  });

  it("skips hint when task is no longer foreground", () => {
    const sendMessage = vi.fn();
    const pi = { sendMessage } as never;

    expect(maybePostForegroundBashHint(pi, "task-a")).toBe(false);
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("posts user hint once per task id with display-only send options", () => {
    setForegroundUserBashTaskId("task-a");
    const sendMessage = vi.fn();
    const pi = { sendMessage } as never;

    expect(maybePostForegroundBashHint(pi, "task-a")).toBe(true);
    setForegroundUserBashTaskId("task-a");
    expect(maybePostForegroundBashHint(pi, "task-a")).toBe(false);
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        customType: FOREGROUND_BASH_HINT_CUSTOM_TYPE,
        content: "",
        display: true,
        details: {
          taskId: "task-a",
          hintText: FOREGROUND_BASH_HINT_TEXT,
        },
      }),
      FOREGROUND_BASH_HINT_SEND_OPTIONS,
    );
  });
});

describe("setForegroundUserBashTaskId hint wiring", () => {
  afterEach(() => {
    teardownForegroundBashHint();
    setForegroundUserBashTaskId(null);
  });

  it("fires hint when foreground user bash id is set", async () => {
    const { setupForegroundBashHint } = await import("./foreground-bash-hint");
    const sendMessage = vi.fn();
    setupForegroundBashHint({ sendMessage, on: vi.fn(), registerMessageRenderer: vi.fn() } as never);

    setForegroundUserBashTaskId("fg-task-1");
    expect(sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        details: expect.objectContaining({ hintText: FOREGROUND_BASH_HINT_TEXT }),
      }),
      FOREGROUND_BASH_HINT_SEND_OPTIONS,
    );

    setForegroundUserBashTaskId("fg-task-2");
    expect(sendMessage).toHaveBeenCalledTimes(2);
  });
});
