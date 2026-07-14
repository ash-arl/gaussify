"""
Gaussify — Blurred-Fill Wallpaper Processor
===========================================

Batch-processes images so they fill a target screen resolution without black
bars. The original image stays crisp; the empty gutters are filled with a
blurred (or solid, or mirrored) backdrop derived from the same image, fading
smoothly into the sharp center.

Run the GUI:      python gaussify.py
Run a self-test:  python gaussify.py --selftest

Only dependency: Pillow (pip install Pillow). Tkinter ships with Python.

Code layout:
  gaussify.py          — this entry point
  gaussify_core.py     — Settings + the image-processing pipeline (GUI-free)
  gaussify_config.py   — settings persistence and presets
  gaussify_gui.py      — the Tkinter application
  gaussify_selftest.py — headless verification of the core
"""

import sys


def main() -> int:
    if "--selftest" in sys.argv:
        from gaussify_selftest import run_selftest
        return run_selftest()
    from gaussify_gui import run_gui
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
