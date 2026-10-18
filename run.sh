#!/bin/bash
# camhdmi launcher - started by labwc autostart.
# Keeps the app running and watches HDMI: whichever port is connected gets
# the labwc window rule and a 1080p60 mode. If HDMI moves port or comes up
# late (or at 4K), it re-pins and restarts the app so video goes fullscreen.
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/.cache/camhdmi.log"
mkdir -p "$HOME/.cache"
log() { echo "$(date '+%F %T') launcher: $*" >> "$LOG"; }

hdmi_port() {
  for s in /sys/class/drm/card*-HDMI-A-*/status; do
    grep -q '^connected' "$s" && { basename "$(dirname "$s")" | sed 's/^card[0-9]*-//'; return; }
  done
}

is_1080() { wlr-randr --output "$1" 2>/dev/null | grep current | grep -q '1920x1080'; }

setup_outputs() {  # $1 = HDMI port name
  sed "s/@HDMI@/$1/" "$DIR/rc.xml.in" > "$HOME/.config/labwc/rc.xml"
  kill -HUP "$(pgrep -x labwc)" 2>/dev/null
  sleep 1
  wlr-randr --output "$1" --mode 1920x1080@60 2>/dev/null \
    || wlr-randr --output "$1" --mode 1920x1080 2>/dev/null
  log "HDMI=$1 mode=$(wlr-randr --output "$1" 2>/dev/null | grep current | awk '{print $1}')"
}

# app loop in the background
(
  while true; do
    python3 "$DIR/app.py" >> "$LOG" 2>&1
    echo "$(date '+%F %T') app exited ($?), restarting" >> "$LOG"
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    sleep 2
  done
) &

# HDMI watcher
CUR=""; TRIED=""
while true; do
  P="$(hdmi_port)"
  if [ -n "$P" ]; then
    if [ "$P" != "$CUR" ] || { ! is_1080 "$P" && [ "$TRIED" != "$P" ]; }; then
      [ "$P" = "$CUR" ] && TRIED="$P"   # only one retry per port if 1080p won't stick
      setup_outputs "$P"
      # restart the app so the video window maps fullscreen on the right output
      pkill -f "^python3 $DIR/app.py"
      CUR="$P"
    fi
  else
    CUR=""; TRIED=""
  fi
  sleep 5
done
