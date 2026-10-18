# camhdmi

**Turn any USB webcam into an HDMI camera with a Raspberry Pi.**

Plug a UVC camera into a Pi, plug the Pi into a monitor, capture card or switcher, and you get a clean
HDMI feed. Control it from your phone, a cheap air-mouse remote, or a keyboard. camhdmi reads the
camera's own capabilities at runtime, so zoom, pan, tilt, exposure, focus and white balance appear
automatically for whatever camera you plug in.

Built for live AV work: the HDMI output stays a **clean program feed** — no menus, no on-screen text —
so it can go straight into a stream or vision mixer.

## Features

- **Any UVC camera.** Formats and controls are discovered on plug-in; the best mode the board can
  sustain is picked automatically (raw NV12/YUYV first, then MJPEG).
- **Clean HDMI out** at full frame rate — 1080p30 on a Pi 4 with a USB 3 camera, 720p30 on a Pi 3.
- **Phone control panel** at `http://<pi>.local:8080/`: live preview, LIVE/FROZEN tally, pan/tilt
  D-pad, zoom, mirror/flip, presets, and every image control the camera exposes. Add it to your home
  screen and it behaves like an app. Changes sync live between phone and remote.
- **Freeze frame.** Hold the HDMI picture while you reframe; the phone keeps showing the live camera.
- **Remote / keyboard control.** Arrows pan/tilt, PgUp/PgDn or Vol± zoom, OK opens the menu,
  play/pause freezes. The remote's power/sleep key is swallowed so it can't shut the Pi down mid-show.
- **Hardware zoom/pan when the camera has it**, digital crop (up to 2×) when it doesn't.
- **Mirror/flip in the Pi's ISP** — zero CPU cost.
- **Per-camera memory.** Settings and 9 presets saved per camera (vendor:product:serial) and restored
  on plug-in.
- **Appliance behaviour.** Boots straight into the camera, hot-plugs cameras and keyboards, follows the
  HDMI port if you swap it, and a watchdog rebuilds the pipeline if video ever stalls.
- **Optional touch panel** on the official Raspberry Pi 7" display, built from the same controls.

## Tested hardware

| Board | Camera | Result |
|---|---|---|
| Raspberry Pi 4 Model B | Logitech StreamCam (USB 3) | 1080p30 raw NV12, ~10% CPU |
| Raspberry Pi 3 Model B | Huddly IQ (on USB 2) | 720p30 MJPEG (software decode), ~30% CPU |

Should work on any Pi 3 / Pi 4 / Pi 400 / CM4 / Zero 2 W. **Pi 5 is not supported yet**: it has no
`v4l2convert` ISP or hardware JPEG, which this pipeline relies on (software fallbacks are on the roadmap).

## Install

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. Set a hostname (e.g. `camhdmi`),
   your user, Wi-Fi/Ethernet and SSH.
2. Plug in the camera (a **USB 3 / blue** port on a Pi 4) and HDMI, boot, SSH in.
3. Install:
   ```bash
   sudo apt install -y git
   git clone https://github.com/SidSpanos/camhdmi.git
   cd camhdmi && ./install.sh
   sudo reboot
   ```
4. The camera appears on HDMI. Open `http://camhdmi.local:8080/` on your phone.

## Controls

### Phone panel
Everything is on one page: tap a preset to recall, hold it to save. Sliders, switches and drop-downs are
generated from the camera — auto modes grey out the manual controls they override.

### Remote / keyboard
| Key | Action |
|---|---|
| Arrows | pan / tilt |
| PgUp / PgDn, Vol ±, keypad ± | zoom |
| Home | reset view |
| OK / Enter / Menu | settings menu (feedback on the phone panel; HDMI stays clean) |
| Play/Pause | freeze / live |
| M / F | mirror / flip |
| A / E / W | autofocus / exposure mode / auto white balance |
| R | reset camera to defaults |
| 1–9 / Shift+1–9 | recall / save preset |
| Power / Sleep | ignored (grabbed so the OS never sees it) |

Tested with Plexgear air-mouse remotes; any USB keyboard or HID remote works.

## Configuration

Optional `~/.config/camhdmi/config.json`:

```json
{
  "power_line_frequency": 1,
  "keyboard_layout": "auto",
  "web_port": 8080,
  "osd_on_program": false,
  "soft_zoom_max": 2.0
}
```

| Key | Meaning |
|---|---|
| `power_line_frequency` | anti-flicker for new cameras: `1` = 50 Hz, `2` = 60 Hz |
| `keyboard_layout` | `auto` reads the system layout; `se` maps `+`/`−` to Swedish key positions |
| `web_port` | phone panel port |
| `osd_on_program` | `true` draws status text on HDMI (handy for bench setup, never for a live feed) |
| `soft_zoom_max` | digital zoom limit for cameras without hardware zoom |

## How it works

```
                    ┌─ videocrop ─ v4l2convert (ISP: colour, scale, mirror) ─ waylandsink → HDMI
camera ─ v4l2src ─ tee
 (UVC)   [jpegdec]  ├─ v4l2convert 640×360 ─ v4l2jpegenc ─ appsink → MJPEG → phone panel
                    └─ (touch panel preview, only when a DSI screen is connected)
```

One Python process runs inside [labwc](https://labwc.github.io/):

| File | Role |
|---|---|
| `camera.py` | probes formats and controls with `v4l2-ctl`, picks the mode, maps generic roles (zoom/pan/tilt/focus/exposure) onto whatever the camera calls them |
| `video.py` | GStreamer pipeline, freeze, mirror, digital zoom, stall counter |
| `inputs.py` | evdev keyboards/remotes (grabbed exclusively), HDMI-CEC |
| `web.py` + `index.html` | phone panel: MJPEG preview, Server-Sent Events state, JSON actions |
| `panel.py` | GTK4 touch panel for the 7" DSI display |
| `app.py` | ties it together: hot-plug, watchdog, presets, key map, state |
| `run.sh` | launcher: finds the HDMI port, pins it to 1080p60, keeps the app alive |

## Troubleshooting

- **Picture stutters / flickers, `vcgencmd get_throttled` isn't `0x0`:** under-voltage. Hungry cameras
  (conference cameras especially) can brown out a Pi. Use the official supply for your board and put the
  camera on a **powered USB hub**.
- **Video freezes right after starting on a Pi 3 with an MJPEG camera:** some cameras' MJPEG hangs the
  Pi 3's hardware JPEG decoder. The watchdog detects it and switches to software decoding automatically.
- **HDMI comes up at 4K or the picture is a quarter size:** the launcher re-pins HDMI to 1080p60 within a
  few seconds of the display (or capture card) appearing.
- **Is video moving?** `python3 tools/live.py` prints a frame-difference score (`0.00` = frozen).
- **Logs:** `~/.cache/camhdmi.log`

## Lessons learned (for anyone hacking on it)

- Two `kmssink`s can't share the DRM device (external fd → `drmModeSetPlane` EACCES) — hence labwc.
- `waylandsink` must get its own `display=` connection; sharing GTK's Wayland connection in the same
  process freezes video after the first frame.
- `waylandsink` won't take NV12 from labwc: convert to BGRx in the ISP (`v4l2convert`, ~4% CPU at 1080p30).
- Mirror in the compositor also mirrors any overlay text — do it in the ISP before the overlay.
- The Pi 4 JPEG decoder tops out around 22–25 fps at 1080p; raw formats over USB 3 hold 30 fps.
- **Never power a display from the GPIO header and its own supply at the same time.** Two dead screens
  taught us that one.

## Roadmap

- Pi 5 support (software fallbacks for the ISP and JPEG blocks)
- WebRTC preview for sub-100 ms latency
- Optional PIN for the phone panel
- Multi-camera switching

## License

MIT © 2026 Sid Spanos
