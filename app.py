#!/usr/bin/env python3
"""camhdmi - any USB camera to HDMI, with auto-discovered controls.

Runs inside labwc. One process:
  * GStreamer video to HDMI (waylandsink, window placed by labwc rule)
  * GTK4 touch panel on the DSI screen (app_id se.camhdmi.panel)
  * evdev keyboard / remote / CEC input with an on-screen display on HDMI
  * udev hotplug for cameras and keyboards
"""
import json
import re
import os
import sys
import time

os.environ.setdefault("GSK_RENDERER", "ngl")  # skip GTK's Vulkan probe on the Pi

import gi
gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gst, Gtk  # noqa: E402

GLib.set_prgname("camhdmi-video")  # waylandsink app_id -> labwc rule puts it on HDMI
Gst.init(None)

import pyudev  # noqa: E402
from evdev import ecodes as E  # noqa: E402

import camera  # noqa: E402
from inputs import Inputs  # noqa: E402
from video import Video, esc  # noqa: E402
import web  # noqa: E402

CONF_DIR = os.path.expanduser("~/.config/camhdmi")
CONFIG = {
    "power_line_frequency": 1,   # 50 Hz mains (Sweden); applied to new cameras
    "keyboard_layout": "auto",   # auto = system layout; "se" maps + and - to the Swedish keys
    "soft_zoom_max": 2.0,
    "osd_seconds": 2.5,
    "menu_timeout": 20,
    "web_port": 8080,            # phone panel: http://<pi>:8080/
    "osd_on_program": False,     # HDMI goes to the stream: no overlay on it by default
}


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


CONFIG.update(load_json(os.path.join(CONF_DIR, "config.json"), {}))


def keyboard_layout():
    """Configured layout, or the system's (XKBLAYOUT in /etc/default/keyboard)."""
    kl = CONFIG.get("keyboard_layout", "auto")
    if kl != "auto":
        return kl
    try:
        for line in open("/etc/default/keyboard"):
            if line.startswith("XKBLAYOUT="):
                return line.split("=", 1)[1].strip().strip('"').split(",")[0] or "us"
    except OSError:
        pass
    return "us"


def dsi_connected():
    import glob
    for s in glob.glob("/sys/class/drm/card*-DSI-*/status"):
        try:
            if open(s).read().strip() == "connected":
                return True
        except OSError:
            pass
    return False


class CamApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="se.camhdmi.panel")
        self.cam = None
        self.video = Video(self.on_video_error)
        self.video.osd_enabled = bool(CONFIG.get("osd_on_program", False))
        self.panel = None
        self.mirror = self.flip = False
        self.soft = [1.0, 0.0, 0.0]          # zoom, ox, oy (fallback when no hw zoom)
        self.state = {}
        self.menu_open = False
        self.menu_idx = 0
        self.menu_last = 0
        self.osd_timer = 0
        self.save_timer = 0
        self.rescan_timer = 0
        self.last_error = 0
        self.inputs = None
        self.last_msg, self.last_msg_t, self.menu_line = "", 0.0, ""

    # ================= startup =================
    def do_activate(self):
        import panel
        self.hold()
        self.panel = panel.Panel(self)
        # only show the touch panel when a DSI screen is actually connected;
        # otherwise labwc would drop it onto HDMI on top of the video
        if dsi_connected():
            self.panel.present()
        else:
            print("panel: no DSI screen connected, running headless", flush=True)
        self.inputs = Inputs(self.on_key)
        try:
            self.web = web.Web(self, int(CONFIG.get("web_port", 8080)))
            self.video.on_jpeg = self.web.push_jpeg
        except OSError as e:
            self.web = None
            print("web: disabled:", e, flush=True)
        ctx = pyudev.Context()
        self.mon = pyudev.Monitor.from_netlink(ctx)
        self.mon.filter_by("video4linux")
        self.mon.filter_by("input")
        self.mon.start()
        GLib.io_add_watch(self.mon.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, self.on_udev)
        GLib.timeout_add_seconds(1, self.connect_camera)
        self.wd_frames = -1
        GLib.timeout_add(400, self.monitor_update)
        GLib.timeout_add_seconds(4, self.watchdog)

    def web_state(self):
        """Everything the phone panel needs, rebuilt every monitor tick."""
        st = {"camera": None}
        if self.cam and self.video.mode:
            m = self.video.mode
            view = {self.cam.role(r).name for r in ("zoom", "pan", "tilt") if self.cam.role(r)}
            z = self.cam.role("zoom")
            if z and z.min:
                zl = f"Zoom {z.value / z.min:.1f}×"
            elif z:
                zl = f"Zoom {z.value}"
            else:
                zl = f"Zoom {self.soft[0]:.1f}× (digital)"
            st = {"camera": self.cam.name,
                  "mode": f"{m['w']}×{m['h']} {m['fourcc']} {m['fps']}fps",
                  "zoom": zl,
                  "controls": [{"name": c.name, "label": c.label, "kind": c.kind, "min": c.min,
                                "max": c.max, "step": c.step, "value": c.value, "default": c.default,
                                "inactive": c.inactive, "menu": {str(k): v for k, v in c.menu.items()}}
                               for c in self.cam.controls.values() if c.name not in view]}
        recent = time.time() - self.last_msg_t < CONFIG["osd_seconds"]
        st.update(frozen=bool(self.video.frozen), mirror=self.mirror, flip=self.flip,
                  presets=sorted(self.state.get("presets", {}).keys()),
                  msg=re.sub(r"<[^>]+>", "", self.menu_line if self.menu_open else self.last_msg.split("\n")[0])
                  if (recent or self.menu_open) else "")
        return st

    def web_action(self, p):
        """Phone actions -> the same handlers the remote uses (main loop)."""
        a = p.get("a")
        if a == "set":
            self.set_control(str(p["name"]), int(p["value"]))
        elif a == "zoom":
            self.zoom(1 if int(p.get("d", 1)) > 0 else -1)
        elif a == "pan":
            self.pan(int(p.get("dx", 0)), int(p.get("dy", 0)))
        elif a == "center":
            self.center()
        elif a == "mirror":
            self.toggle_mirror()
        elif a == "flip":
            self.toggle_flip()
        elif a == "freeze":
            self.toggle_freeze()
        elif a == "preset":
            self.preset(int(p["n"]), bool(p.get("save")))
        elif a == "reset":
            self.reset_all()
        else:
            raise ValueError(f"unknown action {a!r}")
        GLib.timeout_add(150, lambda: self.monitor_update() and False)
        return True

    def monitor_update(self):
        """Two-line status bar on the small monitor screen."""
        if self.cam and self.video.mode:
            m = self.video.mode
            if self.video.frozen:
                st = '<span foreground="#ff4040">❚❚ FROZEN</span>'
            else:
                st = '<span foreground="#40ff60">● LIVE</span>'
            st += f"  {m['h']}p{m['fps']}"
            z = self.cam.role("zoom")
            if z and z.min:
                st += f"  Z{z.value / z.min:.1f}×"
            elif self.soft[0] > 1:
                st += f"  Z{self.soft[0]:.1f}×"
            if self.mirror:
                st += "  MIR"
            if self.flip:
                st += "  FLIP"
        else:
            st = "No camera"
        if self.menu_open:
            info = self.menu_line
        elif time.time() - self.last_msg_t < CONFIG["osd_seconds"] + 1:
            info = self.last_msg.split("\n")[0]
        else:
            info = "OK menu · ▶‖ freeze · ⌂ reset"
        text = st + "\n" + info
        if text != self.video.mon_text:
            self.video.monitor(text)
        if getattr(self, "web", None):
            self.web.publish(json.dumps(self.web_state()))
        return True

    def watchdog(self):
        """Rebuild the pipeline if HDMI frames stop arriving while a camera is up."""
        f = self.video.frames
        if self.cam and self.video.mode and f == self.wd_frames:
            print(time.strftime("%F %T"), "watchdog: video stalled, restarting pipeline", flush=True)
            if self.video.mode.get("fourcc") == "MJPG" and self.video.jpeg_hw:
                self.video.jpeg_hw = False
                print(time.strftime("%F %T"), "watchdog: hardware JPEG decode gave no frames, "
                      "switching to software decode", flush=True)
            self.cam = None
            self.connect_camera()
        self.wd_frames = self.video.frames
        return True

    def on_udev(self, fd, cond):
        while True:
            d = self.mon.poll(timeout=0)
            if d is None:
                break
            if d.subsystem == "input":
                GLib.timeout_add(800, self.inputs.scan)
            else:
                if self.rescan_timer:
                    GLib.source_remove(self.rescan_timer)
                self.rescan_timer = GLib.timeout_add(1200, self.connect_camera)
        return True

    # ================= camera lifecycle =================
    def connect_camera(self):
        self.rescan_timer = 0
        cams = camera.find_cameras()
        if self.cam and any(c[0] == self.cam.dev and c[2] == self.cam.id for c in cams):
            return False  # current camera still there
        self.save_now()
        if not cams:
            self.cam = None
            self.video.standby("No camera\nPlug in a USB camera")
            self.panel.rebuild(None)
            return False
        dev, name, cid = cams[0]
        try:
            cam = camera.Camera(dev, name, cid)
        except Exception as e:
            print("probe failed:", e, flush=True)
            GLib.timeout_add_seconds(3, self.connect_camera)
            return False
        if not cam.mode:
            self.cam = None
            self.video.standby(f"{name}\nno usable video mode")
            self.panel.rebuild(None)
            return False
        self.cam = cam
        self.state = load_json(self.state_path(), {})
        if not self.state.get("controls"):
            pl = cam.role("powerline")
            if pl and CONFIG.get("power_line_frequency") in pl.menu:
                cam.set(pl.name, CONFIG["power_line_frequency"])
        else:
            cam.apply(self.state["controls"])
        self.mirror = bool(self.state.get("mirror", False))
        self.flip = bool(self.state.get("flip", False))
        self.soft = list(self.state.get("soft", [1.0, 0.0, 0.0]))
        self.video.mirror, self.video.flip = self.mirror, self.flip
        print(f"camera: {name} {dev} mode={cam.mode}", flush=True)
        self.video.start(cam)
        self.video.set_crop(*self.soft)
        self.panel.rebuild(cam)
        m = cam.mode
        self.flash(f"<b>{esc(name)}</b>\n{m['w']}×{m['h']} {m['fourcc']} {m['fps']} fps", 4)
        return False

    def on_video_error(self, message):
        now = time.time()
        if now - self.last_error < 3:
            return False
        self.last_error = now
        self.cam = None  # force a fresh probe
        GLib.timeout_add_seconds(2, self.connect_camera)
        return False

    def state_path(self):
        return os.path.join(CONF_DIR, f"{self.cam.id}.json")

    def save_soon(self):
        if self.save_timer:
            GLib.source_remove(self.save_timer)
        self.save_timer = GLib.timeout_add_seconds(2, self.save_now)

    def save_now(self):
        self.save_timer = 0
        if self.cam:
            self.state["controls"] = self.cam.snapshot()
            self.state["mirror"] = self.mirror
            self.state["flip"] = self.flip
            self.state["soft"] = self.soft
            save_json(self.state_path(), self.state)
        return False

    # ================= actions (shared by keys, menu and panel) =================
    def changed(self, rebuild_panel=False):
        self.save_soon()
        if self.panel:
            self.panel.sync(rebuild_panel)

    def set_control(self, name, value):
        if not self.cam:
            return
        c = self.cam.controls.get(name)
        before = {n: x.inactive for n, x in self.cam.controls.items()}
        if self.cam.set(name, value):
            after = {n: x.inactive for n, x in self.cam.controls.items()}
            self.changed(before != after)

    def zoom(self, d):
        if not self.cam:
            return
        z = self.cam.role("zoom")
        if z:
            v = self.cam.nudge(z.name, d)
            self.flash(f"Zoom {v / z.min:.1f}×" if z.min > 0 else f"Zoom {v}")
        else:
            zmax = float(CONFIG["soft_zoom_max"])
            self.soft[0] = round(min(zmax, max(1.0, self.soft[0] + 0.1 * d)), 2)
            if self.soft[0] == 1.0:
                self.soft[1] = self.soft[2] = 0.0
            self.video.set_crop(*self.soft)
            self.flash(f"Zoom {self.soft[0]:.1f}× (digital)")
        self.changed()

    def pan(self, dx, dy):
        """dx/dy are screen directions: +x right, +y down."""
        if not self.cam:
            return
        if self.mirror:
            dx = -dx
        if self.flip:
            dy = -dy
        p, t = self.cam.role("pan"), self.cam.role("tilt")
        hw = (dx and p) or (dy and t)
        if hw:
            if dx and p:
                self.cam.nudge(p.name, dx)
            if dy and t:
                self.cam.nudge(t.name, -dy)  # UVC tilt: positive = up
            pv = p.value // p.step if p else 0
            tv = t.value // t.step if t else 0
            self.flash(f"Pan {pv:+d}   Tilt {tv:+d}")
        elif self.soft[0] > 1.0:
            self.soft[1] = max(-1.0, min(1.0, self.soft[1] + 0.1 * dx))
            self.soft[2] = max(-1.0, min(1.0, self.soft[2] + 0.1 * dy))
            self.video.set_crop(*self.soft)
            self.flash(f"Pan {self.soft[1]:+.1f}   Tilt {self.soft[2]:+.1f} (digital)")
        else:
            self.flash("Zoom in first to pan" if not (p or t) else "Pan/tilt limit")
        self.changed()

    def center(self):
        if not self.cam:
            return
        for r in ("pan", "tilt", "zoom"):
            c = self.cam.role(r)
            if c:
                self.cam.set(c.name, c.default)
        self.soft = [1.0, 0.0, 0.0]
        self.video.set_crop(*self.soft)
        self.flash("View reset")
        self.changed()

    def toggle_mirror(self):
        self.mirror = not self.mirror
        self.video.set_orientation(self.mirror, self.flip)
        self.flash("Mirror " + ("on" if self.mirror else "off"))
        self.changed()

    def toggle_flip(self):
        self.flip = not self.flip
        self.video.set_orientation(self.mirror, self.flip)
        self.flash("Flip " + ("on" if self.flip else "off"))
        self.changed()

    def toggle_role(self, role, label):
        if not self.cam:
            return
        c = self.cam.role(role)
        if not c:
            self.flash(f"{label}: not supported by this camera")
            return
        if c.kind == "bool":
            self.set_control(c.name, 0 if c.value else 1)
            self.flash(f"{label} " + ("on" if self.cam.controls[c.name].value else "off"))
        else:  # menu (auto_exposure): cycle
            self.cam.nudge(c.name, 1)
            self.changed(True)
            c = self.cam.controls[c.name]
            self.flash(f"{label}: {c.menu.get(c.value, c.value)}")

    def reset_all(self):
        if not self.cam:
            return
        self.cam.reset_defaults()
        pl = self.cam.role("powerline")
        if pl and CONFIG.get("power_line_frequency") in pl.menu:
            self.cam.set(pl.name, CONFIG["power_line_frequency"])
        self.mirror = self.flip = False
        self.soft = [1.0, 0.0, 0.0]
        self.video.set_orientation(False, False)
        self.video.set_crop(*self.soft)
        self.flash("Camera reset to defaults")
        self.changed(True)

    def preset(self, n, save):
        if not self.cam:
            return
        presets = self.state.setdefault("presets", {})
        if save:
            presets[str(n)] = {"controls": self.cam.snapshot(), "mirror": self.mirror,
                               "flip": self.flip, "soft": list(self.soft)}
            self.save_now()
            self.flash(f"Preset {n} saved")
        elif str(n) in presets:
            p = presets[str(n)]
            self.cam.apply(p["controls"])
            self.mirror, self.flip = p.get("mirror", False), p.get("flip", False)
            self.soft = list(p.get("soft", [1.0, 0.0, 0.0]))
            self.video.set_orientation(self.mirror, self.flip)
            self.video.set_crop(*self.soft)
            self.flash(f"Preset {n}")
            self.changed(True)
        else:
            self.flash(f"Preset {n} is empty (Shift+{n} to save)")
        if self.panel:
            self.panel.sync_presets()

    def toggle_freeze(self):
        """Freeze the HDMI picture (clean, no OSD) while the camera is moved."""
        if not self.video.frozen:
            self.close_menu() if self.menu_open else None
            self.video.osd(None)
            # let one clean frame through before holding it
            GLib.timeout_add(120, lambda: setattr(self.video, "frozen", True) or False)
            print(time.strftime("%F %T"), "freeze on", flush=True)
        else:
            self.video.frozen = False
            print(time.strftime("%F %T"), "freeze off", flush=True)

    # ================= OSD =================
    def flash(self, markup, seconds=None):
        self.last_msg, self.last_msg_t = markup, time.time()
        if self.menu_open:
            self.draw_menu()
            return
        self.video.osd(markup)
        if self.osd_timer:
            GLib.source_remove(self.osd_timer)
        self.osd_timer = GLib.timeout_add(int((seconds or CONFIG["osd_seconds"]) * 1000), self._osd_clear)

    def _osd_clear(self):
        self.osd_timer = 0
        if not self.menu_open:
            self.video.osd(None)
        return False

    HELP = ("<b>camhdmi keys</b>\n"
            "Arrows  pan / tilt      + −  zoom\n"
            "Home  reset view        M  mirror   F  flip\n"
            "A  autofocus   E  exposure mode   W  white balance\n"
            "1–9  recall preset      Shift+1–9  save preset\n"
            "▶‖  freeze / live\n"
            "Enter  menu   R  reset camera   H  this help")

    # ---------- menu ----------
    def menu_items(self):
        items = []
        if self.cam:
            for c in self.cam.controls.values():
                if not c.inactive:
                    items.append(("ctrl", c.name))
        items += [("mirror", None), ("flip", None), ("reset", None), ("close", None)]
        return items

    def draw_menu(self):
        items = self.menu_items()
        self.menu_idx = max(0, min(self.menu_idx, len(items) - 1))
        # one slim line: only the selected setting, so the picture stays visible
        for i in (self.menu_idx,):
            kind, name = items[i]
            if kind == "ctrl":
                c = self.cam.controls[name]
                if c.kind == "bool":
                    val = "on" if c.value else "off"
                elif c.kind in ("menu", "intmenu"):
                    val = c.menu.get(c.value, c.value)
                else:
                    val = f"{c.value}  ({c.min}–{c.max})"
                text = f"{esc(c.label)}: {esc(val)}"
            elif kind == "mirror":
                text = "Mirror: " + ("on" if self.mirror else "off")
            elif kind == "flip":
                text = "Flip: " + ("on" if self.flip else "off")
            elif kind == "reset":
                text = "Reset camera to defaults"
            else:
                text = "Close menu"
        self.menu_line = f"◀ <b>{text}</b> ▶  {self.menu_idx + 1}/{len(items)}"
        self.monitor_update()
        self.video.osd(f"◀  <b>{text}</b>  ▶      {self.menu_idx + 1}/{len(items)}   "
                       "▲▼ setting · ◀▶ adjust · ≡ close")

    def menu_key(self, code):
        items = self.menu_items()
        self.menu_last = time.time()
        kind, name = items[min(self.menu_idx, len(items) - 1)]
        if code in (E.KEY_UP, E.KEY_KP8):
            self.menu_idx = (self.menu_idx - 1) % len(items)
        elif code in (E.KEY_DOWN, E.KEY_KP2):
            self.menu_idx = (self.menu_idx + 1) % len(items)
        elif code in (E.KEY_LEFT, E.KEY_RIGHT, E.KEY_KP4, E.KEY_KP6):
            d = 1 if code in (E.KEY_RIGHT, E.KEY_KP6) else -1
            if kind == "ctrl":
                before = {n: x.inactive for n, x in self.cam.controls.items()}
                self.cam.nudge(name, d)
                after = {n: x.inactive for n, x in self.cam.controls.items()}
                self.changed(before != after)
            elif kind == "mirror":
                self.toggle_mirror()
            elif kind == "flip":
                self.toggle_flip()
        elif code in (E.KEY_ENTER, E.KEY_KPENTER, E.KEY_OK, E.KEY_SELECT, E.KEY_SPACE):
            if kind == "reset":
                self.reset_all()
            elif kind == "close":
                return self.close_menu()
            elif kind == "mirror":
                self.toggle_mirror()
            elif kind == "flip":
                self.toggle_flip()
            elif kind == "ctrl" and self.cam.controls[name].kind in ("bool", "menu", "intmenu"):
                self.cam.nudge(name, 1)
                self.changed(True)
        elif code in (E.KEY_ESC, E.KEY_BACK, E.KEY_EXIT, E.KEY_BACKSPACE, E.KEY_H,
                      E.KEY_COMPOSE, E.KEY_MENU, E.KEY_HOMEPAGE, E.KEY_HOME):  # Plexgear ≡ / Home
            return self.close_menu()
        self.draw_menu()

    def open_menu(self):
        self.menu_open = True
        self.menu_last = time.time()
        self.draw_menu()
        GLib.timeout_add_seconds(2, self._menu_watchdog)

    def close_menu(self):
        self.menu_open = False
        self.last_msg_t = 0.0
        self.video.osd(None)

    def _menu_watchdog(self):
        if not self.menu_open:
            return False
        if time.time() - self.menu_last > CONFIG["menu_timeout"]:
            self.close_menu()
            return False
        return True

    # ================= keys =================
    def on_key(self, code, value, shift):
        if self.menu_open:
            self.menu_key(code)
            return
        se = keyboard_layout() == "se"
        zoom_in = {E.KEY_PAGEUP, E.KEY_VOLUMEUP, E.KEY_KPPLUS, E.KEY_CHANNELUP, E.KEY_ZOOMIN, E.KEY_Z}
        zoom_out = {E.KEY_PAGEDOWN, E.KEY_VOLUMEDOWN, E.KEY_KPMINUS, E.KEY_CHANNELDOWN, E.KEY_ZOOMOUT, E.KEY_X}
        if se:
            zoom_in.add(E.KEY_MINUS)    # Swedish '+'
            zoom_out.add(E.KEY_SLASH)   # Swedish '-'
        else:
            zoom_in.add(E.KEY_EQUAL)
            zoom_out.add(E.KEY_MINUS)
        repeatable = zoom_in | zoom_out | {E.KEY_UP, E.KEY_DOWN, E.KEY_LEFT, E.KEY_RIGHT}
        if value == 2 and code not in repeatable:
            return
        digits = {getattr(E, f"KEY_{i}"): i for i in range(1, 10)}
        digits.update({getattr(E, f"KEY_KP{i}"): i for i in range(1, 10)})
        digits.update({getattr(E, f"KEY_NUMERIC_{i}"): i for i in range(1, 10)})

        if code in zoom_in:
            self.zoom(1)
        elif code in zoom_out:
            self.zoom(-1)
        elif code == E.KEY_UP:
            self.pan(0, -1)
        elif code == E.KEY_DOWN:
            self.pan(0, 1)
        elif code == E.KEY_LEFT:
            self.pan(-1, 0)
        elif code == E.KEY_RIGHT:
            self.pan(1, 0)
        elif code in (E.KEY_HOME, E.KEY_HOMEPAGE, E.KEY_C):
            self.center()
        elif code == E.KEY_M:
            self.toggle_mirror()
        elif code == E.KEY_F:
            self.toggle_flip()
        elif code == E.KEY_A:
            self.toggle_role("focus_auto", "Autofocus")
        elif code == E.KEY_E:
            self.toggle_role("exposure_auto", "Exposure")
        elif code == E.KEY_W:
            self.toggle_role("wb_auto", "Auto white balance")
        elif code == E.KEY_R:
            self.reset_all()
        elif code in (E.KEY_H, E.KEY_F1, E.KEY_HELP, E.KEY_INFO):
            self.flash(self.HELP, 8)
        elif code in (E.KEY_ENTER, E.KEY_KPENTER, E.KEY_OK, E.KEY_SELECT, E.KEY_MENU, E.KEY_COMPOSE):
            self.open_menu()
        elif code in (E.KEY_PLAYPAUSE, E.KEY_PLAY, E.KEY_PAUSE, E.KEY_PLAYCD, E.KEY_PAUSECD):
            self.toggle_freeze()
        elif code in (E.KEY_POWER, E.KEY_SLEEP, E.KEY_WAKEUP, E.KEY_POWER2):
            pass  # grabbed so the OS never shuts down on a stray press
        elif code in digits:
            self.preset(digits[code], shift)


if __name__ == "__main__":
    app = CamApp()
    sys.exit(app.run(None))
