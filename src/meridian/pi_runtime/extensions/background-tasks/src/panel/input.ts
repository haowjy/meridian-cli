import { getKeybindings, type KeybindingsManager } from "@earendil-works/pi-tui";
import { decodePrintableKey, matchesKey } from "@earendil-works/pi-tui/dist/keys.js";

/** Resolve a single printable character from raw terminal input (Kitty or legacy). */
export function printableChar(data: string): string | undefined {
  const decoded = decodePrintableKey(data);
  if (decoded != null && decoded.length > 0) {
    return decoded;
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
  const ch = printableChar(data);
  return ch === "J";
}

export function isPanelLogScrollDown(data: string): boolean {
  const ch = printableChar(data);
  return ch === "K";
}

export function isPanelKill(data: string): boolean {
  const ch = printableChar(data);
  return ch === "x" || ch === "X";
}

export function isPanelClear(data: string): boolean {
  const ch = printableChar(data);
  return ch === "c" || ch === "C";
}
