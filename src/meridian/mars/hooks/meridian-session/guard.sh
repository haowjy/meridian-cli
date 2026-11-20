# Run by agy's `sh -c` handler as: sh guard.sh <event> <agy pid>.
# Starts Meridian only when this conversation has no request file yet or has queued or
# unconfirmed messages, so a session with nothing to do pays for a shell, not for Python.
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
  birth=$(ps -o lstart= -p "$pid" 2>/dev/null | tr -c 'A-Za-z0-9\n' _)
fi
root=${MERIDIAN_HOME:-$HOME/.meridian}/agy
if [ -e "$root/$pid-$birth/requests/$c.json" ] \
  && [ ! -s "$root/inbox/$c.jsonl" ] && [ ! -s "$root/inflight/$c.jsonl" ]; then
  exit 0
fi
exec meridian harness-hook agy "$event" --agy-pid "$pid"
