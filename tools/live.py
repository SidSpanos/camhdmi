#!/usr/bin/env python3
"""Is HDMI video moving? Diffs two screenshots 1 s apart (0.00 = frozen)."""
import subprocess, time, os, sys, cairo, numpy as np
env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/1000", WAYLAND_DISPLAY="wayland-0")
def connected_hdmi():
    import glob
    for s in sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/status")):
        if open(s).read().strip() == "connected":
            return s.split("/")[-2].split("-", 1)[1]
    return "HDMI-A-1"
out = sys.argv[2] if len(sys.argv) > 2 else connected_hdmi()
def load(p):
    s = cairo.ImageSurface.create_from_png(p)
    return np.ndarray((s.get_height(), s.get_stride() // 4, 4), np.uint8, s.get_data())[:, :s.get_width(), :3].astype(float)
subprocess.run(["grim", "-o", out, "-s", "0.25", "/tmp/l1.png"], env=env); time.sleep(1.0)
subprocess.run(["grim", "-o", out, "-s", "0.25", "/tmp/l2.png"], env=env)
print(f"{(sys.argv[1] if len(sys.argv) > 1 else 'now'):14} live={np.abs(load('/tmp/l1.png') - load('/tmp/l2.png')).mean():.2f}")
