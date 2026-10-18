"""camera.py - generic UVC camera probing and control for camhdmi.

Everything here is camera-agnostic: formats and controls are discovered at
runtime through v4l2-ctl, so any UVC webcam gets whatever it supports.
"""
import re
import subprocess

CTRL_RE = re.compile(r"^\s*(\w+)\s+0x([0-9a-f]+)\s+\((\w+)\)\s*:\s*(.*)$")
MENU_RE = re.compile(r"^\s+(\d+):\s+(.*)$")
KV_RE = re.compile(r"(\w+)=(\S+)")

# Generic roles -> candidate V4L2 control names (first match wins)
ROLES = {
    "zoom": ["zoom_absolute"],
    "pan": ["pan_absolute"],
    "tilt": ["tilt_absolute"],
    "focus_auto": ["focus_automatic_continuous", "focus_auto"],
    "focus": ["focus_absolute"],
    "exposure_auto": ["auto_exposure", "exposure_auto"],
    "exposure": ["exposure_time_absolute", "exposure_absolute"],
    "wb_auto": ["white_balance_automatic", "white_balance_temperature_auto"],
    "wb": ["white_balance_temperature"],
    "powerline": ["power_line_frequency"],
}


def board_max_height():
    """Tallest mode this board can show at full frame rate."""
    try:
        model = open("/proc/device-tree/model").read()
    except OSError:
        model = ""
    if "Raspberry Pi 3" in model or "Zero 2" in model:
        return 720   # USB 2 + slower JPEG decoder: 1080p MJPEG drops to ~15 fps
    return 1080


def run(args, timeout=5):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.stdout


class Control:
    def __init__(self, name, kind, kv):
        self.name = name
        self.kind = kind  # int | bool | menu | intmenu
        self.min = int(kv.get("min", 0))
        self.max = int(kv.get("max", 1))
        self.step = max(1, int(kv.get("step", 1)))
        self.default = int(kv.get("default", 0))
        self.value = int(kv.get("value", self.default))
        flags = kv.get("flags", "")
        self.inactive = "inactive" in flags
        self.readonly = "read-only" in flags or "write-only" in flags
        self.menu = {}

    @property
    def label(self):
        return self.name.replace("_absolute", "").replace("_", " ").strip().title()


class Camera:
    def __init__(self, dev, name="Camera", cam_id="unknown"):
        self.dev = dev
        self.name = name
        self.id = cam_id
        self.formats = {}
        self.controls = {}
        self.mode = None
        self.probe()

    # ---------- probing ----------
    def probe(self):
        self.formats = self._formats()
        self.refresh_controls()
        self.mode = self.choose_mode()

    def _formats(self):
        out = run(["v4l2-ctl", "-d", self.dev, "--list-formats-ext"])
        fmts, cur, size = {}, None, None
        for line in out.splitlines():
            m = re.search(r"\[\d+\]: '(\w+)'", line)
            if m:
                cur = m.group(1); fmts.setdefault(cur, {}); size = None; continue
            m = re.search(r"Size: Discrete (\d+)x(\d+)", line)
            if m and cur:
                size = (int(m.group(1)), int(m.group(2)))
                fmts[cur].setdefault(size, []); continue
            m = re.search(r"\(([\d.]+) fps\)", line)
            if m and cur and size:
                fmts[cur][size].append(float(m.group(1)))
        return fmts

    def refresh_controls(self):
        out = run(["v4l2-ctl", "-d", self.dev, "--list-ctrls-menus"])
        ctrls, last = {}, None
        for line in out.splitlines():
            m = CTRL_RE.match(line)
            if m:
                name, _, kind, rest = m.groups()
                last = None
                if kind in ("int", "bool", "menu", "intmenu"):
                    c = Control(name, kind, dict(KV_RE.findall(rest)))
                    if not c.readonly:
                        ctrls[name] = c
                        last = c
                continue
            m = MENU_RE.match(line)
            if m and last is not None and last.kind in ("menu", "intmenu"):
                last.menu[int(m.group(1))] = m.group(2).strip()
        self.controls = ctrls

    def choose_mode(self):
        """Best mode the Pi 4 can show at full frame rate.

        Raw NV12/YUYV needs no decoding (USB 3 cameras at 1080p30).
        MJPEG goes through the hardware JPEG decoder (~22-25 fps at 1080p).
        """
        def has(fourcc, w, h, fps=29):
            return any(f >= fps for f in self.formats.get(fourcc, {}).get((w, h), []))

        max_h = board_max_height()
        order = [("NV12", 1920, 1080), ("YUYV", 1920, 1080), ("MJPG", 1920, 1080),
                 ("NV12", 1280, 720), ("YUYV", 1280, 720), ("MJPG", 1280, 720)]
        for fourcc, w, h in order:
            if h <= max_h and has(fourcc, w, h):
                return {"fourcc": fourcc, "w": w, "h": h, "fps": 30}
        best = None
        for fourcc in ("NV12", "YUYV", "MJPG"):
            for (w, h), rates in self.formats.get(fourcc, {}).items():
                fps = max(rates) if rates else 0
                if fps >= 10 and h <= max_h and (best is None or w * h > best["w"] * best["h"]):
                    best = {"fourcc": fourcc, "w": w, "h": h, "fps": int(round(fps))}
        return best

    # ---------- controls ----------
    def role(self, role):
        for n in ROLES.get(role, []):
            if n in self.controls:
                return self.controls[n]
        return None

    def set(self, name, value, refresh=True):
        c = self.controls.get(name)
        if not c:
            return False
        if c.kind == "int":
            value = max(c.min, min(c.max, int(value)))
            value = c.min + round((value - c.min) / c.step) * c.step
        elif c.kind == "bool":
            value = 1 if value else 0
        r = subprocess.run(["v4l2-ctl", "-d", self.dev, "-c", f"{name}={int(value)}"],
                           capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            return False
        c.value = int(value)
        if refresh and c.kind in ("bool", "menu", "intmenu"):
            # auto modes flip other controls' inactive flags
            self.refresh_controls()
        return True

    def nudge(self, name, steps):
        c = self.controls.get(name)
        if not c:
            return None
        if c.kind == "bool":
            self.set(name, 0 if c.value else 1)
        elif c.kind in ("menu", "intmenu"):
            keys = sorted(c.menu) or [c.value]
            i = keys.index(c.value) if c.value in keys else 0
            self.set(name, keys[(i + (1 if steps > 0 else -1)) % len(keys)])
        else:
            span = c.max - c.min
            if span / c.step <= 50:
                chunk = c.step
            else:
                chunk = max(c.step, int(round(span / 50 / c.step)) * c.step)
            self.set(name, c.value + steps * chunk)
        return self.controls[name].value

    def reset_defaults(self):
        # autos first so dependent manual controls become writable
        for c in sorted(self.controls.values(), key=lambda c: c.kind == "int"):
            cur = self.controls.get(c.name)
            if cur and not cur.inactive:
                self.set(c.name, c.default)

    def snapshot(self):
        return {n: c.value for n, c in self.controls.items()}

    def apply(self, values):
        for kinds in (("bool", "menu", "intmenu"), ("int",)):
            for n, v in values.items():
                c = self.controls.get(n)
                if c and c.kind in kinds and not c.inactive and c.value != v:
                    self.set(n, v)


def find_cameras():
    """USB video capture devices (skips the Pi's own codec/ISP nodes)."""
    import pyudev
    ctx = pyudev.Context()
    cams = []
    for d in ctx.list_devices(subsystem="video4linux"):
        caps = d.properties.get("ID_V4L_CAPABILITIES", "")
        if ":capture:" not in caps or d.properties.get("ID_BUS") != "usb":
            continue
        # UVC exposes a metadata node too; keep only nodes with real formats
        if "'" not in run(["v4l2-ctl", "-d", d.device_node, "--list-formats"]):
            continue
        name = d.properties.get("ID_V4L_PRODUCT") or d.properties.get("ID_MODEL", "Camera")
        cid = "%s_%s_%s" % (d.properties.get("ID_VENDOR_ID", "0000"),
                            d.properties.get("ID_MODEL_ID", "0000"),
                            d.properties.get("ID_SERIAL_SHORT", "0"))
        cams.append((d.device_node, name, cid))
    cams.sort()
    return cams
