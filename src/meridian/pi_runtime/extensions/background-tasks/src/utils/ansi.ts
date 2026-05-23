const ESC = String.fromCodePoint(0x001b);
const BEL = String.fromCodePoint(0x0007);

const ANSI_REPLACEMENTS: RegExp[] = [
  new RegExp(`${ESC}\\[[0-9;]*[A-Za-z]`, "gu"),
  new RegExp(`${ESC}\\][^${BEL}${ESC}]*(?:${BEL}|${ESC}\\\\)`, "gu"),
  new RegExp(`${ESC}_[^${BEL}${ESC}]*(?:${BEL}|${ESC}\\\\)`, "gu"),
];

// biome-ignore lint/suspicious/noControlCharactersInRegex: intentional control-char strip
const TERMINAL_CONTROL_CHARS = /[\u0000-\u0008\u000b-\u001f\u007f]/gu;

export function stripAnsi(value: string): string {
  let result = value;
  for (const pattern of ANSI_REPLACEMENTS) {
    result = result.replace(pattern, "");
  }
  return result.replace(TERMINAL_CONTROL_CHARS, "");
}
