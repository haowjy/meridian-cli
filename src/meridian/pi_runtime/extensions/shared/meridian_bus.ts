import { EventEmitter } from "node:events";

const bus = new EventEmitter();

export function emitMeridianEvent(
  channel: string,
  payload: Record<string, unknown>,
): void {
  bus.emit(channel, payload);
}

export function onMeridianEvent(
  channel: string,
  handler: (payload: Record<string, unknown>) => void,
): void {
  bus.on(channel, handler);
}

export function offMeridianEvent(
  channel: string,
  handler: (payload: Record<string, unknown>) => void,
): void {
  bus.off(channel, handler);
}
