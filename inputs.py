"""inputs.py - keyboard / remote / HDMI-CEC input via evdev.

Keyboards are grabbed exclusively so keystrokes never reach the compositor.
CEC (vc4-hdmi) devices are read without grabbing. Touchscreens and mice are
left alone so the compositor can route them to the touch panel.
"""
import evdev
from evdev import ecodes as E
from gi.repository import GLib

SKIP_WORDS = ("ft5x06", "touch", "jack")


class Inputs:
    def __init__(self, on_key):
        self.on_key = on_key  # on_key(code, value, shift)
        self.devs = {}        # path -> (device, watch_id)
        self.shift = False
        self.scan()

    def _wanted(self, d):
        name = d.name.lower()
        if any(w in name for w in SKIP_WORDS):
            return None
        caps = d.capabilities()
        keys = set(caps.get(E.EV_KEY, []))
        if E.BTN_TOUCH in keys:
            return None  # touchscreens belong to the compositor
        if "vc4-hdmi" in name:
            return "cec"
        if (E.KEY_A in keys or E.KEY_UP in keys or E.KEY_VOLUMEUP in keys
                or E.KEY_POWER in keys or E.KEY_SLEEP in keys):  # remotes' power keys too
            # skip pure mice (buttons, no letter keys)
            if E.BTN_LEFT in keys and E.KEY_A not in keys:
                return None
            return "kbd"
        return None

    def scan(self):
        present = set(evdev.list_devices())
        for path in list(self.devs):
            if path not in present:
                self._drop(path)
        for path in present:
            if path in self.devs:
                continue
            try:
                d = evdev.InputDevice(path)
                kind = self._wanted(d)
                if not kind:
                    d.close(); continue
                if kind == "kbd":
                    try:
                        d.grab()
                    except OSError:
                        pass
                wid = GLib.io_add_watch(d.fd, GLib.PRIORITY_DEFAULT,
                                        GLib.IOCondition.IN | GLib.IOCondition.ERR | GLib.IOCondition.HUP,
                                        self._read, path)
                self.devs[path] = (d, wid)
                print(f"input: {kind} {d.name} ({path})", flush=True)
            except OSError:
                continue
        return False

    def _drop(self, path):
        d, wid = self.devs.pop(path)
        GLib.source_remove(wid)
        try:
            d.close()
        except Exception:
            pass

    def _read(self, fd, cond, path):
        entry = self.devs.get(path)
        if not entry:
            return False
        d = entry[0]
        try:
            for ev in d.read():
                if ev.type != E.EV_KEY:
                    continue
                if ev.code in (E.KEY_LEFTSHIFT, E.KEY_RIGHTSHIFT):
                    self.shift = ev.value != 0
                    continue
                if ev.value == 1:
                    print("key:", E.KEY.get(ev.code, ev.code), "|", d.name, flush=True)
                if ev.value in (1, 2):  # press, autorepeat
                    self.on_key(ev.code, ev.value, self.shift)
        except (OSError, BlockingIOError) as e:
            if getattr(e, "errno", None) == 11:
                return True
            self.devs.pop(path, None)
            return False
        return True
