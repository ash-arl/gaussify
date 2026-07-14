"""
Gaussify self-test — headless verification of the processing core.

Run with `python gaussify.py --selftest`. Renders synthetic images through
every fill style and docking mode and asserts on output pixels, so the whole
effect pipeline can be checked without opening the GUI.
"""

from __future__ import annotations

import dataclasses
import os
import random
import tempfile

from PIL import Image

from gaussify_core import (Settings, _place_foreground, needs_fill,
                           process_file, render)
from gaussify_config import settings_from_dict, settings_to_dict


def run_selftest() -> int:
    print("Running Gaussify self-test...")
    tmp = tempfile.mkdtemp(prefix="gaussify_test_")

    # A narrow image that leaves left/right gutters on 16:9.
    src = Image.new("RGB", (800, 1000), (200, 30, 30))
    src_path = os.path.join(tmp, "narrow.png")
    src.save(src_path)

    s = Settings(width=1920, height=1080, blur=40, darken=30, feather=15)
    assert needs_fill(800, 1000, s), "narrow image should need filling"
    assert not needs_fill(1920, 1080, s), "matching aspect should not need fill"

    # --- default blur fill ---
    dst = process_file(src_path, tmp, s, fmt="png")
    out = Image.open(dst)
    assert out.size == (1920, 1080), f"expected 1920x1080, got {out.size}"
    px = out.getpixel((5, 540))
    assert px != (0, 0, 0), f"gutter pixel is black — fill not applied: {px}"
    cx = out.getpixel((960, 540))
    assert abs(cx[0] - 200) < 30, f"center pixel wrong colour: {cx}"
    print(f"  blur fill ......... gutter {px}, center {cx}  OK")

    # --- solid fill (auto dominant color, no darkening) ---
    s2 = dataclasses.replace(s, fill_style="solid", solid_color="auto", darken=0)
    out2 = render(src, s2)
    px2 = out2.getpixel((5, 540))
    assert abs(px2[0] - 200) < 30 and px2[1] < 80, f"solid auto fill wrong: {px2}"
    print(f"  solid fill ........ gutter {px2} (~dominant red)  OK")

    # --- mirror fill ---
    out3 = render(src, dataclasses.replace(s, fill_style="mirror"))
    px3 = out3.getpixel((450, 540))  # inside mirrored strip
    assert px3 != (0, 0, 0), f"mirror fill left gutter black: {px3}"
    print(f"  mirror fill ....... gutter {px3}  OK")

    # --- docked left / right / random ---
    outL = render(src, dataclasses.replace(s, fg_position="left"))
    pL = outL.getpixel((5, 540))
    assert abs(pL[0] - 200) < 30, f"left-dock: left edge should be crisp: {pL}"
    outR = render(src, dataclasses.replace(s, fg_position="right"))
    pR = outR.getpixel((1914, 540))
    assert abs(pR[0] - 200) < 30, f"right-dock: right edge should be crisp: {pR}"
    rng = random.Random(42)
    outRnd = render(src, dataclasses.replace(s, fg_position="random"), rng)
    lp = outRnd.getpixel((5, 540))
    rp = outRnd.getpixel((1914, 540))
    assert (abs(lp[0] - 200) < 30) != (abs(rp[0] - 200) < 30), \
        "random dock: exactly one side should hold the crisp image"
    print("  dock left/right/random  OK")

    # --- the works: scaled card + shadow + corners + tint + vignette ---
    s5 = dataclasses.replace(
        s, fg_scale=80, fg_shadow=50, fg_corner_radius=40, vignette=30,
        saturation=50, tint_color="#203050", tint_strength=20, bg_zoom=1.3)
    out5 = render(src, s5)
    assert out5.size == (1920, 1080)
    # The fg box corner should NOT be crisp red (rounded away to backdrop).
    fg, fx, fy = _place_foreground(src, s5)
    corner = out5.getpixel((fx + 1, fy + 1))
    assert abs(corner[0] - 200) >= 30, f"corner should be rounded off: {corner}"
    print(f"  card+shadow+corners+vignette  OK (corner {corner})")

    # --- settings round-trip & forward compat ---
    d = settings_to_dict(s5)
    d["some_future_key"] = 123
    assert settings_from_dict(d) == s5, "settings round-trip failed"
    print("  settings round-trip  OK")

    print("All checks passed.")
    return 0
