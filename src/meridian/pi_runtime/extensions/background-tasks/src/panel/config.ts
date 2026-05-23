/** Local defaults mirroring pi-processes panel layout (no external settings package). */
export const panelConfig = {
  processList: {
    maxVisibleProcesses: 8,
    maxPreviewLines: 12,
  },
  widget: {
    showStatusWidget: true,
    dockDefaultState: "hidden" as "hidden" | "collapsed",
    dockHeight: 8,
  },
  follow: {
    enabledByDefault: false,
    autoHideOnFinish: true,
  },
};
