#!/usr/bin/env bash
# watchdog: keep the bot supervisor alive after reboots
if ! pgrep -f "bash .*acobot/sup.sh" > /dev/null; then
  nohup_bash() { :; }
  bash /w/hosts/0/u/6ada39caa6b246a89f25d6c9/home/.folk/acobot/sup.sh &> /dev/null &
fi
