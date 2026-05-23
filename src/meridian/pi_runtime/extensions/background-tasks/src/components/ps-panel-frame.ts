import type { Theme } from "@earendil-works/pi-coding-agent";
import { Container } from "@earendil-works/pi-tui";

import type { TaskPanelHost } from "../panel/host";
import { TasksPanelComponent } from "./tasks-panel-component";

export type PsPanelActions = {
  onQuit: () => void;
  /** Open full-screen log stream overlay; /ps stays open underneath. */
  onOpenStream: (taskId: string) => void | Promise<void>;
};

/**
 * Container shell so Pi keeps keyboard focus on the panel (same pattern as ExtensionSelector).
 */
export class PsPanelFrame extends Container {
  private readonly panel: TasksPanelComponent;

  constructor(
    tui: {
      requestRender: () => void;
      setFocus: (component: unknown) => void;
      terminal?: { rows?: number };
    },
    theme: Theme,
    actions: PsPanelActions,
    host: TaskPanelHost,
  ) {
    super();
    this.panel = new TasksPanelComponent(tui, theme, actions, host);
    tui.setFocus(this);
  }

  dispose(): void {
    this.panel.dispose();
  }

  invalidate(): void {
    this.panel.invalidate();
  }

  render(width: number): string[] {
    return this.panel.render(width);
  }

  handleInput(data: string): void {
    this.panel.handleInput(data);
  }
}
