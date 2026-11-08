#!/usr/bin/env bash
# crash-loop supervisor: restart bot within 3s when it dies.
# singleton guard: refuses to start if another acobot poller is already alive
# (two instances = telegram Conflict kills both, the "stuck" bug).
exec 9>/tmp/acobot.sup.lock
flock -n 9 || exit 0
cd "$(dirname "$0")"
while true; do
  others=$(pgrep -f "python3 -u -m acobot.bot" | grep -v "^$$\$" | wc -l)
  if [ "$others" -gt 0 ]; then
    echo "$(date -u +%FT%TZ) supervisor: $others duplicate poller(s) detected, killing them" >> bot.run.log
    pkill -9 -f "python3 -u -m acobot.bot" 2>/dev/null
    sleep 2
  fi
  python3 -u -m acobot.bot >> bot.run.log 2>&1
  echo "$(date -u +%FT%TZ) supervisor: bot exited, restarting in 3s" >> bot.run.log
  sleep 3
done
