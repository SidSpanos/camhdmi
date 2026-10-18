#!/bin/bash
# camhdmi installer - Raspberry Pi OS Lite (64-bit, Debian 13 "trixie")
#
# Run from the cloned repo as your normal user (it uses sudo where needed):
#   git clone https://github.com/SidSpanos/camhdmi.git && cd camhdmi && ./install.sh
#
# Sets the Pi up as an appliance: console autologin -> labwc -> camhdmi.
set -e
if [ "$(id -u)" -eq 0 ]; then echo "Run as your normal user, not root (sudo is used where needed)."; exit 1; fi
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== Installing packages (takes a while on a Pi 3)"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  v4l-utils gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-x gstreamer1.0-gtk4 \
  python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gtk-4.0 \
  python3-evdev python3-pyudev python3-cairo python3-numpy \
  labwc wlr-randr seatd wlrctl grim

echo "== Groups (camera, input devices, GPU)"
sudo usermod -aG video,input,render "$USER"

echo "== Console autologin"
sudo raspi-config nonint do_boot_behaviour B2

echo "== Start labwc on the local console"
if ! grep -q 'camhdmi: start the compositor' "$HOME/.bash_profile" 2>/dev/null; then
  cat >> "$HOME/.bash_profile" <<'EOF'
# camhdmi: start the compositor on the local console only
if [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
  exec labwc > /tmp/labwc.log 2>&1
fi
EOF
fi

echo "== labwc autostart"
mkdir -p "$HOME/.config/labwc"
echo "$DIR/run.sh &" > "$HOME/.config/labwc/autostart"
LAYOUT="$(sed -n 's/^XKBLAYOUT="\{0,1\}\([^",]*\).*/\1/p' /etc/default/keyboard 2>/dev/null)"
cat > "$HOME/.config/labwc/environment" <<EOF
XKB_DEFAULT_LAYOUT=${LAYOUT:-us}
GSK_RENDERER=ngl
GDK_DISABLE=vulkan
EOF

chmod +x "$DIR/run.sh" "$DIR/app.py"
echo
echo "Done. Reboot to start camhdmi:  sudo reboot"
echo "Phone panel afterwards:        http://$(hostname).local:8080/"
