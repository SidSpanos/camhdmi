"""video.py - GStreamer pipeline: camera -> HDMI (waylandsink) + touch preview.

Main path   : v4l2src -> videocrop (software zoom fallback) -> v4l2convert (ISP,
              hardware colour convert/scale) -> textoverlay (OSD) -> waylandsink
Preview path: tee -> v4l2convert (ISP downscale) -> videoflip -> gtk4paintablesink
Mirror/flip on HDMI uses waylandsink rotate-method (compositor buffer transform,
zero cost, changes live).
"""
import os
from gi.repository import Gst, GLib

# waylandsink gets its own connection; sharing GTK's display stalls frame callbacks
WL = os.environ.get("WAYLAND_DISPLAY", "wayland-0")

def find_monitor_fb():
    """The small SPI screen (fbtft driver), not the vc4 HDMI framebuffer."""
    import glob
    for n in sorted(glob.glob("/sys/class/graphics/fb*/name")):
        try:
            if open(n).read().startswith("fb_"):
                return "/dev/" + n.split("/")[-2]
        except OSError:
            pass
    return None


FB = find_monitor_fb()


def dsi_present():
    import glob
    for s in glob.glob("/sys/class/drm/card*-DSI-*/status"):
        try:
            if open(s).read().strip() == "connected":
                return True
        except OSError:
            pass
    return False

# GstVideoOrientationMethod values
ORIENT = {(False, False): 0, (True, False): 4, (False, True): 5, (True, True): 2}
PREVIEW_W = 368


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Video:
    def __init__(self, on_error):
        self.on_error = on_error
        self.pipe = None
        self.mode = None
        self.paintable = None
        self.mirror = False
        self.flip = False
        self.frames = 0  # HDMI frame counter for the stall watchdog
        self.frozen = False  # freeze frame: drop new frames, HDMI keeps the last one

    # ---------- lifecycle ----------
    def _launch(self, desc):
        self.stop()
        self.mon_text = None  # new pipeline: monitor text must be re-sent
        self.pipe = Gst.parse_launch(desc)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._bus_error)
        psink = self.pipe.get_by_name("psink")
        self.paintable = psink.get_property("paintable") if psink else None
        js = self.pipe.get_by_name("jpg")
        if js:
            js.connect("new-sample", self._jpeg)
        vs = self.pipe.get_by_name("vsink")
        if vs:
            vs.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._count)
        self.pipe.set_state(Gst.State.PLAYING)
        self.set_orientation(self.mirror, self.flip)

    def _count(self, pad, info):
        self.frames += 1  # counted before the drop so the watchdog stays quiet
        return Gst.PadProbeReturn.DROP if self.frozen else Gst.PadProbeReturn.OK

    def start(self, cam):
        self.cam = cam
        self.frozen = False
        m = cam.mode
        self.mode = m
        w, h, fps = m["w"], m["h"], m["fps"]
        if m["fourcc"] == "MJPG":
            src = (f"v4l2src device={cam.dev} ! image/jpeg,width={w},height={h},framerate={fps}/1 "
                   f"! {'v4l2jpegdec' if self.jpeg_hw else 'jpegdec'} ! ")
        else:
            fmt = "NV12" if m["fourcc"] == "NV12" else "YUY2"
            src = (f"v4l2src device={cam.dev} ! "
                   f"video/x-raw,format={fmt},width={w},height={h},framerate={fps}/1 ! ")
        ph = int(round(PREVIEW_W * h / w / 2)) * 2
        desc = (
            src + "tee name=t "
            "t. ! queue max-size-buffers=2 leaky=downstream ! videocrop name=crop "
            # mirror/flip in the ISP, before the OSD, so OSD text is never mirrored
            f"! v4l2convert extra-controls=\"c,horizontal_flip={int(self.mirror)},vertical_flip={int(self.flip)}\" "
            f"! video/x-raw,format=BGRx,width={w},height={h} "
            "! textoverlay name=osd silent=true font-desc=\"Sans Bold 15\" valignment=bottom "
            "halignment=left xpad=36 ypad=26 shaded-background=true shading-value=150 "
            f"! waylandsink name=vsink sync=false display={WL} "
            + self._preview(w, h) + " " + self._web(w, h)
        )
        self._launch(desc)

    def _preview(self, w, h):
        if FB:
            ph = min(240, int(round(320 * h / w / 2)) * 2)
            box = f"! videobox bottom=-{240 - ph} " if ph < 240 else ""
            return ("t. ! queue max-size-buffers=1 leaky=downstream "
                    f"! v4l2convert ! video/x-raw,format=BGRx,width=320,height={ph} "
                    "! videorate drop-only=true ! video/x-raw,framerate=15/1 "
                    "! videoflip name=pflip " + box +
                    "! textoverlay name=mosd font-desc=\"Sans Bold 10\" valignment=bottom "
                    "halignment=left xpad=5 ypad=4 "
                    f"! videoconvert ! fbdevsink device={FB} sync=false")
        if not dsi_present():
            return ""  # no touch panel: don't waste the ISP on a preview nobody sees
        ph = int(round(PREVIEW_W * h / w / 2)) * 2
        return ("t. ! queue max-size-buffers=1 leaky=downstream "
                f"! v4l2convert ! video/x-raw,format=BGRx,width={PREVIEW_W},height={ph} "
                "! videorate drop-only=true ! video/x-raw,framerate=15/1 "
                "! videoflip name=pflip ! videoconvert ! gtk4paintablesink name=psink sync=false")

    def _web(self, w, h):
        """Phone preview: ISP downscale, mirror like HDMI, hardware JPEG."""
        wh = int(round(640 * h / w / 2)) * 2
        if Gst.ElementFactory.find("v4l2jpegenc"):
            enc = "v4l2jpegenc extra-controls=\"c,compression_quality=75\""
        else:
            enc = "jpegenc quality=75"
        return ("t. ! queue max-size-buffers=1 leaky=downstream "
                f"! v4l2convert ! video/x-raw,format=I420,width=640,height={wh} "
                "! videorate drop-only=true ! video/x-raw,framerate=15/1 "
                f"! videoflip name=wflip ! {enc} "
                "! appsink name=jpg emit-signals=true max-buffers=1 drop=true sync=false")

    def _jpeg(self, sink):
        s = sink.emit("pull-sample")
        if s and self.on_jpeg:
            b = s.get_buffer()
            ok, m = b.map(Gst.MapFlags.READ)
            if ok:
                try:
                    self.on_jpeg(bytes(m.data))
                finally:
                    b.unmap(m)
        return Gst.FlowReturn.OK

    def _standby_fb(self, text):
        if not FB:
            return ""
        return (" videotestsrc pattern=black is-live=true ! video/x-raw,width=320,height=240,framerate=5/1 "
                "! textoverlay name=mosd font-desc=\"Sans Bold 13\" valignment=center halignment=center "
                f"text=\"{esc(text)}\" ! videoconvert ! fbdevsink device={FB} sync=false")

    def monitor(self, markup):
        self.mon_text = markup
        m = self._el("mosd")
        if m:
            m.set_property("text", markup or "")

    def standby(self, text):
        desc = ("videotestsrc pattern=black is-live=true ! video/x-raw,width=1280,height=720,framerate=5/1 "
                "! textoverlay name=osd font-desc=\"Sans Bold 36\" valignment=center halignment=center "
                f"text=\"{esc(text)}\" ! videoconvert ! waylandsink name=vsink sync=false display={WL}"
                + self._standby_fb(text))
        self.mode = None
        self._launch(desc)

    def stop(self):
        if self.pipe:
            self.pipe.set_state(Gst.State.NULL)
            self.pipe.get_bus().remove_signal_watch()
            self.pipe = None
            self.paintable = None

    def _bus_error(self, bus, msg):
        err, dbg = msg.parse_error()
        print("GST ERROR:", err.message, dbg, flush=True)
        GLib.idle_add(self.on_error, err.message)

    # ---------- live adjustments ----------
    def _el(self, name):
        return self.pipe.get_by_name(name) if self.pipe else None

    def set_orientation(self, mirror, flip):
        changed = (mirror, flip) != (self.mirror, self.flip)
        self.mirror, self.flip = mirror, flip
        for name in ("pflip", "wflip"):
            p = self._el(name)
            if p:
                p.set_property("video-direction", ORIENT[(mirror, flip)])
        # HDMI flips in the ISP, which only reads the setting at start: rebuild
        if changed and self.pipe and self.mode and getattr(self, "cam", None):
            self.start(self.cam)
            if getattr(self, "crop_args", None):
                self.set_crop(*self.crop_args)

    def set_crop(self, zoom, ox, oy):
        """Software zoom/pan: zoom >= 1, ox/oy in -1..1."""
        self.crop_args = (zoom, ox, oy)
        c = self._el("crop")
        if not c or not self.mode:
            return
        w, h = self.mode["w"], self.mode["h"]
        cw, ch = w / zoom, h / zoom
        left = (w - cw) / 2 * (1 + ox)
        top = (h - ch) / 2 * (1 + oy)
        ev = lambda x: max(0, int(x) // 2 * 2)
        L, T = ev(left), ev(top)
        R, B = ev(w - cw - left), ev(h - ch - top)
        for k, v in (("left", L), ("right", R), ("top", T), ("bottom", B)):
            c.set_property(k, v)

    osd_enabled = False  # HDMI is the program feed: keep it clean unless asked
    mon_text = None      # last text sent to the small monitor screen
    on_jpeg = None       # callback(bytes) for the phone preview
    jpeg_hw = True       # MJPEG via hardware decoder; watchdog flips to software if it hangs

    def osd(self, markup):
        o = self._el("osd")
        if not o:
            return
        if not self.osd_enabled:
            o.set_property("silent", True)
            if markup:
                print("osd (not shown on program):", markup.replace("\n", " | "), flush=True)
            return
        if markup:
            o.set_property("text", markup)
            o.set_property("silent", False)
        else:
            o.set_property("silent", True)
