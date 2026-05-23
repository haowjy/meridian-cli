import { afterEach, describe, expect, it, vi } from "vitest";

import { setForegroundUserBashTaskId } from "../bash_bridge";
import {
  AGENT_FOREGROUND_BASH_HINT_TEXT,
  clearForegroundBashHintDedupe,
  FOREGROUND_BASH_HINT_CUSTOM_TYPE,
  FOREGROUND_BASH_HINT_SEND_OPTIONS,
  maybePostForegroundBashHint,
  teardownForegroundBashHint,
  USER_FOREGROUND_BASH_HINT_TEXT,
} from "./foreground-bash-hint";

describe("maybePostForegroundBashHint", () => {
  afterEach(() => {
    teardownForegroundBashHint();
    setForegroundUserBashTaskId(null);
  });

  it("posts user hint once per task id with display-only send options", () => {
    const sendMessage = vi.fn();
    const pi = { sendMessage } as never;

    expect(maybePostForegroundBashHint(pi, "task-a", "user")).toBe(true);
    expect(maybePostForegroundBashHint(pi, "task-a", "user")).toBe(false);
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        customType: FOREGROUND_BASH_HINT_CUSTOM_TYPE,
        content: "",
        display: true,
        details: {
          kind: "user",
          taskId: "task-a",
          hintText: USER_FOREGROUND_BASH_HINT_TEXT,
        },
      }),
      FOREGROUND_BASH_HINT_SEND_OPTIONS,
    );
  });

  it("posts agent hint only for tracked wait_policy", () => {
    const sendMessage = vi.fn();
    const pi = { sendMessage } as never;

    expect(maybePostForegroundBashHint(pi, "job-1", "agent", "detached")).toBe(false);
    expect(sendMessage).not.toHaveBeenCalled();

    clearForegroundBashHintDedupe();
    expect(maybePostForegroundBashHint(pi, "job-1", "agent", "tracked")).toBe(true);
    expect(sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        content: "",
        details: {
          kind: "agent",
          taskId: "job-1",
          hintText: AGENT_FOREGROUND_BASH_HINT_TEXT,
        },
      }),
      expect.objectContaining({
        triggerTurn: false,
        excludeFromContext: true,
      }),
    );
  });

  it("dedupes across user and agent for the same task id", () => {
    const sendMessage = vi.fn();
    const pi = { sendMessage } as never;

    expect(maybePostForegroundBashHint(pi, "shared-id", "user")).toBe(true);
    expect(maybePostForegroundBashHint(pi, "shared-id", "agent", "tracked")).toBe(false);
    expect(sendMessage).toHaveBeenCalledTimes(1);
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
        details: expect.objectContaining({ hintText: USER_FOREGROUND_BASH_HINT_TEXT }),
      }),
      FOREGROUND_BASH_HINT_SEND_OPTIONS,
    );

    setForegroundUserBashTaskId("fg-task-2");
    expect(sendMessage).toHaveBeenCalledTimes(2);
  });
});
