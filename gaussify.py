"""
Gaussify — Blurred-Fill Wallpaper Processor
===========================================

Batch-processes images so they fill a target screen resolution without black
bars. The original image stays crisp; the empty gutters are filled with a
blurred (or solid, or mirrored) backdrop derived from the same image, fading
smoothly into the sharp center. Extensive controls: fill style, tint,
saturation, background zoom, vignette, foreground scale/position (including a
random left/right dock), drop shadow, and rounded corners. Settings persist
between sessions and can be saved as named presets.

Run the GUI:      python gaussify.py
Run a self-test:  python gaussify.py --selftest

Only dependency: Pillow (pip install Pillow). Tkinter ships with Python.
"""

from __future__ import annotations

import dataclasses
import json
import os
import random
import sys
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

# ---------------------------------------------------------------------------
# Core image processing (no GUI — importable & testable on its own)
# ---------------------------------------------------------------------------

SUPPORTED_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "gaussify_config.json")


@dataclass
class Settings:
    """All knobs that control the fill effect."""
    width: int = 1920
    height: int = 1080
    blur: float = 60.0          # Gaussian blur radius (px) applied to the fill
    darken: float = 35.0        # 0..100 %, how much to darken the backdrop
    feather: float = 12.0       # 0..40 %, crisp->fill fade width as % of gutter
    process_matching: bool = False   # also process images that already fit
    tolerance: float = 0.02     # aspect match tolerance (fraction of a side)
    # Background / fill
    fill_style: str = "blur"    # "blur" | "solid" | "mirror"
    solid_color: str = "auto"   # "auto" (dominant image color) or "#RRGGBB"
    bg_zoom: float = 1.0        # 1.0..2.0 extra zoom on the cover-scaled backdrop
    saturation: float = 100.0   # 0..150 %, backdrop only (100 = unchanged)
    tint_color: str = "#000000"  # tint overlay color for the backdrop
    tint_strength: float = 0.0  # 0..100 %, 0 = off
    vignette: float = 0.0       # 0..100 %, darkened edges over the final image
    # Foreground (the crisp image)
    fg_scale: float = 100.0     # 50..100 % of the max fit size
    fg_position: str = "center"  # "center"|"left"|"right"|"top"|"bottom"|"random"
    fg_shadow: float = 0.0      # 0..100 drop-shadow strength (blur + opacity)
    fg_corner_radius: int = 0   # px, rounded corners on the crisp image


# Fields that belong to a visual "preset" (everything except output geometry).
PRESET_FIELDS = (
    "blur", "darken", "feather", "fill_style", "solid_color", "bg_zoom",
    "saturation", "tint_color", "tint_strength", "vignette",
    "fg_scale", "fg_position", "fg_shadow", "fg_corner_radius",
)

BUILTIN_PRESETS: dict[str, dict] = {
    "Subtle": dict(blur=40, darken=20, feather=10, fill_style="blur",
                   bg_zoom=1.0, saturation=100, tint_strength=0, vignette=0,
                   fg_scale=100, fg_position="center", fg_shadow=0,
                   fg_corner_radius=0),
    "Classy": dict(blur=70, darken=45, feather=15, fill_style="blur",
                   bg_zoom=1.15, saturation=40, tint_color="#1a2a40",
                   tint_strength=15, vignette=10, fg_scale=100,
                   fg_position="center", fg_shadow=0, fg_corner_radius=0),
    "Dramatic": dict(blur=100, darken=60, feather=12, fill_style="blur",
                     bg_zoom=1.3, saturation=70, tint_strength=0, vignette=35,
                     fg_scale=92, fg_position="center", fg_shadow=40,
                     fg_corner_radius=24),
}


def settings_to_dict(s: Settings) -> dict:
    return dataclasses.asdict(s)


def settings_from_dict(d: dict) -> Settings:
    """Build Settings from a dict, ignoring unknown keys (forward compat)."""
    fields = {f.name for f in dataclasses.fields(Settings)}
    return Settings(**{k: v for k, v in d.items() if k in fields})


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass  # persistence is best-effort; never crash the app over it


def _parse_hex(color: str, fallback=(0, 0, 0)) -> tuple[int, int, int]:
    try:
        c = color.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore
    except (ValueError, IndexError):
        return fallback


def _dominant_color(img: Image.Image) -> tuple[int, int, int]:
    """Cheap dominant/average color: shrink the image to a single pixel."""
    return img.resize((1, 1), Image.LANCZOS).getpixel((0, 0))


def needs_fill(img_w: int, img_h: int, s: Settings) -> bool:
    """True if the fitted image leaves visible gutters on the target canvas."""
    fit = min(s.width / img_w, s.height / img_h)
    gap_x = s.width - img_w * fit
    gap_y = s.height - img_h * fit
    return (gap_x > s.tolerance * s.width) or (gap_y > s.tolerance * s.height)


def _cover_resize(img: Image.Image, w: int, h: int, zoom: float = 1.0) -> Image.Image:
    """Scale + center-crop `img` so it completely covers a w x h box.
    `zoom` > 1 over-scales further before cropping (tighter, more abstract fill)."""
    scale = max(w / img.width, h / img.height) * max(zoom, 1.0)
    new_w = max(1, round(img.width * scale))
    new_h = max(1, round(img.height * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _fit_resize(img: Image.Image, w: int, h: int) -> tuple[Image.Image, int, int]:
    """Scale `img` to fit inside w x h (letterbox), centered. Returns (img, x, y)."""
    scale = min(w / img.width, h / img.height)
    new_w = max(1, round(img.width * scale))
    new_h = max(1, round(img.height * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    return resized, (w - new_w) // 2, (h - new_h) // 2


def _place_foreground(img: Image.Image, s: Settings,
                      rng: random.Random | None = None
                      ) -> tuple[Image.Image, int, int]:
    """Fit-resize the crisp image (times fg_scale) and position it on the canvas."""
    w, h = s.width, s.height
    fg_frac = min(max(s.fg_scale, 10.0), 100.0) / 100.0
    scale = min(w / img.width, h / img.height) * fg_frac
    fw = max(1, round(img.width * scale))
    fh = max(1, round(img.height * scale))
    fg = img.resize((fw, fh), Image.LANCZOS)

    pos = s.fg_position
    if pos == "random":
        pos = (rng or random).choice(("left", "right"))

    margin = round(0.02 * min(w, h)) if fg_frac < 0.995 else 0
    x = (w - fw) // 2
    y = (h - fh) // 2
    if pos == "left":
        x = margin
    elif pos == "right":
        x = w - fw - margin
    elif pos == "top":
        y = margin
    elif pos == "bottom":
        y = h - fh - margin
    return fg, x, y


def _mirror_fill(img: Image.Image, w: int, h: int) -> Image.Image:
    """Backdrop made of the fitted image with gutters filled by mirrored edges."""
    fg, x, y = _fit_resize(img, w, h)
    fw, fh = fg.size
    bg = Image.new("RGB", (w, h), (0, 0, 0))
    bg.paste(fg, (x, y))
    if x > 0:  # left/right gutters
        gw = min(x, fw)
        bg.paste(ImageOps.mirror(fg.crop((0, 0, gw, fh))), (x - gw, y))
        gw2 = min(w - (x + fw), fw)
        bg.paste(ImageOps.mirror(fg.crop((fw - gw2, 0, fw, fh))), (x + fw, y))
    if y > 0:  # top/bottom gutters
        gh = min(y, fh)
        bg.paste(ImageOps.flip(fg.crop((0, 0, fw, gh))), (x, y - gh))
        gh2 = min(h - (y + fh), fh)
        bg.paste(ImageOps.flip(fg.crop((0, fh - gh2, fw, fh))), (x, y + fh))
    return bg


def _build_background(img: Image.Image, s: Settings) -> Image.Image:
    """Full-canvas backdrop in the chosen fill style, then tone adjustments."""
    w, h = s.width, s.height
    style = s.fill_style

    if style == "solid":
        color = (_dominant_color(img) if s.solid_color == "auto"
                 else _parse_hex(s.solid_color))
        bg = Image.new("RGB", (w, h), color)
    elif style == "mirror":
        bg = _mirror_fill(img, w, h)
        if s.blur > 0:
            bg = bg.filter(ImageFilter.GaussianBlur(radius=s.blur))
    else:  # "blur"
        bg = _cover_resize(img, w, h, zoom=s.bg_zoom)
        if s.blur > 0:
            bg = bg.filter(ImageFilter.GaussianBlur(radius=s.blur))

    if style != "solid" and abs(s.saturation - 100.0) > 0.5:
        bg = ImageEnhance.Color(bg).enhance(max(s.saturation, 0.0) / 100.0)
    if s.darken > 0:
        bg = ImageEnhance.Brightness(bg).enhance(max(0.0, 1.0 - s.darken / 100.0))
    if s.tint_strength > 0:
        tint = Image.new("RGB", (w, h), _parse_hex(s.tint_color))
        bg = Image.blend(bg, tint, min(s.tint_strength, 100.0) / 100.0)
    return bg


def _edge_fade_profile(length: int, start: int, span: int,
                       feather_start: int, feather_end: int) -> list[int]:
    """
    1-D alpha profile: 0 outside [start, start+span), 255 inside, fading
    linearly over `feather_start` px at the start edge and `feather_end` px at
    the end edge (either may be 0 for a hard edge).
    """
    profile = [0] * length
    end = start + span
    for i in range(max(0, start), min(length, end)):
        a = 255
        if feather_start > 0 and (i - start) < feather_start:
            a = min(a, round(255 * (i - start) / feather_start))
        if feather_end > 0 and (end - 1 - i) < feather_end:
            a = min(a, round(255 * (end - 1 - i) / feather_end))
        profile[i] = a
    return profile


def _build_fade_mask(w: int, h: int, fx: int, fy: int, fw: int, fh: int,
                     feather_frac: float) -> Image.Image:
    """
    Feathered alpha mask for a foreground box at (fx, fy, fw, fh). Only edges
    that actually face a gutter fade; each edge's fade width is `feather_frac`
    of that gutter. Built from two 1-D gradients multiplied together (fast).
    """
    def edge(gap: int, cap: int) -> int:
        return min(int(feather_frac * gap), cap) if gap > 2 else 0

    f_left = edge(fx, fw // 2)
    f_right = edge(w - (fx + fw), fw // 2)
    f_top = edge(fy, fh // 2)
    f_bottom = edge(h - (fy + fh), fh // 2)

    row = Image.new("L", (w, 1))
    row.putdata(_edge_fade_profile(w, fx, fw, f_left, f_right))
    col = Image.new("L", (1, h))
    col.putdata(_edge_fade_profile(h, fy, fh, f_top, f_bottom))
    return ImageChops.multiply(row.resize((w, h), Image.NEAREST),
                               col.resize((w, h), Image.NEAREST))


def _apply_vignette(img: Image.Image, amount: float) -> Image.Image:
    """Darken edges/corners with a soft radial mask. `amount` is 0..100 %."""
    if amount <= 0:
        return img
    w, h = img.size
    qw, qh = max(4, w // 8), max(4, h // 8)
    edge_val = 255 - int(2.55 * min(amount, 100.0))
    m = Image.new("L", (qw, qh), edge_val)
    ImageDraw.Draw(m).ellipse(
        [-qw * 0.18, -qh * 0.18, qw * 1.18, qh * 1.18], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(radius=max(qw, qh) / 5))
    m = m.resize((w, h), Image.BILINEAR)
    return ImageChops.multiply(img, Image.merge("RGB", (m, m, m)))


def render(img: Image.Image, s: Settings,
           rng: random.Random | None = None) -> Image.Image:
    """
    Produce the final `s.width` x `s.height` image from `img`.

    If the image already fits at full foreground scale and `process_matching`
    is False, it is fit-scaled onto the canvas unchanged. Otherwise the crisp
    foreground is composited over the styled backdrop with a feathered seam,
    optional drop shadow, rounded corners, and vignette.
    """
    img = img.convert("RGB")
    w, h = s.width, s.height

    fg, fx, fy = _place_foreground(img, s, rng)
    fw, fh = fg.size
    effect_forced = s.fg_scale < 99.5 or s.process_matching

    if not needs_fill(img.width, img.height, s) and not effect_forced:
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(fg, (fx, fy))
        return _apply_vignette(canvas, s.vignette)

    bg = _build_background(img, s)

    # Drop shadow: a blurred dark rounded-rect under the foreground box.
    if s.fg_shadow > 0:
        strength = min(s.fg_shadow, 100.0) / 100.0
        sh_blur = max(2.0, strength * min(w, h) * 0.04)
        offset = round(sh_blur * 0.5)
        smask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(smask).rounded_rectangle(
            [fx, fy + offset, fx + fw - 1, fy + fh - 1 + offset],
            radius=max(s.fg_corner_radius, 0), fill=round(210 * strength))
        smask = smask.filter(ImageFilter.GaussianBlur(radius=sh_blur))
        bg.paste((0, 0, 0), (0, 0), smask)

    mask = _build_fade_mask(w, h, fx, fy, fw, fh, s.feather / 100.0)
    if s.fg_corner_radius > 0:
        rmask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(rmask).rounded_rectangle(
            [fx, fy, fx + fw - 1, fy + fh - 1],
            radius=s.fg_corner_radius, fill=255)
        mask = ImageChops.multiply(mask, rmask)

    fg_full = Image.new("RGB", (w, h), (0, 0, 0))
    fg_full.paste(fg, (fx, fy))
    out = Image.composite(fg_full, bg, mask)
    return _apply_vignette(out, s.vignette)


def output_path(src: str, out_dir: str, fmt: str) -> str:
    base = os.path.splitext(os.path.basename(src))[0]
    ext = "png" if fmt.lower() == "png" else "jpg"
    return os.path.join(out_dir, f"{base}.{ext}")


def process_file(src: str, out_dir: str, s: Settings, fmt: str = "png",
                 quality: int = 92) -> str:
    """Render one file to `out_dir` and return the written path."""
    with Image.open(src) as im:
        result = render(im, s)
    os.makedirs(out_dir, exist_ok=True)
    dst = output_path(src, out_dir, fmt)
    # Never overwrite the source file itself (e.g. output folder == source folder).
    if os.path.abspath(dst) == os.path.abspath(src):
        base, ext = os.path.splitext(dst)
        dst = f"{base}_gaussified{ext}"
    if fmt.lower() == "jpg":
        result.save(dst, "JPEG", quality=quality)
    else:
        result.save(dst, "PNG")
    return dst


# ---------------------------------------------------------------------------
# Self-test (headless verification of the core algorithm)
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import tempfile
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


# ---------------------------------------------------------------------------
# GUI (Tkinter)
# ---------------------------------------------------------------------------

def _run_gui() -> int:
    import threading
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
    from PIL import ImageTk

    RES_PRESETS = {
        "1920 x 1080 (1080p)": (1920, 1080),
        "2560 x 1440 (1440p)": (2560, 1440),
        "3840 x 2160 (4K)": (3840, 2160),
        "Custom": None,
    }
    FG_POSITIONS = ("center", "left", "right", "top", "bottom", "random")

    def detect_screen(widget) -> tuple[int, int]:
        try:
            return widget.winfo_screenwidth(), widget.winfo_screenheight()
        except Exception:
            return 1920, 1080

    class App:
        def __init__(self, root: tk.Tk):
            self.root = root
            root.title("Gaussify — Blurred-Fill Wallpaper Processor")
            root.geometry("1180x780")
            root.minsize(980, 660)

            self.files: list[str] = []
            self.out_dir: str = ""
            self._preview_img = None          # keep a ref so Tk doesn't GC it
            self._preview_after = None        # debounce handle
            self._cur_src: Image.Image | None = None
            self._cur_path: str = ""
            self._slider_labels: list[tuple[tk.DoubleVar, ttk.Label]] = []
            self.solid_color_val = "#336699"
            self.tint_color_val = "#000000"
            self.user_presets: dict[str, dict] = {}

            scr_w, scr_h = detect_screen(root)

            # ---- layout: left (list) | right (preview) ; bottom (controls) ----
            main = ttk.Frame(root, padding=8)
            main.pack(fill="both", expand=True)

            left = ttk.Frame(main)
            left.pack(side="left", fill="y")

            ttk.Label(left, text="Images", font=("Segoe UI", 11, "bold")).pack(anchor="w")
            btns = ttk.Frame(left)
            btns.pack(fill="x", pady=4)
            ttk.Button(btns, text="Add Images…", command=self.add_images).pack(side="left")
            ttk.Button(btns, text="Add Folder…", command=self.add_folder).pack(side="left", padx=4)
            ttk.Button(btns, text="Clear", command=self.clear_list).pack(side="left")

            list_frame = ttk.Frame(left)
            list_frame.pack(fill="both", expand=True)
            self.listbox = tk.Listbox(list_frame, width=34, activestyle="dotbox")
            self.listbox.pack(side="left", fill="both", expand=True)
            sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
            sb.pack(side="right", fill="y")
            self.listbox.config(yscrollcommand=sb.set)
            self.listbox.bind("<<ListboxSelect>>", self.on_select)

            right = ttk.Frame(main)
            right.pack(side="left", fill="both", expand=True, padx=(10, 0))
            ttk.Label(right, text="Preview", font=("Segoe UI", 11, "bold")).pack(anchor="w")
            self.canvas = tk.Canvas(right, bg="#1e1e1e", highlightthickness=0)
            self.canvas.pack(fill="both", expand=True)
            self.canvas.bind("<Configure>", lambda e: self.schedule_preview())

            # ---- bottom controls ----
            ctrl = ttk.LabelFrame(root, text="Settings", padding=8)
            ctrl.pack(fill="x", padx=8, pady=(0, 8))

            # Presets row (above the tabs)
            row_p = ttk.Frame(ctrl)
            row_p.pack(fill="x", pady=(0, 4))
            ttk.Label(row_p, text="Preset:").pack(side="left")
            self.preset_var = tk.StringVar(value="")
            self.preset_combo = ttk.Combobox(row_p, textvariable=self.preset_var,
                                             width=18, state="readonly")
            self.preset_combo.pack(side="left", padx=4)
            self.preset_combo.bind("<<ComboboxSelected>>", self.on_preset_selected)
            ttk.Button(row_p, text="Save preset…",
                       command=self.save_preset).pack(side="left", padx=2)
            ttk.Button(row_p, text="Delete",
                       command=self.delete_preset).pack(side="left")

            nb = ttk.Notebook(ctrl)
            nb.pack(fill="x")
            tab_basic = ttk.Frame(nb, padding=6)
            tab_bg = ttk.Frame(nb, padding=6)
            tab_fg = ttk.Frame(nb, padding=6)
            nb.add(tab_basic, text=" Basics ")
            nb.add(tab_bg, text=" Background ")
            nb.add(tab_fg, text=" Foreground ")

            # ===== Basics tab =====
            row1 = ttk.Frame(tab_basic)
            row1.pack(fill="x", pady=2)
            ttk.Label(row1, text="Resolution:").pack(side="left")
            self.res_var = tk.StringVar(value="1920 x 1080 (1080p)")
            res_combo = ttk.Combobox(row1, textvariable=self.res_var, width=20,
                                     state="readonly", values=list(RES_PRESETS.keys()))
            res_combo.pack(side="left", padx=4)
            res_combo.bind("<<ComboboxSelected>>", self.on_res_change)
            ttk.Label(row1, text="W:").pack(side="left")
            self.w_var = tk.StringVar(value=str(scr_w))
            ttk.Entry(row1, textvariable=self.w_var, width=6).pack(side="left")
            ttk.Label(row1, text="H:").pack(side="left")
            self.h_var = tk.StringVar(value=str(scr_h))
            ttk.Entry(row1, textvariable=self.h_var, width=6).pack(side="left")
            ttk.Button(row1, text="Use my screen",
                       command=lambda: self.set_res(scr_w, scr_h, from_preset=False)
                       ).pack(side="left", padx=8)

            self.blur_var = tk.DoubleVar(value=60)
            self.darken_var = tk.DoubleVar(value=35)
            self.feather_var = tk.DoubleVar(value=12)
            self._add_slider(tab_basic, "Blur strength", self.blur_var, 0, 150)
            self._add_slider(tab_basic, "Side darkening (%)", self.darken_var, 0, 100)
            self._add_slider(tab_basic, "Feather / fade (%)", self.feather_var, 0, 40)

            row_misc = ttk.Frame(tab_basic)
            row_misc.pack(fill="x", pady=2)
            self.process_matching_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(row_misc, text="Also process already-matching images",
                            variable=self.process_matching_var,
                            command=self.schedule_preview).pack(side="left")
            ttk.Label(row_misc, text="Format:").pack(side="left", padx=(12, 2))
            self.fmt_var = tk.StringVar(value="png")
            ttk.Combobox(row_misc, textvariable=self.fmt_var, width=5, state="readonly",
                         values=["png", "jpg"]).pack(side="left")

            # ===== Background tab =====
            row_fill = ttk.Frame(tab_bg)
            row_fill.pack(fill="x", pady=2)
            ttk.Label(row_fill, text="Fill style:", width=18).pack(side="left")
            self.fill_style_var = tk.StringVar(value="blur")
            for style, lbl in (("blur", "Blur"), ("solid", "Solid color"),
                               ("mirror", "Mirrored")):
                ttk.Radiobutton(row_fill, text=lbl, value=style,
                                variable=self.fill_style_var,
                                command=self.schedule_preview).pack(side="left", padx=4)

            row_solid = ttk.Frame(tab_bg)
            row_solid.pack(fill="x", pady=2)
            ttk.Label(row_solid, text="Solid color:", width=18).pack(side="left")
            self.solid_auto_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(row_solid, text="Auto (from image)",
                            variable=self.solid_auto_var,
                            command=self._on_solid_auto).pack(side="left")
            self.solid_swatch = tk.Button(row_solid, width=3, bg=self.solid_color_val,
                                          state="disabled",
                                          command=self.pick_solid_color)
            self.solid_swatch.pack(side="left", padx=6)

            self.bg_zoom_var = tk.DoubleVar(value=100)      # percent, 100..200
            self.saturation_var = tk.DoubleVar(value=100)
            self._add_slider(tab_bg, "Background zoom (%)", self.bg_zoom_var, 100, 200)
            self._add_slider(tab_bg, "Saturation (%)", self.saturation_var, 0, 150)

            row_tint = ttk.Frame(tab_bg)
            row_tint.pack(fill="x", pady=2)
            ttk.Label(row_tint, text="Tint color:", width=18).pack(side="left")
            self.tint_swatch = tk.Button(row_tint, width=3, bg=self.tint_color_val,
                                         command=self.pick_tint_color)
            self.tint_swatch.pack(side="left", padx=6)
            self.tint_strength_var = tk.DoubleVar(value=0)
            self._add_slider(tab_bg, "Tint strength (%)", self.tint_strength_var, 0, 100)

            self.vignette_var = tk.DoubleVar(value=0)
            self._add_slider(tab_bg, "Vignette (%)", self.vignette_var, 0, 100)

            # ===== Foreground tab =====
            row_pos = ttk.Frame(tab_fg)
            row_pos.pack(fill="x", pady=2)
            ttk.Label(row_pos, text="Position:", width=18).pack(side="left")
            self.fg_pos_var = tk.StringVar(value="center")
            pos_combo = ttk.Combobox(row_pos, textvariable=self.fg_pos_var, width=10,
                                     state="readonly", values=list(FG_POSITIONS))
            pos_combo.pack(side="left")
            pos_combo.bind("<<ComboboxSelected>>", lambda e: self.schedule_preview())
            ttk.Label(row_pos, text="(random = docked to a random left/right side per image)",
                      foreground="#888").pack(side="left", padx=8)

            self.fg_scale_var = tk.DoubleVar(value=100)
            self.fg_shadow_var = tk.DoubleVar(value=0)
            self.corner_var = tk.DoubleVar(value=0)
            self._add_slider(tab_fg, "Image scale (%)", self.fg_scale_var, 50, 100)
            self._add_slider(tab_fg, "Drop shadow", self.fg_shadow_var, 0, 100)
            self._add_slider(tab_fg, "Corner radius (px)", self.corner_var, 0, 80)

            # ---- run row ----
            row_run = ttk.Frame(ctrl)
            row_run.pack(fill="x", pady=(6, 0))
            ttk.Button(row_run, text="Output Folder…", command=self.pick_out).pack(side="left")
            self.out_label = ttk.Label(row_run, text="(no output folder)", foreground="#888")
            self.out_label.pack(side="left", padx=8)
            self.process_btn = ttk.Button(row_run, text="Process All",
                                          command=self.process_all)
            self.process_btn.pack(side="right")
            self.progress = ttk.Progressbar(row_run, length=220, mode="determinate")
            self.progress.pack(side="right", padx=8)

            self.status = ttk.Label(root, text="Add images to begin.", anchor="w",
                                    relief="sunken", padding=4)
            self.status.pack(fill="x", side="bottom")

            # width/height changes re-render the preview
            self.w_var.trace_add("write", lambda *a: self.schedule_preview())
            self.h_var.trace_add("write", lambda *a: self.schedule_preview())

            # ---- persistence ----
            self._load_persisted()
            self._refresh_preset_list()
            root.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---- slider helper ----
        def _add_slider(self, parent, label, var, lo, hi):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=18).pack(side="left")
            val_lbl = ttk.Label(row, text=f"{var.get():.0f}", width=4)
            val_lbl.pack(side="right")
            self._slider_labels.append((var, val_lbl))

            def on_move(v):
                val_lbl.config(text=f"{float(v):.0f}")
                self.schedule_preview()

            scale = ttk.Scale(row, from_=lo, to=hi, variable=var,
                              orient="horizontal", command=on_move)
            scale.pack(side="left", fill="x", expand=True, padx=6)

        def _refresh_slider_labels(self):
            for var, lbl in self._slider_labels:
                lbl.config(text=f"{var.get():.0f}")

        # ---- color pickers ----
        def _on_solid_auto(self):
            self.solid_swatch.config(
                state="disabled" if self.solid_auto_var.get() else "normal")
            self.schedule_preview()

        def pick_solid_color(self):
            c = colorchooser.askcolor(color=self.solid_color_val,
                                      title="Solid fill color")
            if c and c[1]:
                self.solid_color_val = c[1]
                self.solid_swatch.config(bg=self.solid_color_val)
                self.schedule_preview()

        def pick_tint_color(self):
            c = colorchooser.askcolor(color=self.tint_color_val, title="Tint color")
            if c and c[1]:
                self.tint_color_val = c[1]
                self.tint_swatch.config(bg=self.tint_color_val)
                self.schedule_preview()

        # ---- settings <-> UI ----
        def current_settings(self) -> Settings | None:
            try:
                w = int(self.w_var.get())
                h = int(self.h_var.get())
                if w < 1 or h < 1:
                    raise ValueError
            except ValueError:
                return None
            return Settings(
                width=w, height=h,
                blur=self.blur_var.get(),
                darken=self.darken_var.get(),
                feather=self.feather_var.get(),
                process_matching=self.process_matching_var.get(),
                fill_style=self.fill_style_var.get(),
                solid_color=("auto" if self.solid_auto_var.get()
                             else self.solid_color_val),
                bg_zoom=self.bg_zoom_var.get() / 100.0,
                saturation=self.saturation_var.get(),
                tint_color=self.tint_color_val,
                tint_strength=self.tint_strength_var.get(),
                vignette=self.vignette_var.get(),
                fg_scale=self.fg_scale_var.get(),
                fg_position=self.fg_pos_var.get(),
                fg_shadow=self.fg_shadow_var.get(),
                fg_corner_radius=int(self.corner_var.get()),
            )

        def apply_effect_dict(self, d: dict):
            """Push a preset/persisted effect dict into the UI controls."""
            s = settings_from_dict({**settings_to_dict(Settings()), **d})
            self.blur_var.set(s.blur)
            self.darken_var.set(s.darken)
            self.feather_var.set(s.feather)
            self.fill_style_var.set(s.fill_style)
            self.solid_auto_var.set(s.solid_color == "auto")
            if s.solid_color != "auto":
                self.solid_color_val = s.solid_color
                self.solid_swatch.config(bg=s.solid_color)
            self._on_solid_auto()
            self.bg_zoom_var.set(s.bg_zoom * 100.0)
            self.saturation_var.set(s.saturation)
            self.tint_color_val = s.tint_color
            self.tint_swatch.config(bg=s.tint_color)
            self.tint_strength_var.set(s.tint_strength)
            self.vignette_var.set(s.vignette)
            self.fg_scale_var.set(s.fg_scale)
            self.fg_pos_var.set(s.fg_position)
            self.fg_shadow_var.set(s.fg_shadow)
            self.corner_var.set(s.fg_corner_radius)
            self._refresh_slider_labels()
            self.schedule_preview()

        def gather_effect_dict(self) -> dict:
            s = self.current_settings()
            if s is None:
                s = Settings()
            full = settings_to_dict(s)
            return {k: full[k] for k in PRESET_FIELDS}

        # ---- presets ----
        def _all_presets(self) -> dict[str, dict]:
            return {**BUILTIN_PRESETS, **self.user_presets}

        def _refresh_preset_list(self):
            self.preset_combo.config(values=list(self._all_presets().keys()))

        def on_preset_selected(self, _evt=None):
            name = self.preset_var.get()
            preset = self._all_presets().get(name)
            if preset:
                self.apply_effect_dict(preset)
                self.status.config(text=f"Preset '{name}' applied.")

        def save_preset(self):
            name = simpledialog.askstring("Save preset", "Preset name:",
                                          parent=self.root)
            if not name:
                return
            name = name.strip()
            if name in BUILTIN_PRESETS:
                messagebox.showinfo("Gaussify",
                                    f"'{name}' is a built-in preset — pick another name.")
                return
            self.user_presets[name] = self.gather_effect_dict()
            self._refresh_preset_list()
            self.preset_var.set(name)
            self._save_persisted()
            self.status.config(text=f"Preset '{name}' saved.")

        def delete_preset(self):
            name = self.preset_var.get()
            if not name:
                return
            if name in BUILTIN_PRESETS:
                messagebox.showinfo("Gaussify", "Built-in presets can't be deleted.")
                return
            if name in self.user_presets:
                del self.user_presets[name]
                self._refresh_preset_list()
                self.preset_var.set("")
                self._save_persisted()
                self.status.config(text=f"Preset '{name}' deleted.")

        # ---- persistence ----
        def _load_persisted(self):
            cfg = load_config()
            self.user_presets = {k: v for k, v in cfg.get("presets", {}).items()
                                 if isinstance(v, dict)}
            last = cfg.get("last")
            if isinstance(last, dict):
                self.apply_effect_dict(last)
                if "width" in last and "height" in last:
                    try:
                        self.set_res(int(last["width"]), int(last["height"]),
                                     from_preset=False)
                    except (TypeError, ValueError):
                        pass
                if last.get("process_matching") is not None:
                    self.process_matching_var.set(bool(last["process_matching"]))
            fmt = cfg.get("format")
            if fmt in ("png", "jpg"):
                self.fmt_var.set(fmt)
            out_dir = cfg.get("output_dir", "")
            if out_dir and os.path.isdir(out_dir):
                self.out_dir = out_dir
                self.out_label.config(text=out_dir, foreground="#000")

        def _save_persisted(self):
            s = self.current_settings()
            cfg = {
                "last": settings_to_dict(s) if s else {},
                "presets": self.user_presets,
                "format": self.fmt_var.get(),
                "output_dir": self.out_dir,
            }
            save_config(cfg)

        def on_close(self):
            self._save_persisted()
            self.root.destroy()

        # ---- resolution handling ----
        def on_res_change(self, _evt=None):
            preset = RES_PRESETS.get(self.res_var.get())
            if preset:
                self.set_res(*preset, from_preset=True)

        def set_res(self, w, h, from_preset=True):
            self.w_var.set(str(w))
            self.h_var.set(str(h))
            if not from_preset:
                for name, val in RES_PRESETS.items():
                    if val == (w, h):
                        self.res_var.set(name)
                        break
                else:
                    self.res_var.set("Custom")
            self.schedule_preview()

        # ---- file list ----
        def add_images(self):
            paths = filedialog.askopenfilenames(
                title="Select images",
                filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff"),
                           ("All files", "*.*")])
            self._add_paths(paths)

        def add_folder(self):
            d = filedialog.askdirectory(title="Select a folder of images")
            if not d:
                return
            paths = [os.path.join(d, f) for f in sorted(os.listdir(d))
                     if f.lower().endswith(SUPPORTED_EXTS)]
            self._add_paths(paths)

        def _add_paths(self, paths):
            added = 0
            for p in paths:
                if p not in self.files:
                    self.files.append(p)
                    self.listbox.insert("end", os.path.basename(p))
                    added += 1
            if added:
                self.status.config(text=f"{len(self.files)} image(s) loaded.")
                if self.listbox.size() and not self.listbox.curselection():
                    self.listbox.selection_set(0)
                    self.on_select()

        def clear_list(self):
            self.files.clear()
            self.listbox.delete(0, "end")
            self.canvas.delete("all")
            self._cur_src = None
            self._cur_path = ""
            self.status.config(text="List cleared.")

        def on_select(self, _evt=None):
            sel = self.listbox.curselection()
            if not sel:
                return
            path = self.files[sel[0]]
            try:
                self._cur_src = Image.open(path).convert("RGB")
                self._cur_path = path
            except Exception as e:
                self._cur_src = None
                self._cur_path = ""
                messagebox.showerror("Gaussify", f"Could not open:\n{path}\n\n{e}")
                return
            self.schedule_preview()

        # ---- preview (debounced) ----
        def schedule_preview(self):
            if self._preview_after is not None:
                self.root.after_cancel(self._preview_after)
            self._preview_after = self.root.after(120, self.render_preview)

        def render_preview(self):
            self._preview_after = None
            if self._cur_src is None:
                return
            s = self.current_settings()
            if s is None:
                self.status.config(text="Enter a valid width/height.")
                return

            cw = max(self.canvas.winfo_width(), 10)
            ch = max(self.canvas.winfo_height(), 10)

            # Render at a downscaled proxy resolution for speed, keeping the
            # target aspect ratio so the preview matches the real output.
            scale = min(cw / s.width, ch / s.height, 1.0)
            pv_w = max(1, int(s.width * scale))
            pv_h = max(1, int(s.height * scale))
            proxy = dataclasses.replace(
                s, width=pv_w, height=pv_h, blur=s.blur * scale,
                fg_corner_radius=max(0, int(round(s.fg_corner_radius * scale))))
            # Stable random dock per file so the preview doesn't jump around
            # while dragging sliders; the batch run re-rolls per image.
            rng = random.Random(self._cur_path)
            try:
                result = render(self._cur_src, proxy, rng)
            except Exception as e:
                self.status.config(text=f"Preview error: {e}")
                return

            self._preview_img = ImageTk.PhotoImage(result)
            self.canvas.delete("all")
            self.canvas.create_image(cw // 2, ch // 2, image=self._preview_img)
            fill = ("fill" if needs_fill(self._cur_src.width, self._cur_src.height, s)
                    or s.fg_scale < 99.5 or s.process_matching
                    else "no fill (matches)")
            self.status.config(
                text=f"{self._cur_src.width}x{self._cur_src.height}  →  "
                     f"{s.width}x{s.height}   [{fill}]")

        # ---- output & batch ----
        def pick_out(self):
            d = filedialog.askdirectory(title="Choose output folder")
            if d:
                self.out_dir = d
                self.out_label.config(text=d, foreground="#000")

        def process_all(self):
            if not self.files:
                messagebox.showinfo("Gaussify", "Add some images first.")
                return
            if not self.out_dir:
                messagebox.showinfo("Gaussify", "Pick an output folder first.")
                return
            s = self.current_settings()
            if s is None:
                messagebox.showerror("Gaussify", "Enter a valid width/height.")
                return
            fmt = self.fmt_var.get()
            self.process_btn.config(state="disabled")
            self.progress.config(maximum=len(self.files), value=0)
            self._save_persisted()

            def worker():
                errors = []
                for i, path in enumerate(self.files, 1):
                    try:
                        process_file(path, self.out_dir, s, fmt=fmt)
                    except Exception as e:
                        errors.append(f"{os.path.basename(path)}: {e}")
                    self.root.after(0, lambda i=i, p=path: self._tick(i, p))
                self.root.after(0, lambda: self._done(errors))

            threading.Thread(target=worker, daemon=True).start()

        def _tick(self, i, path):
            self.progress.config(value=i)
            self.status.config(text=f"Processing {i}/{len(self.files)}: {os.path.basename(path)}")

        def _done(self, errors):
            self.process_btn.config(state="normal")
            if errors:
                messagebox.showwarning(
                    "Gaussify",
                    f"Done with {len(errors)} error(s):\n\n" + "\n".join(errors[:10]))
                self.status.config(text=f"Finished with {len(errors)} error(s).")
            else:
                self.status.config(text=f"Done. {len(self.files)} image(s) written to {self.out_dir}")
                messagebox.showinfo("Gaussify",
                                    f"Processed {len(self.files)} image(s) into:\n{self.out_dir}")

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    return _run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
