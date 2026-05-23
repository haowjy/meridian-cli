import type { Theme } from "@earendil-works/pi-coding-agent";
import { Container } from "@earendil-works/pi-tui";

import type { TaskPanelHost } from "../panel/host";
import { TasksPanelComponent } from "./tasks-panel-component";

/**
 * Container shell so Pi keeps keyboard focus on the panel (same pattern as ExtensionSelector).
 */
export class PsPanelFrame extends Container {
  private readonly panel: TasksPanelComponent;

  constructor(
    tui: { requestRender: () => void; setFocus: (component: unknown) => void },
    theme: Theme,
    onClose: (taskId?: string) => void,
    host: TaskPanelHost,
  ) {
    super();
    this.panel = new TasksPanelComponent(tui, theme, onClose, host);
    tui.setFocus(this);
  }

  render(width: number): string[] {
    return this.panel.render(width);
  }

  invalidate(): void {
    this.panel.invalidate();
  }

  handleInput(data: string): void {
    this.panel.handleInput(data);
  }
}
