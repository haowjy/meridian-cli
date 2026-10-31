import type { EngineInterface, Register } from 'claude-code'

// Delivers Meridian's starting prompt as the first user turn of an interactive
// launch. Meridian names the file in `_MERIDIAN_CLAUDE_FIRST_TURN_PATH` only for
// a fresh launch, and keeps the prompt off argv so `pkill -f <word>` elsewhere
// cannot match it and `ps` cannot show it.
//
// `session.start` fires again on `--resume` and after a reload, so the submit is
// also gated on the session having no turns yet.

async function submitFirstTurn($: EngineInterface): Promise<void> {
  const path = await $.env.get('_MERIDIAN_CLAUDE_FIRST_TURN_PATH')
  if (path === undefined || path === '') return
  if ((await $.session.turns()) > 0) return
  const text = await $.fs.read(path)
  if (text.trim() === '') return
  await $.prompt.submit({ text, asUser: true })
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    if (e.isInteractive === false || e.surface === null) return next(e)
    // Not awaited: the submit lands after the session has started (probe p10381).
    void submitFirstTurn($).catch(() => {})
    return next(e)
  })
}
