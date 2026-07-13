"""
Gaussify — Blurred-Fill Wallpaper Processor
===========================================

Batch-processes images so they fill a target screen resolution without black
bars. The original image stays crisp and centered; the empty gutters (left/right
for narrow images, top/bottom for short ones) are filled with a blurred, zoomed
copy of the same image that fades out from the sharp center. A "side darkening"
slider tones the blurred gutters down for a classy look.

Run the GUI:      python gaussify.py
Run a self-test:  python gaussify.py --selftest

Only dependency: Pillow (pip install Pillow). Tkinter ships with Python.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from PIL import Image, ImageEnhance, ImageFilter

# ---------------------------------------------------------------------------
# Core image processing (no GUI — importable & testable on its own)
# ---------------------------------------------------------------------------

SUPPORTED_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff")


@dataclass
class Settings:
    """All knobs that control the blurred-fill effect."""
    width: int = 1920
    height: int = 1080
    blur: float = 60.0          # Gaussian blur radius (px) applied to the fill
    darken: float = 35.0        # 0..100 %, how much to darken the blurred gutters
    feather: float = 12.0       # 0..40 %, crisp->blur fade width as % of gutter
    process_matching: bool = False   # also blur-fill images that already fit
    tolerance: float = 0.02     # aspect match tolerance (fraction of a side)


def needs_fill(img_w: int, img_h: int, s: Settings) -> bool:
    """True if the fitted image leaves visible gutters on the target canvas."""
    fit = min(s.width / img_w, s.height / img_h)
    fitted_w = img_w * fit
    fitted_h = img_h * fit
    gap_x = s.width - fitted_w
    gap_y = s.height - fitted_h
    # A gutter matters only if it is larger than the tolerance band.
    return (gap_x > s.tolerance * s.width) or (gap_y > s.tolerance * s.height)


def _cover_resize(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale + center-crop `img` so it completely covers a w x h box."""
    scale = max(w / img.width, h / img.height)
    new_w = max(1, round(img.width * scale))
    new_h = max(1, round(img.height * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _fit_resize(img: Image.Image, w: int, h: int) -> tuple[Image.Image, int, int]:
    """Scale `img` to fit inside w x h (letterbox). Returns (img, x, y) offset."""
    scale = min(w / img.width, h / img.height)
    new_w = max(1, round(img.width * scale))
    new_h = max(1, round(img.height * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    x = (w - new_w) // 2
    y = (h - new_h) // 2
    return resized, x, y


def _edge_fade_profile(length: int, start: int, span: int, feather_px: int) -> list[int]:
    """
    1-D alpha profile of `length` values: 0 outside [start, start+span), 255 in
    the middle, fading linearly over `feather_px` at each end of the span.
    """
    feather_px = max(0, min(feather_px, span // 2))
    profile = [0] * length
    end = start + span
    for i in range(max(0, start), min(length, end)):
        if feather_px <= 0:
            profile[i] = 255
        else:
            d = min(i - start, end - 1 - i)
            profile[i] = 255 if d >= feather_px else round(255 * d / feather_px)
    return profile


def _horizontal_fade_mask(w: int, h: int, x: int, fg_w: int, feather_px: int) -> Image.Image:
    """
    Alpha mask (L mode) opaque in the middle columns and fading to transparent
    over `feather_px` on the left and right edges. Built as a 1-row gradient and
    stretched to full height (fast — no per-pixel Python loop over the canvas).
    """
    profile = _edge_fade_profile(w, x, fg_w, feather_px)
    row = Image.new("L", (w, 1))
    row.putdata(profile)
    return row.resize((w, h), Image.NEAREST)


def _vertical_fade_mask(w: int, h: int, y: int, fg_h: int, feather_px: int) -> Image.Image:
    """Vertical counterpart of `_horizontal_fade_mask` (top/bottom gutters)."""
    profile = _edge_fade_profile(h, y, fg_h, feather_px)
    col = Image.new("L", (1, h))
    col.putdata(profile)
    return col.resize((w, h), Image.NEAREST)


def _build_fade_mask(w: int, h: int, fx: int, fy: int, fg_w: int, fg_h: int,
                     feather_frac: float) -> Image.Image:
    """
    Build a feathered alpha mask. Chooses the horizontal or vertical fade based
    on which gutter exists. `feather_frac` is 0..1 of the gutter size.
    """
    gutter_x = (w - fg_w) / 2
    gutter_y = (h - fg_h) / 2
    if gutter_x >= gutter_y:
        # Left/right gutters dominate -> fade the vertical seams.
        feather_px = int(feather_frac * max(gutter_x, 1))
        return _horizontal_fade_mask(w, h, fx, fg_w, feather_px)
    else:
        feather_px = int(feather_frac * max(gutter_y, 1))
        return _vertical_fade_mask(w, h, fy, fg_h, feather_px)


def render(img: Image.Image, s: Settings) -> Image.Image:
    """
    Produce the final `s.width` x `s.height` blurred-fill image from `img`.
    If the image already fits and `process_matching` is False, it is fit-scaled
    onto the canvas unchanged (no blur), which for a matching aspect just fills
    the screen exactly.
    """
    img = img.convert("RGB")
    w, h = s.width, s.height

    fill_needed = needs_fill(img.width, img.height, s)

    # Foreground: the crisp, centered, fit-scaled image.
    fg, fx, fy = _fit_resize(img, w, h)

    if not fill_needed and not s.process_matching:
        # Nothing to fill (or user opted out): just place the crisp image on a
        # black canvas. For a truly matching aspect this covers the whole thing.
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(fg, (fx, fy))
        return canvas

    # Background: cover-scaled, blurred, optionally darkened copy.
    bg = _cover_resize(img, w, h)
    if s.blur > 0:
        bg = bg.filter(ImageFilter.GaussianBlur(radius=s.blur))
    if s.darken > 0:
        factor = max(0.0, 1.0 - s.darken / 100.0)
        bg = ImageEnhance.Brightness(bg).enhance(factor)

    # Composite crisp foreground over blurred background with a feathered seam.
    mask = _build_fade_mask(w, h, fx, fy, fg.width, fg.height, s.feather / 100.0)
    fg_full = Image.new("RGB", (w, h), (0, 0, 0))
    fg_full.paste(fg, (fx, fy))
    return Image.composite(fg_full, bg, mask)


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

    # A narrow (portrait-ish) image that will leave left/right gutters on 16:9.
    src = Image.new("RGB", (800, 1000), (200, 30, 30))
    src_path = os.path.join(tmp, "narrow.png")
    src.save(src_path)

    s = Settings(width=1920, height=1080, blur=40, darken=30, feather=15)
    assert needs_fill(800, 1000, s), "narrow image should need filling"

    dst = process_file(src_path, tmp, s, fmt="png")
    out = Image.open(dst)
    assert out.size == (1920, 1080), f"expected 1920x1080, got {out.size}"

    # A gutter pixel (far left, middle row) must NOT be pure black — the blurred
    # fill should have painted something there.
    px = out.getpixel((5, 540))
    assert px != (0, 0, 0), f"gutter pixel is black — fill not applied: {px}"

    # A center pixel should match the crisp source colour closely.
    cx = out.getpixel((960, 540))
    assert abs(cx[0] - 200) < 30, f"center pixel wrong colour: {cx}"

    # A wide 16:9 image should NOT need filling.
    assert not needs_fill(1920, 1080, s), "matching aspect should not need fill"

    print(f"  output size ....... {out.size}  OK")
    print(f"  gutter pixel ...... {px}  (non-black)  OK")
    print(f"  center pixel ...... {cx}  (~source red)  OK")
    print(f"  written to ........ {dst}")
    print("All checks passed.")
    return 0


# ---------------------------------------------------------------------------
# GUI (Tkinter)
# ---------------------------------------------------------------------------

def _run_gui() -> int:
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from PIL import ImageTk

    RES_PRESETS = {
        "1920 x 1080 (1080p)": (1920, 1080),
        "2560 x 1440 (1440p)": (2560, 1440),
        "3840 x 2160 (4K)": (3840, 2160),
        "Custom": None,
    }

    def detect_screen(widget) -> tuple[int, int]:
        try:
            return widget.winfo_screenwidth(), widget.winfo_screenheight()
        except Exception:
            return 1920, 1080

    class App:
        def __init__(self, root: tk.Tk):
            self.root = root
            root.title("Gaussify — Blurred-Fill Wallpaper Processor")
            root.geometry("1120x720")
            root.minsize(940, 620)

            self.files: list[str] = []
            self.out_dir: str = ""
            self._preview_img = None          # keep a ref so Tk doesn't GC it
            self._preview_after = None        # debounce handle
            self._cur_src: Image.Image | None = None

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

            # Resolution row
            row1 = ttk.Frame(ctrl)
            row1.pack(fill="x", pady=2)
            ttk.Label(row1, text="Resolution:").pack(side="left")
            self.res_var = tk.StringVar(value="1920 x 1080 (1080p)")
            res_combo = ttk.Combobox(row1, textvariable=self.res_var, width=20,
                                     state="readonly", values=list(RES_PRESETS.keys()))
            res_combo.pack(side="left", padx=4)
            res_combo.bind("<<ComboboxSelected>>", self.on_res_change)
            ttk.Label(row1, text="W:").pack(side="left")
            self.w_var = tk.StringVar(value=str(scr_w if (scr_w, scr_h) else 1920))
            self.w_entry = ttk.Entry(row1, textvariable=self.w_var, width=6)
            self.w_entry.pack(side="left")
            ttk.Label(row1, text="H:").pack(side="left")
            self.h_var = tk.StringVar(value=str(scr_h))
            self.h_entry = ttk.Entry(row1, textvariable=self.h_var, width=6)
            self.h_entry.pack(side="left")
            ttk.Button(row1, text="Use my screen",
                       command=lambda: self.set_res(scr_w, scr_h)).pack(side="left", padx=8)
            # start on the detected screen resolution
            self.set_res(scr_w, scr_h, from_preset=False)
            self.w_var.trace_add("write", lambda *a: self.schedule_preview())
            self.h_var.trace_add("write", lambda *a: self.schedule_preview())

            # Sliders row
            self.blur_var = tk.DoubleVar(value=60)
            self.darken_var = tk.DoubleVar(value=35)
            self.feather_var = tk.DoubleVar(value=12)
            self._add_slider(ctrl, "Blur strength", self.blur_var, 0, 150)
            self._add_slider(ctrl, "Side darkening (%)", self.darken_var, 0, 100)
            self._add_slider(ctrl, "Feather / fade (%)", self.feather_var, 0, 40)

            # Output row
            row_out = ttk.Frame(ctrl)
            row_out.pack(fill="x", pady=4)
            self.process_matching_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(row_out, text="Also process already-matching images",
                            variable=self.process_matching_var,
                            command=self.schedule_preview).pack(side="left")
            ttk.Label(row_out, text="Format:").pack(side="left", padx=(12, 2))
            self.fmt_var = tk.StringVar(value="png")
            ttk.Combobox(row_out, textvariable=self.fmt_var, width=5, state="readonly",
                         values=["png", "jpg"]).pack(side="left")

            row_run = ttk.Frame(ctrl)
            row_run.pack(fill="x", pady=4)
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

        # ---- slider helper ----
        def _add_slider(self, parent, label, var, lo, hi):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=18).pack(side="left")
            val_lbl = ttk.Label(row, text=f"{var.get():.0f}", width=4)
            val_lbl.pack(side="right")

            def on_move(v):
                val_lbl.config(text=f"{float(v):.0f}")
                self.schedule_preview()

            scale = ttk.Scale(row, from_=lo, to=hi, variable=var,
                              orient="horizontal", command=on_move)
            scale.pack(side="left", fill="x", expand=True, padx=6)

        # ---- settings gathering ----
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
            )

        # ---- resolution handling ----
        def on_res_change(self, _evt=None):
            preset = RES_PRESETS.get(self.res_var.get())
            if preset:
                self.set_res(*preset, from_preset=True)

        def set_res(self, w, h, from_preset=True):
            self.w_var.set(str(w))
            self.h_var.set(str(h))
            if not from_preset:
                # match a preset name if it exists, else Custom
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
            self.status.config(text="List cleared.")

        def on_select(self, _evt=None):
            sel = self.listbox.curselection()
            if not sel:
                return
            path = self.files[sel[0]]
            try:
                self._cur_src = Image.open(path).convert("RGB")
            except Exception as e:
                self._cur_src = None
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
            proxy = Settings(width=pv_w, height=pv_h,
                             blur=s.blur * scale, darken=s.darken,
                             feather=s.feather,
                             process_matching=s.process_matching)
            try:
                result = render(self._cur_src, proxy)
            except Exception as e:
                self.status.config(text=f"Preview error: {e}")
                return

            self._preview_img = ImageTk.PhotoImage(result)
            self.canvas.delete("all")
            self.canvas.create_image(cw // 2, ch // 2, image=self._preview_img)
            fill = "fill" if needs_fill(self._cur_src.width, self._cur_src.height, s) else "no fill (matches)"
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
