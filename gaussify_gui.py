"""
Gaussify GUI — the Tkinter application.

All image processing lives in `gaussify_core`; persistence and presets in
`gaussify_config`. This module only wires those into widgets: an image list,
a live preview (rendered at a downscaled proxy resolution for speed), tabbed
setting controls, presets, and the batch "Process All" runner.
"""

from __future__ import annotations

import dataclasses
import os
import random
import threading

from PIL import Image

from gaussify_core import SUPPORTED_EXTS, Settings, needs_fill, process_file, render
from gaussify_config import (BUILTIN_PRESETS, PRESET_FIELDS, load_config,
                             save_config, settings_from_dict, settings_to_dict)


def run_gui() -> int:
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
