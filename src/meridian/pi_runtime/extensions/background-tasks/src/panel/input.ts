import { getKeybindings, matchesKey, type KeybindingsManager } from "@earendil-works/pi-tui";

/**
 * Minimal Kitty/legacy printable decode — do not import pi-tui/dist/keys.js; Pi's
 * extension loader aliases @earendil-works/pi-tui to dist/index.js and breaks subpaths.
 */
function decodeKittyPrintable(data: string): string | undefined {
  const csiU = data.match(/^\x1b\[(\d{1,8})u$/);
  if (csiU) {
    const codepoint = Number.parseInt(csiU[1], 10);
    if (Number.isFinite(codepoint) && codepoint >= 32 && codepoint < 0x110000) {
      return String.fromCodePoint(codepoint);
    }
  }
  return undefined;
}

/** Resolve a single printable character from raw terminal input (Kitty or legacy). */
export function printableChar(data: string): string | undefined {
  const kitty = decodeKittyPrintable(data);
  if (kitty != null && kitty.length > 0) {
    return kitty;
  }
  if (data.length === 1 && data.charCodeAt(0) >= 32) {
    return data;
  }
  return undefined;
}

function kb(): KeybindingsManager {
  return getKeybindings();
}

export function isPanelQuit(data: string): boolean {
  if (matchesKey(data, "escape")) {
    return true;
  }
  if (kb().matches(data, "tui.select.cancel")) {
    return true;
  }
  if (matchesKey(data, "q")) {
    return true;
  }
  const ch = printableChar(data);
  return ch === "q" || ch === "Q";
}

export function isPanelConfirm(data: string): boolean {
  if (matchesKey(data, "return")) {
    return true;
  }
  if (kb().matches(data, "tui.select.confirm")) {
    return true;
  }
  return data === "\n" || data === "\r";
}

export function isPanelUp(data: string): boolean {
  if (matchesKey(data, "up")) {
    return true;
  }
  if (kb().matches(data, "tui.select.up")) {
    return true;
  }
  return printableChar(data) === "k";
}

export function isPanelDown(data: string): boolean {
  if (matchesKey(data, "down")) {
    return true;
  }
  if (kb().matches(data, "tui.select.down")) {
    return true;
  }
  return printableChar(data) === "j";
}

export function isPanelLogScrollUp(data: string): boolean {
  return printableChar(data) === "J";
}

export function isPanelLogScrollDown(data: string): boolean {
  return printableChar(data) === "K";
}

export function isPanelKill(data: string): boolean {
  const ch = printableChar(data);
  return ch === "x" || ch === "X";
}

export function isPanelClear(data: string): boolean {
  const ch = printableChar(data);
  return ch === "c" || ch === "C";
}

export function isPanelBackground(data: string): boolean {
  const ch = printableChar(data);
  return ch === "b" || ch === "B";
}
