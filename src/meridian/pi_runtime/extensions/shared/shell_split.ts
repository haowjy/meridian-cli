const SHELL_RESERVED_WORDS = new Set([
  "!",
  "[[",
  "]]",
  "case",
  "coproc",
  "do",
  "done",
  "elif",
  "else",
  "esac",
  "fi",
  "for",
  "function",
  "if",
  "in",
  "select",
  "then",
  "time",
  "until",
  "while",
]);

export type SplitResult = { commands: string[]; complete: boolean };
type ShellWord = { value: string; quoted: boolean };

function isAssignment(word: ShellWord): boolean {
  return /^[A-Za-z_][A-Za-z0-9_]*=/u.test(word.value);
}

function splitShellInto(source: string, commands: string[]): boolean {
  let current = "";
  let quote: "'" | '"' | null = null;
  let word = "";
  let wordStarted = false;
  let wordQuoted = false;
  let words: ShellWord[] = [];
  let commandRequired = false;
  const finishWord = (): void => {
    if (!wordStarted) return;
    words.push({ value: word, quoted: wordQuoted });
    word = "";
    wordStarted = false;
    wordQuoted = false;
  };
  const flush = (): boolean => {
    finishWord();
    const command = current.trim();
    if (!command) return false;
    const executable = words.find((candidate) => !isAssignment(candidate));
    if (executable !== undefined
      && !executable.quoted
      && SHELL_RESERVED_WORDS.has(executable.value)) {
      return false;
    }
    commands.push(command);
    current = "";
    words = [];
    commandRequired = false;
    return true;
  };

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (quote === "'") {
      current += character;
      if (character === "'") quote = null;
      else word += character;
      continue;
    }
    if (quote === '"') {
      current += character;
      if (character === '"') {
        quote = null;
      } else if (character === "$" || character === "`" || character === "\\") {
        return false;
      } else {
        word += character;
      }
      continue;
    }
    if (character === "'" || character === '"') {
      quote = character;
      current += character;
      wordStarted = true;
      wordQuoted = true;
      commandRequired = false;
      continue;
    }
    if ("\\$`()<>#{}`".includes(character)) return false;
    if (character === "\r") return false;
    if (character === " " || character === "\t") {
      finishWord();
      current += character;
      continue;
    }
    if (character === "\n") {
      if (current.trim() && !flush()) return false;
      continue;
    }
    if (character === ";") {
      if (!flush()) return false;
      continue;
    }
    if (character === "|" || character === "&") {
      const doubled = source[index + 1] === character;
      if (character === "&" && !doubled) return false;
      if (!flush()) return false;
      if (doubled) index += 1;
      commandRequired = true;
      continue;
    }
    current += character;
    word += character;
    wordStarted = true;
    commandRequired = false;
  }
  if (quote !== null || commandRequired) return false;
  if (current.trim()) return flush();
  return commands.length > 0;
}

/** Split a bash command into simple commands; `complete` is false for any syntax it cannot read
 * (expansion, redirection, subshells, backgrounding). */
export function splitShellCommands(source: string): SplitResult {
  const commands: string[] = [];
  return { commands, complete: splitShellInto(source, commands) };
}
