"""panel.py - GTK4 touch control panel for the 800x480 DSI screen.

Built entirely from the camera's discovered controls: ranges become sliders,
booleans become switches, menus become drop-downs. Placement and fullscreen on
DSI-1 come from the labwc window rule for app_id se.camhdmi.panel.
"""
from gi.repository import Gtk, GLib, Gdk

CSS = b"""
window { background: #111; color: #eee; }
.hdr { background: #1d1d1d; padding: 6px 12px; }
.title { font-weight: bold; font-size: 15px; }
.sub { color: #9a9a9a; font-size: 12px; }
.big button, button.big { min-width: 46px; min-height: 42px; font-size: 18px; padding: 0; }
button.preset { min-width: 36px; min-height: 38px; font-size: 15px; font-weight: bold; padding: 0; }
button.preset.set { background: #2f5d8a; color: white; }
button.wide { min-height: 42px; }
togglebutton { min-height: 42px; }
togglebutton:checked { background: #2f5d8a; color: white; }
.ctrl { padding: 6px 10px; border-bottom: 1px solid #262626; }
.ctrl label.name { font-size: 13px; }
.ctrl label.val { color: #8ab4f8; font-size: 13px; }
scale slider { min-width: 26px; min-height: 26px; }
scale trough { min-height: 8px; }
.empty { font-size: 22px; color: #888; }
"""


class Panel(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="camhdmi")
        self.app = app
        self.set_default_size(800, 480)
        self.guard = False
        self.widgets = {}       # control name -> (widget, value_label, row)
        self.preset_btns = {}
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.stack = Gtk.Stack()
        self.set_child(self.stack)
        empty = Gtk.Label(label="No camera\nPlug in a USB camera", justify=Gtk.Justification.CENTER)
        empty.add_css_class("empty")
        self.stack.add_named(empty, "empty")
        self.main = None

    # ================= build =================
    def rebuild(self, cam):
        if self.main:
            self.stack.remove(self.main)
            self.main = None
        self.widgets.clear()
        self.preset_btns.clear()
        if not cam:
            self.stack.set_visible_child_name("empty")
            return
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # header
        hdr = Gtk.Box(spacing=10)
        hdr.add_css_class("hdr")
        t = Gtk.Label(label=cam.name, xalign=0)
        t.add_css_class("title")
        m = cam.mode
        s = Gtk.Label(label=f"{m['w']}×{m['h']}  {m['fourcc']}  {m['fps']} fps  →  HDMI", xalign=0)
        s.add_css_class("sub")
        hdr.append(t)
        hdr.append(s)
        root.append(hdr)

        body = Gtk.Box(spacing=0, vexpand=True)
        root.append(body)

        # ---------- left: preview + view controls ----------
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=8,
                       margin_end=6, margin_top=6, margin_bottom=6)
        left.set_size_request(420, -1)
        left.set_hexpand(False)  # children ask to expand; the left column must not
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.set_size_request(406, 210)
        if self.app.video.paintable:
            self.picture.set_paintable(self.app.video.paintable)
        left.append(self.picture)

        row = Gtk.Box(spacing=8)
        # D-pad
        pad = Gtk.Grid(row_spacing=3, column_spacing=3)
        pad.add_css_class("big")
        hw_pt = cam.role("pan") or cam.role("tilt")
        for (lbl, x, y, fn) in (("▲", 1, 0, lambda: self.app.pan(0, -1)),
                                ("◀", 0, 1, lambda: self.app.pan(-1, 0)),
                                ("⟲", 1, 1, self.app.center),
                                ("▶", 2, 1, lambda: self.app.pan(1, 0)),
                                ("▼", 1, 2, lambda: self.app.pan(0, 1))):
            b = Gtk.Button(label=lbl)
            self._repeat(b, fn, repeat=(lbl != "⟲"))
            pad.attach(b, x, y, 1, 1)
        row.append(pad)

        right_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        zrow = Gtk.Box(spacing=4)
        zrow.add_css_class("big")
        zo = Gtk.Button(label="−")
        zi = Gtk.Button(label="+")
        self._repeat(zo, lambda: self.app.zoom(-1))
        self._repeat(zi, lambda: self.app.zoom(1))
        zl = Gtk.Label(label="Zoom" + ("" if cam.role("zoom") else "\n(digital)"),
                       hexpand=True, justify=Gtk.Justification.CENTER)
        zrow.append(zo)
        zrow.append(zl)
        zrow.append(zi)
        right_col.append(zrow)

        tg = Gtk.Box(spacing=4, homogeneous=True)
        self.t_mirror = Gtk.ToggleButton(label="Mirror", active=self.app.mirror)
        self.t_flip = Gtk.ToggleButton(label="Flip", active=self.app.flip)
        self.t_mirror.connect("toggled", lambda b: (not self.guard) and b.get_active() != self.app.mirror
                              and self.app.toggle_mirror())
        self.t_flip.connect("toggled", lambda b: (not self.guard) and b.get_active() != self.app.flip
                            and self.app.toggle_flip())
        tg.append(self.t_mirror)
        tg.append(self.t_flip)
        right_col.append(tg)

        presets = Gtk.Box(spacing=3, homogeneous=True)
        for n in range(1, 7):
            b = Gtk.Button(label=str(n))
            b.add_css_class("preset")
            b.connect("clicked", lambda _b, n=n: self.app.preset(n, False))
            lp = Gtk.GestureLongPress()
            lp.connect("pressed", lambda *_a, n=n: self.app.preset(n, True))
            b.add_controller(lp)
            presets.append(b)
            self.preset_btns[n] = b
        right_col.append(presets)
        hint = Gtk.Label(label="tap preset = recall · hold = save")
        hint.add_css_class("sub")
        right_col.append(hint)
        row.append(right_col)
        left.append(row)
        body.append(left)

        # ---------- right: auto-generated image controls ----------
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.set_size_request(360, -1)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        lst = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        view_roles = {cam.role(r).name for r in ("zoom", "pan", "tilt") if cam.role(r)}
        for c in cam.controls.values():
            if c.name in view_roles:
                continue  # handled by the D-pad / zoom buttons
            lst.append(self._control_row(c))
        rb = Gtk.Button(label="Reset camera to defaults", margin_top=8, margin_bottom=8,
                        margin_start=10, margin_end=10)
        rb.add_css_class("wide")
        rb.connect("clicked", lambda _b: self.app.reset_all())
        lst.append(rb)
        sw.set_child(lst)
        body.append(sw)

        self.main = root
        self.stack.add_named(root, "main")
        self.stack.set_visible_child_name("main")
        self.sync(False)
        self.sync_presets()

    def _control_row(self, c):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("ctrl")
        top = Gtk.Box()
        name = Gtk.Label(label=c.label, xalign=0, hexpand=True)
        name.add_css_class("name")
        val = Gtk.Label(xalign=1)
        val.add_css_class("val")
        top.append(name)
        top.append(val)
        box.append(top)
        if c.kind == "bool":
            w = Gtk.Switch(halign=Gtk.Align.END)
            w.connect("notify::active", lambda s, _p, n=c.name: self._user(n, 1 if s.get_active() else 0))
            top.append(w)
            val.set_visible(False)
        elif c.kind in ("menu", "intmenu"):
            keys = sorted(c.menu)
            w = Gtk.DropDown.new_from_strings([c.menu[k] for k in keys])
            w.keys = keys
            w.connect("notify::selected", lambda d, _p, n=c.name: self._user(n, d.keys[d.get_selected()]))
            box.append(w)
            val.set_visible(False)
        else:
            w = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, c.min, c.max, c.step)
            w.set_draw_value(False)
            w.connect("value-changed", lambda s, n=c.name: self._user_slider(n, s))
            box.append(w)
        self.widgets[c.name] = (w, val, box)
        return box

    # ================= user input =================
    def _user(self, name, value):
        if self.guard:
            return
        self.app.set_control(name, value)

    def _user_slider(self, name, scale):
        if self.guard:
            return
        w, val, _ = self.widgets[name]
        val.set_text(str(int(scale.get_value())))
        # coalesce drags: apply at most every 80 ms
        if getattr(scale, "pending", 0):
            return
        def apply():
            scale.pending = 0
            self.app.set_control(name, int(scale.get_value()))
            return False
        scale.pending = GLib.timeout_add(80, apply)

    def _repeat(self, button, fn, repeat=True):
        """Tap = one step; hold = keep stepping."""
        state = {"t": 0}
        g = Gtk.GestureClick()
        g.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def tick():
            fn()
            return True

        def pressed(*_a):
            fn()
            if repeat:
                state["t"] = GLib.timeout_add(380, lambda: (state.__setitem__(
                    "t", GLib.timeout_add(110, tick)), False)[1])

        def released(*_a):
            if state["t"]:
                GLib.source_remove(state["t"])
                state["t"] = 0
        g.connect("pressed", pressed)
        g.connect("released", released)
        g.connect("cancel", lambda *_a: released())
        button.add_controller(g)

    # ================= sync from app =================
    def sync(self, rebuild_needed=False):
        cam = self.app.cam
        if not cam or not self.main:
            return
        self.guard = True
        try:
            if self.app.video.paintable and self.picture.get_paintable() is not self.app.video.paintable:
                self.picture.set_paintable(self.app.video.paintable)
            self.t_mirror.set_active(self.app.mirror)
            self.t_flip.set_active(self.app.flip)
            for name, (w, val, box) in self.widgets.items():
                c = cam.controls.get(name)
                if not c:
                    continue
                box.set_sensitive(not c.inactive)
                if c.kind == "bool":
                    w.set_active(bool(c.value))
                elif c.kind in ("menu", "intmenu"):
                    if c.value in w.keys:
                        w.set_selected(w.keys.index(c.value))
                else:
                    if not getattr(w, "pending", 0):
                        w.set_value(c.value)
                    val.set_text(str(c.value))
        finally:
            self.guard = False

    def sync_presets(self):
        presets = self.app.state.get("presets", {})
        for n, b in self.preset_btns.items():
            if str(n) in presets:
                b.add_css_class("set")
            else:
                b.remove_css_class("set")
