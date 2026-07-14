"""
Gaussify core — the image-processing engine.

Everything here is GUI-free and importable on its own: the `Settings`
dataclass, the fill/feather/compositing pipeline, and `process_file()` for
batch use. See `render()` for the top-level algorithm.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

SUPPORTED_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")


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
