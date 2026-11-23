# Run by agy's `sh -c` handler as: sh guard.sh <event> <agy pid>.
# On every PreInvocation, before anything else: creates busy/<conversation> (a turn is
# running; the Stop hook removes it) and removes wake/<conversation> (a model call
# satisfies a pending wake request).
# Then starts Meridian for Stop always, and for the other events only when this
# conversation has no request file yet, has queued or unconfirmed messages, has a row the
# hook has not yet briefed it on, or whose transcript shows a compaction since the hook
# last looked (the seen file: "<offset> <transcript>"; grep stops at the first hit) or is
# shorter than that offset, so a session with nothing to do pays for a shell, not for
# Python. Meridian's exit status is dropped: a plugin newer than the meridian on PATH
# must not make agy see a failed hook.
# Paths mirror lib/harness/agy/state.py and inbox.py.
command -v meridian >/dev/null 2>&1 || exit 0
event=$1
pid=$2
c=$ANTIGRAVITY_CONVERSATION_ID
[ -n "$c" ] && [ -n "$pid" ] || exit 0
if [ -r "/proc/$pid/stat" ]; then
  s=$(cat "/proc/$pid/stat")
  s=${s##*) }
  set -- $s
  birth=${20}
else
  # Trim before mapping, as state.py does: ps pads the start time on some systems.
  birth=$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr -c 'A-Za-z0-9\n' _)
fi
[ -n "$birth" ] || exit 0
root=${MERIDIAN_HOME:-$HOME/.meridian}/agy
p=$root/$pid-$birth
if [ "$event" = PreInvocation ]; then
  mkdir -p "$p/busy" 2>/dev/null && : > "$p/busy/$c"
  [ ! -e "$p/wake/$c" ] || rm -f "$p/wake/$c"
fi
if [ "$event" != Stop ] && [ -e "$p/requests/$c.json" ] \
  && [ ! -s "$root/inbox/$c.jsonl" ] && [ ! -s "$root/inflight/$c.jsonl" ] \
  && { [ ! -e "$p/rows/$c.json" ] || [ -e "$p/briefed/$c" ]; }; then
  off= file=
  [ -r "$p/seen/$c" ] && read -r off file < "$p/seen/$c"
  if [ -z "$file" ]; then
    exit 0
  fi
  size=$({ wc -c < "$file"; } 2>/dev/null)
  if ! { [ -n "$size" ] && [ "$size" -lt "$off" ]; } \
    && ! tail -c "+$((off + 1))" "$file" 2>/dev/null | grep -qF '"type":"CHECKPOINT"'; then
    exit 0
  fi
fi
meridian harness-hook agy "$event" --agy-pid "$pid"
exit 0
