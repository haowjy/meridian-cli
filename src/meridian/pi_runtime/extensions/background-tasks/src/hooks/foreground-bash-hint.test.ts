import { afterEach, describe, expect, it, vi } from "vitest";

import { setForegroundUserBashTaskId } from "../bash_bridge";
import {
  AGENT_FOREGROUND_BASH_HINT_TEXT,
  clearForegroundBashHintDedupe,
  FOREGROUND_BASH_HINT_CUSTOM_TYPE,
  maybePostForegroundBashHint,
  teardownForegroundBashHint,
  USER_FOREGROUND_BASH_HINT_TEXT,
} from "./foreground-bash-hint";

describe("maybePostForegroundBashHint", () => {
  afterEach(() => {
    teardownForegroundBashHint();
    setForegroundUserBashTaskId(null);
  });

  it("posts user hint once per task id", () => {
    const sendMessage = vi.fn();
    const pi = { sendMessage } as never;

    expect(maybePostForegroundBashHint(pi, "task-a", "user")).toBe(true);
    expect(maybePostForegroundBashHint(pi, "task-a", "user")).toBe(false);
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        customType: FOREGROUND_BASH_HINT_CUSTOM_TYPE,
        content: USER_FOREGROUND_BASH_HINT_TEXT,
        display: true,
        details: { kind: "user", taskId: "task-a" },
      }),
      { deliverAs: "followUp", triggerTurn: false },
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
        content: AGENT_FOREGROUND_BASH_HINT_TEXT,
        details: { kind: "agent", taskId: "job-1" },
      }),
      { deliverAs: "followUp", triggerTurn: false },
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
      expect.objectContaining({ content: USER_FOREGROUND_BASH_HINT_TEXT }),
      expect.any(Object),
    );

    setForegroundUserBashTaskId("fg-task-2");
    expect(sendMessage).toHaveBeenCalledTimes(2);
  });
});
