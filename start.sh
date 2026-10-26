#!/bin/zsh
# Start Claude Session Deck (if it isn't running) and open it in the browser.
DIR=${0:A:h}
[ -f "$DIR/config.local.sh" ] && source "$DIR/config.local.sh"   # optional, untracked: export CSD_CLAUDE_ARGS / PORT
export PORT=${PORT:-7337}
if ! curl -s -o /dev/null "http://127.0.0.1:$PORT/api/sessions"; then
  nohup python3 "$DIR/server.py" > "$DIR/server.log" 2>&1 &
  sleep 0.7
fi
open "http://127.0.0.1:$PORT"
