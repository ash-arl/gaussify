"""
Gaussify config — settings serialization, persistence, and presets.

Settings are saved to `gaussify_config.json` next to the scripts so the app
remembers everything between sessions. Presets are named subsets of Settings
(`PRESET_FIELDS`) describing a visual "look" without output geometry.
"""

from __future__ import annotations

import dataclasses
import json
import os

from gaussify_core import Settings

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "gaussify_config.json")

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
