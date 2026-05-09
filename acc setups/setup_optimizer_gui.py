"""
ACC Setup Optimizer — Tkinter GUI
---------------------------------
Interactive track map. Left-click a corner marker to select it. Pick the
issue + phase on the right, then "Apply Selected Fix". MoTeC CSV is
optional — when loaded, the validation result is shown next to the issue.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from setup_optimizer import (
    SetupManager,
    TelemetryAnalyzer,
    TRACK_MAP,
    RECOMMENDATIONS,
)


# ---------------------------------------------------------------------------
# Track-map images (Track Maps/ folder)
# ---------------------------------------------------------------------------
# Maps a track key → base filename (without extension) inside Track Maps/.
# If a .png exists it's used; otherwise we convert .avif → .png with macOS
# `sips` once and cache the result.
TRACK_MAPS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "Track Maps"
)
# All 24 ACC tracks → base filename in Track Maps/ (no extension).
# Source images can be .png, .jpg, .jpeg, .webp, or .avif — non-PNG sources
# are auto-converted to PNG via macOS `sips` and cached.
TRACK_IMAGES: dict[str, str] = {
    "barcelona":         "barcelona-track-map",
    "bathurst":          "bathurst-track-map",
    "brands hatch":      "brands-hatch-track-map",
    "cota":              "COTA-circuit-of-the-americas-track-map",
    "donington":         "donington-park-track-map",
    "hungaroring":       "hungaroring-track-map",
    "imola":             "imola-track-map",
    "indianapolis":      "indianapolis-track-map",
    "kyalami":           "kylami-track-map",
    "laguna seca":       "laguna-seca-track-map",
    "misano":            "misano-track-map",
    "monza":             "monza-track-map",
    "nurburgring":       "nurburgring-track-map",
    "oulton park":       "oulton-park-track-map",
    "paul ricard":       "paul-ricard-track-map",
    "red bull ring":     "red-bull-ring-track-map",
    "valencia":          "ricard-tormo-track-map",
    "silverstone":       "silverstone-track-map",
    "snetterton":        "snetterton-track-map",
    "spa":               "spa-francorchamps-track-map",
    "suzuka":            "suzuka-track-map",
    "watkins glen":      "watkins-glen-track-map",
    "zandvoort":         "zandvoort-track-map",
    "zolder":            "zolder-track-map",
}

# Source extensions to look for, in priority order.
_SOURCE_EXTS = (".avif", ".webp", ".jpg", ".jpeg")


def _convert_image_pillow(src: str, dst: str, target_width: int) -> bool:
    """Convert any image to PNG via Pillow. Returns True on success."""
    try:
        from PIL import Image    # noqa: PLC0415 — optional dep
    except ImportError:
        return False
    try:
        with Image.open(src) as im:
            if im.width != target_width:
                ratio = target_width / im.width
                new_h = max(1, int(im.height * ratio))
                im = im.resize((target_width, new_h), Image.LANCZOS)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA")
            im.save(dst, "PNG")
        return True
    except Exception:
        return False


def _convert_image_sips(src: str, dst: str, target_width: int) -> bool:
    """Convert via macOS `sips`. No-op on non-macOS hosts."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["sips", "-s", "format", "png",
             "--resampleWidth", str(target_width), src, "--out", dst],
            check=True, capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _convert_image_magick(src: str, dst: str, target_width: int) -> bool:
    """Convert via ImageMagick (`magick` on Win/Linux, sometimes `convert`)."""
    for cmd in ("magick", "convert"):
        try:
            subprocess.run(
                [cmd, src, "-resize", f"{target_width}x", dst],
                check=True, capture_output=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    return False


def _ensure_png(base_name: str, target_width: int = 720) -> str | None:
    """Return the PNG path for a track image, converting from any supported
    source format (AVIF/WEBP/JPG/JPEG) if needed.

    Conversion backends are tried in order: Pillow (cross-platform),
    macOS ``sips``, ImageMagick ``magick``/``convert``. Returns ``None`` if
    no source image is available or every backend failed.

    On Windows the recommended setup is ``pip install pillow``; the bundled
    PNG cache works without it as long as the source images don't change.
    """
    png = os.path.join(TRACK_MAPS_DIR, f"{base_name}.png")
    have_png = os.path.isfile(png)

    src: str | None = None
    src_mtime = -1.0
    for ext in _SOURCE_EXTS:
        candidate = os.path.join(TRACK_MAPS_DIR, f"{base_name}{ext}")
        if os.path.isfile(candidate):
            m = os.path.getmtime(candidate)
            if m > src_mtime:
                src, src_mtime = candidate, m

    if have_png and (src is None or os.path.getmtime(png) >= src_mtime):
        return png
    if src is None:
        return png if have_png else None

    for backend in (_convert_image_pillow,
                    _convert_image_sips,
                    _convert_image_magick):
        if backend(src, png, target_width):
            return png
    return png if have_png else None


# ---------------------------------------------------------------------------
# Track layouts (normalised 0–1 coordinates, scaled to the canvas at draw time)
# ---------------------------------------------------------------------------
# Corner positions are normalised (0–1) over the IMAGE bounds shown by the
# canvas (the PNG cached by _ensure_png at width=720). All 24 ACC tracks
# have at least 4–6 markers covering the named corners that matter most for
# setup work. Positions are eyeballed from the minimalist outline maps in
# Track Maps/; tweak the numbers in this dict to nudge a marker.
TRACK_LAYOUTS: dict[str, dict] = {
    "barcelona": {
        "corners": {
            "T1 Elf":         (0.16, 0.40),
            "T3 Repsol":      (0.30, 0.30),
            "T5 Seat":        (0.20, 0.45),
            "T9 Campsa":      (0.50, 0.30),
            "T10 La Caixa":   (0.55, 0.40),
            "T14 New Holland": (0.62, 0.55),
        },
    },
    "bathurst": {
        "corners": {
            "T1 Hell Corner":   (0.10, 0.45),
            "T2 The Cutting":   (0.30, 0.30),
            "T7 Skyline":       (0.55, 0.20),
            "T15 Forrest Elbow": (0.78, 0.55),
            "T19 The Chase":    (0.85, 0.78),
            "T22 Murray's":     (0.18, 0.55),
        },
    },
    "brands hatch": {
        "corners": {
            "T1 Paddock Hill":  (0.20, 0.30),
            "T2 Druids":        (0.18, 0.20),
            "T3 Graham Hill":   (0.32, 0.40),
            "T6 Surtees":       (0.55, 0.55),
            "T9 Stirlings":     (0.72, 0.30),
            "T10 Clearways":    (0.40, 0.62),
        },
    },
    "cota": {
        "corners": {
            "T1 Hairpin":   (0.42, 0.10),
            "T3-6 Esses":   (0.55, 0.28),
            "T11 Hairpin":  (0.55, 0.55),
            "T12":          (0.55, 0.70),
            "T15":          (0.40, 0.78),
            "T20 Final":    (0.22, 0.68),
        },
    },
    "donington": {
        "corners": {
            "T1 Redgate":          (0.55, 0.15),
            "T2 Craner Curves":    (0.62, 0.25),
            "T3 Old Hairpin":      (0.40, 0.40),
            "T6 Coppice":          (0.30, 0.30),
            "T9 Melbourne Hairpin": (0.45, 0.78),
            "T11 Goddards":        (0.50, 0.62),
        },
    },
    "hungaroring": {
        "corners": {
            "T1":            (0.62, 0.22),
            "T2":            (0.55, 0.18),
            "T4":            (0.40, 0.38),
            "T6-7 Chicane":  (0.45, 0.55),
            "T11":           (0.70, 0.62),
            "T14":           (0.55, 0.85),
        },
    },
    "imola": {
        "corners": {
            "T2 Tamburello":    (0.40, 0.55),
            "T4 Villeneuve":    (0.30, 0.55),
            "T7 Tosa":          (0.18, 0.55),
            "T9 Piratella":     (0.35, 0.30),
            "T11 Acque Minerali": (0.55, 0.20),
            "T14 Variante Alta": (0.75, 0.30),
            "T18 Rivazza":      (0.55, 0.65),
        },
    },
    "indianapolis": {
        "corners": {
            "T1":          (0.92, 0.55),
            "T2":          (0.85, 0.30),
            "T7 Chicane":  (0.50, 0.30),
            "T11":         (0.30, 0.45),
            "T13":         (0.20, 0.60),
            "T16 Final":   (0.18, 0.78),
        },
    },
    "kyalami": {
        "corners": {
            "T1 Crowthorne":   (0.55, 0.10),
            "T2 Jukskei":      (0.62, 0.18),
            "T7 Mineshaft":    (0.85, 0.25),
            "T8 Sunset":       (0.45, 0.55),
            "T9 Clubhouse":    (0.20, 0.65),
            "T13":             (0.30, 0.45),
        },
    },
    "laguna seca": {
        "corners": {
            "T2 Andretti":   (0.30, 0.65),
            "T3":            (0.22, 0.35),
            "T5":            (0.45, 0.18),
            "T6":            (0.62, 0.20),
            "T8 Corkscrew":  (0.50, 0.30),
            "T11 Final":     (0.85, 0.85),
        },
    },
    "misano": {
        "corners": {
            "T1":              (0.20, 0.40),
            "T4 Quercia":      (0.40, 0.22),
            "T8 Curvone":      (0.65, 0.20),
            "T10 Variante":    (0.55, 0.40),
            "T13 Carro":       (0.60, 0.55),
            "T16 Final":       (0.85, 0.85),
        },
    },
    "monza": {
        # Corner names match TRACK_MAP entries in setup_optimizer.py so
        # auto-detected distances map cleanly onto these markers.
        "corners": {
            "T1 Variante del Rettifilo": (0.18, 0.20),
            "T4 Variante della Roggia":  (0.40, 0.45),
            "T6 Lesmo 1":                (0.20, 0.45),
            "T7 Lesmo 2":                (0.25, 0.62),
            "T8 Variante Ascari":        (0.45, 0.55),
            "T11 Parabolica":            (0.75, 0.85),
        },
    },
    "nurburgring": {
        "corners": {
            "T1 Castrol-S":       (0.55, 0.20),
            "T4 Mercedes Arena":  (0.40, 0.40),
            "T6 Dunlop-Kehre":    (0.82, 0.50),
            "T8 NGK":             (0.65, 0.55),
            "T10 Schumacher-S":   (0.50, 0.55),
            "T15 Coca-Cola":      (0.40, 0.55),
        },
    },
    "oulton park": {
        "corners": {
            "T1 Old Hall":     (0.85, 0.45),
            "T3 Cascades":     (0.72, 0.20),
            "T6 Island Bend":  (0.45, 0.10),
            "T9 Knickerbrook": (0.20, 0.45),
            "T12 Druids":      (0.20, 0.65),
            "T14 Lodge":       (0.40, 0.55),
        },
    },
    "paul ricard": {
        "corners": {
            "T1 Verriere":         (0.72, 0.55),
            "T3 Sainte-Beaume":    (0.55, 0.30),
            "T5 Mistral Chicane":  (0.40, 0.42),
            "T8 Signes":           (0.10, 0.55),
            "T10 Beausset":        (0.18, 0.45),
            "T13 Bendor":          (0.55, 0.55),
        },
    },
    "red bull ring": {
        "corners": {
            "T1 Castrol":         (0.85, 0.85),
            "T3 Remus":           (0.40, 0.18),
            "T4 Schlossgold":     (0.30, 0.30),
            "T6 Wuerth":          (0.50, 0.50),
            "T7 Rauch":           (0.70, 0.55),
            "T9 Red Bull Mobile": (0.85, 0.55),
        },
    },
    "valencia": {
        "corners": {
            "T1":             (0.85, 0.30),
            "T4":             (0.60, 0.22),
            "T8":             (0.40, 0.18),
            "T10":            (0.30, 0.30),
            "T13":            (0.40, 0.55),
            "T14 Final":      (0.20, 0.55),
        },
    },
    "silverstone": {
        "corners": {
            "T1 Abbey":            (0.85, 0.20),
            "T3 Village":          (0.85, 0.45),
            "T6 Brooklands":       (0.78, 0.62),
            "T7 Luffield":         (0.70, 0.62),
            "T9 Copse":            (0.55, 0.40),
            "T10-12 Maggotts/Becketts": (0.40, 0.30),
            "T15 Stowe":           (0.30, 0.65),
            "T18 Club":            (0.60, 0.78),
        },
    },
    "snetterton": {
        "corners": {
            "T1 Riches":      (0.65, 0.20),
            "T3 Montreal":    (0.30, 0.40),
            "T6 Bomb Hole":   (0.10, 0.40),
            "T8 Coram":       (0.18, 0.20),
            "T10 Murrays":    (0.85, 0.30),
        },
    },
    "spa": {
        # Names match TRACK_MAP['spa'] for telemetry distance lookup.
        "corners": {
            "T1 La Source":    (0.15, 0.65),
            "T5 Les Combes":   (0.62, 0.20),
            "T8 Pouhon":       (0.78, 0.40),
            "T13 Stavelot":    (0.85, 0.62),
            "T15 Bus Stop":    (0.28, 0.78),
        },
    },
    "suzuka": {
        "corners": {
            "T1":              (0.30, 0.85),
            "T3-7 Esses":      (0.45, 0.65),
            "T8 Dunlop":       (0.55, 0.50),
            "T11 Hairpin":     (0.85, 0.30),
            "T13 Spoon":       (0.55, 0.10),
            "T15 130R":        (0.45, 0.30),
            "T16 Casio":       (0.32, 0.55),
        },
    },
    "watkins glen": {
        "corners": {
            "T1 The 90":         (0.85, 0.30),
            "T2 Esses":          (0.55, 0.18),
            "T5 Toe of Boot":    (0.30, 0.20),
            "T6 Heel of Boot":   (0.18, 0.55),
            "T8 Chicane":        (0.45, 0.55),
            "T11 Final":         (0.55, 0.62),
        },
    },
    "zandvoort": {
        "corners": {
            "T1 Tarzanbocht":     (0.30, 0.85),
            "T3 Hugenholtz":      (0.32, 0.62),
            "T7 Scheivlak":       (0.55, 0.30),
            "T9 Mast":            (0.40, 0.30),
            "T11 Slotemaker":     (0.50, 0.65),
            "T13 Arie Luyendyk":  (0.65, 0.65),
        },
    },
    "zolder": {
        "corners": {
            "T1 Sterrewacht":  (0.45, 0.55),
            "T4 Lucienbocht":  (0.65, 0.32),
            "T7 Sacrament":    (0.85, 0.20),
            "T8 Kanaalbocht":  (0.30, 0.55),
            "T10 Bianchi":     (0.20, 0.78),
            "T11 Final":       (0.20, 0.58),
        },
    },
}


# ---------------------------------------------------------------------------
# Corner-position overrides — user can drag markers in Edit Mode and the
# results are persisted here so they survive restarts.
# ---------------------------------------------------------------------------
CORNER_OVERRIDES_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "corner_positions.json"
)


def _load_corner_overrides() -> dict:
    """Read the user's saved corner positions. Returns {} on any error."""
    if not os.path.isfile(CORNER_OVERRIDES_FILE):
        return {}
    try:
        with open(CORNER_OVERRIDES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_corner_overrides(overrides: dict) -> None:
    """Write user-edited corner positions to disk."""
    try:
        with open(CORNER_OVERRIDES_FILE, "w", encoding="utf-8") as fh:
            json.dump(overrides, fh, indent=2, sort_keys=True)
    except OSError as e:
        print(f"[warn] could not save {CORNER_OVERRIDES_FILE}: {e}")


def _apply_corner_overrides() -> None:
    """Merge user-saved positions on top of the defaults in TRACK_LAYOUTS."""
    overrides = _load_corner_overrides()
    for track, corners in overrides.items():
        if not isinstance(corners, dict) or track not in TRACK_LAYOUTS:
            continue
        layout_corners = TRACK_LAYOUTS[track].setdefault("corners", {})
        for name, pos in corners.items():
            if (isinstance(pos, (list, tuple)) and len(pos) == 2
                    and all(isinstance(v, (int, float)) for v in pos)):
                layout_corners[name] = (float(pos[0]), float(pos[1]))


# Apply at import so the canvas reads the user's positions on first paint.
_apply_corner_overrides()


# ---------------------------------------------------------------------------
# Track map canvas
# ---------------------------------------------------------------------------
class TrackMapCanvas(tk.Canvas):
    """Draws a track outline and clickable corner markers."""

    BG = "#101418"
    OUTLINE = "#5cd0ff"
    MARKER = "#f0c040"
    MARKER_HOVER = "#ffffff"
    MARKER_SELECTED = "#ff5050"
    LABEL = "#e0e0e0"

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=self.BG, highlightthickness=0, **kw)
        self._track: str | None = None
        self._selected_name: str | None = None
        # PhotoImage cache (one per track + per subsample factor) so Tk
        # doesn't garbage-collect them out from under the canvas.
        self._image_cache: dict[str, tk.PhotoImage] = {}
        self._image_box: tuple[int, int, int, int] | None = None
        # Per-corner issue annotations (set by TelemetryAnalyzer results) —
        # corner_name → "Understeer"/"Oversteer"/"Bottoming"/None. Drives
        # the marker fill colour so the user can see flagged corners.
        self._issue_by_corner: dict[str, str] = {}
        self.bind("<Configure>", lambda _e: self._redraw())

    # ---- image loading ----
    def _load_track_image(self, track: str, canvas_w: int, canvas_h: int) -> tuple[tk.PhotoImage, int, int] | None:
        """Return (PhotoImage, display_width, display_height) for the given
        track, scaled to fit the canvas. None if no image is available."""
        base = TRACK_IMAGES.get(track)
        if not base:
            return None
        png_path = _ensure_png(base)
        if not png_path or not os.path.isfile(png_path):
            return None

        # Native PhotoImage at full resolution (cached).
        native_key = f"{track}:native"
        if native_key not in self._image_cache:
            try:
                self._image_cache[native_key] = tk.PhotoImage(file=png_path)
            except tk.TclError:
                return None
        native = self._image_cache[native_key]
        iw, ih = native.width(), native.height()

        # Tk's PhotoImage only supports integer subsample/zoom. Find the
        # smallest subsample factor that fits the canvas.
        factor = 1
        while iw // factor > canvas_w or ih // factor > canvas_h:
            factor += 1
        if factor == 1:
            return native, iw, ih
        sub_key = f"{track}:sub{factor}"
        if sub_key not in self._image_cache:
            self._image_cache[sub_key] = native.subsample(factor, factor)
        scaled = self._image_cache[sub_key]
        return scaled, scaled.width(), scaled.height()

    def set_track(self, track_name: str) -> None:
        self._track = track_name
        self._selected_name = None
        self._redraw()

    def select_corner(self, name: str) -> None:
        self._selected_name = name
        self._redraw()

    # ---- drawing ----
    def _redraw(self) -> None:
        self.delete("all")
        self._image_box = None
        if not self._track:
            self.create_text(
                self.winfo_width() // 2, self.winfo_height() // 2,
                text="Pick a track to see the layout",
                fill=self.LABEL, font=("Helvetica", 12, "italic"),
            )
            return

        layout = TRACK_LAYOUTS[self._track]
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50 or h < 50:
            return

        # Try to use a real track-map image; fall back to drawn outline.
        loaded = self._load_track_image(self._track, w, h)
        if loaded is not None:
            img, diw, dih = loaded
            ix = (w - diw) // 2
            iy = (h - dih) // 2
            self.create_image(ix, iy, image=img, anchor="nw")
            self._image_box = (ix, iy, diw, dih)
            self.create_text(20, 14, text=self._track.upper(),
                             anchor="w", fill="#ffffff",
                             font=("Helvetica", 13, "bold"))
        else:
            outline = layout.get("outline")
            if outline:
                pad = 30
                sx, sy = w - 2 * pad, h - 2 * pad
                pts = []
                for nx, ny in outline:
                    pts.extend([pad + nx * sx, pad + ny * sy])
                self.create_line(*pts, fill=self.OUTLINE, width=4,
                                 smooth=True, capstyle=tk.ROUND,
                                 joinstyle=tk.ROUND)
            else:
                self.create_text(
                    w // 2, h // 2,
                    text=f"(no map image found for {self._track})",
                    fill="#ff7070", font=("Helvetica", 12, "italic"),
                )
            self.create_text(30, 18, text=self._track.upper(),
                             anchor="w", fill=self.LABEL,
                             font=("Helvetica", 13, "bold"))

        # Corner markers intentionally NOT drawn — the map is purely a
        # decorative reference now. Issue annotations are still tracked in
        # `_issue_by_corner` (set by the analyzer) but no longer rendered;
        # the issues Treeview in the right panel is the source of truth.

    # ---- public API used by the analysis pipeline ----
    def set_issue_annotations(self, by_corner: dict[str, str]) -> None:
        """Colour each marker by detected handling issue.
        Pass {} to clear annotations."""
        self._issue_by_corner = dict(by_corner)
        self._redraw()


# ---------------------------------------------------------------------------
# Telemetry chart — Speed / Brake / Throttle / G_Lat / Steer vs Distance.
# Click and drag to select a distance range for custom-corner analysis.
# ---------------------------------------------------------------------------
class TelemetryChart(tk.Canvas):
    """Line plot of one channel vs Distance with click-and-drag region select.

    The chart calls ``on_range_change(start_m, end_m)`` whenever the user
    finishes a drag — the GUI uses that to populate the start/end Entry
    fields and to enable the Analyze button.
    """

    BG = "#101418"
    AXIS = "#3a3f48"
    LINE = "#5cd0ff"
    SEL_OUTLINE = "#ffaa33"
    LABEL = "#aaaaaa"

    SUPPORTED_CHANNELS = ("Speed", "Brake", "Throttle", "G_Lat", "Steer")

    def __init__(self, parent, on_range_change=None, **kw):
        super().__init__(parent, bg=self.BG, highlightthickness=0, **kw)
        self.on_range_change = on_range_change
        self._df = None
        self._channel = "Speed"
        self._x_min = 0.0
        self._x_max = 1.0
        self._y_min = 0.0
        self._y_max = 1.0
        self._sel_start_d: float | None = None
        self._sel_end_d: float | None = None
        self._dragging = False
        self._plot_box = (40, 12, 100, 80)
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ---- public API ----
    def set_data(self, df, channel: str | None = None) -> None:
        """Hand the chart a slice of telemetry to plot. ``df`` may be the
        full ``tel.df`` or a single-lap slice."""
        self._df = df
        if channel and channel in self.SUPPORTED_CHANNELS:
            self._channel = channel
        self._compute_bounds()
        self._redraw()

    def set_channel(self, channel: str) -> None:
        if channel in self.SUPPORTED_CHANNELS:
            self._channel = channel
            self._compute_bounds()
            self._redraw()

    def set_selection(self, start_d: float | None, end_d: float | None) -> None:
        """Programmatically set the highlighted range."""
        self._sel_start_d = start_d
        self._sel_end_d = end_d
        self._redraw()

    def get_selection(self) -> tuple[float | None, float | None]:
        if self._sel_start_d is None or self._sel_end_d is None:
            return None, None
        return min(self._sel_start_d, self._sel_end_d), max(self._sel_start_d, self._sel_end_d)

    def clear_selection(self) -> None:
        self.set_selection(None, None)

    # ---- math helpers ----
    def _compute_bounds(self) -> None:
        if self._df is None or "Distance" not in self._df.columns:
            return
        self._x_min = float(self._df["Distance"].min())
        self._x_max = float(self._df["Distance"].max())
        if self._channel in self._df.columns:
            col = self._df[self._channel]
            if self._channel == "Steer" or self._channel == "G_Lat":
                # symmetric around zero so direction is visible
                lim = max(abs(float(col.min())), abs(float(col.max())), 0.1)
                self._y_min, self._y_max = -lim, lim
            else:
                self._y_min = float(col.min())
                self._y_max = float(col.max())
                if self._y_max - self._y_min < 1e-3:
                    self._y_max = self._y_min + 1.0

    def _x_to_dist(self, x: float) -> float:
        l, _, r, _ = self._plot_box
        if r <= l:
            return self._x_min
        frac = max(0.0, min(1.0, (x - l) / (r - l)))
        return self._x_min + frac * (self._x_max - self._x_min)

    def _dist_to_x(self, d: float) -> float:
        l, _, r, _ = self._plot_box
        if self._x_max <= self._x_min:
            return l
        frac = (d - self._x_min) / (self._x_max - self._x_min)
        frac = max(0.0, min(1.0, frac))
        return l + frac * (r - l)

    def _y_to_canvas(self, y: float) -> float:
        _, t, _, b = self._plot_box
        if self._y_max <= self._y_min:
            return b
        frac = (y - self._y_min) / (self._y_max - self._y_min)
        return b - frac * (b - t)

    # ---- drawing ----
    def _redraw(self) -> None:
        self.delete("all")
        w = max(0, int(self.winfo_width()))
        h = max(0, int(self.winfo_height()))
        if w < 80 or h < 60:
            return
        l, t, r, b = 44, 14, w - 12, h - 22
        self._plot_box = (l, t, r, b)

        self.create_rectangle(l, t, r, b, outline=self.AXIS, width=1)

        if self._df is None or self._channel not in self._df.columns:
            self.create_text((l + r) // 2, (t + b) // 2,
                             text="(load a CSV and run analysis to see the trace)",
                             fill="#666", font=("Helvetica", 10, "italic"))
            return

        # Plot the line — sample to canvas pixel resolution to keep redraw cheap.
        df = self._df
        n = len(df)
        target = max(2, r - l)
        step = max(1, n // target)
        d_arr = df["Distance"].to_numpy()
        v_arr = df[self._channel].to_numpy()

        pts: list[float] = []
        for i in range(0, n, step):
            v = v_arr[i]
            if v != v:    # NaN guard
                continue
            pts.append(self._dist_to_x(d_arr[i]))
            pts.append(self._y_to_canvas(v))
        if len(pts) >= 4:
            self.create_line(*pts, fill=self.LINE, width=1)

        # Selection rectangle — draw under the trace would require ordering
        # tricks; drawing on top is cheaper and still readable.
        if self._sel_start_d is not None and self._sel_end_d is not None:
            x1 = self._dist_to_x(min(self._sel_start_d, self._sel_end_d))
            x2 = self._dist_to_x(max(self._sel_start_d, self._sel_end_d))
            self.create_rectangle(x1, t, x2, b, fill=self.SEL_OUTLINE,
                                  stipple="gray25", outline=self.SEL_OUTLINE,
                                  width=1)
            label = (f"{min(self._sel_start_d, self._sel_end_d):.0f} – "
                     f"{max(self._sel_start_d, self._sel_end_d):.0f} m")
            self.create_text((x1 + x2) // 2, t + 8, text=label,
                             fill=self.SEL_OUTLINE,
                             font=("Helvetica", 9, "bold"))

        # Axis labels
        self.create_text((l + r) // 2, t + 1, text=self._channel,
                         anchor="n", fill="#bbb",
                         font=("Helvetica", 9, "bold"))
        self.create_text(l, b + 11, text=f"{self._x_min:.0f} m",
                         anchor="w", fill=self.LABEL, font=("Helvetica", 8))
        self.create_text(r, b + 11, text=f"{self._x_max:.0f} m",
                         anchor="e", fill=self.LABEL, font=("Helvetica", 8))
        self.create_text(l - 4, t, text=f"{self._y_max:.1f}",
                         anchor="ne", fill=self.LABEL, font=("Helvetica", 8))
        self.create_text(l - 4, b, text=f"{self._y_min:.1f}",
                         anchor="se", fill=self.LABEL, font=("Helvetica", 8))

    # ---- mouse handlers ----
    def _on_press(self, ev: tk.Event) -> None:
        if self._df is None:
            return
        l, t, r, b = self._plot_box
        if not (l <= ev.x <= r and t <= ev.y <= b):
            return
        self._dragging = True
        self._sel_start_d = self._x_to_dist(ev.x)
        self._sel_end_d = self._sel_start_d
        self._redraw()

    def _on_drag(self, ev: tk.Event) -> None:
        if not self._dragging:
            return
        self._sel_end_d = self._x_to_dist(ev.x)
        self._redraw()

    def _on_release(self, ev: tk.Event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._sel_end_d = self._x_to_dist(ev.x)
        self._redraw()
        if self.on_range_change and self._sel_start_d is not None:
            s = min(self._sel_start_d, self._sel_end_d)
            e = max(self._sel_start_d, self._sel_end_d)
            if e - s > 5:    # ignore zero-width clicks
                self.on_range_change(s, e)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
class SetupOptimizerApp:
    DEFAULT_TEMP = "20"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("ACC Setup Optimizer — Driver61 + MoTeC")
        root.geometry("1500x780")
        root.configure(bg="#1c1f24")

        self.mgr: SetupManager | None = None
        self.tel: TelemetryAnalyzer | None = None
        self.current_track: str | None = None
        self.current_corner: str | None = None

        self._setup_styles()
        self._build_layout()
        self._set_status("Load a setup JSON to begin.")

    # ---- style ----
    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#1c1f24")
        style.configure("TLabel", background="#1c1f24", foreground="#dcdcdc",
                        font=("Helvetica", 11))
        style.configure("Header.TLabel", font=("Helvetica", 12, "bold"),
                        foreground="#5cd0ff")
        style.configure("TButton", padding=6)
        style.configure("Accent.TButton", padding=8, font=("Helvetica", 11, "bold"))
        style.configure("TEntry", fieldbackground="#262a30", foreground="#fff")
        style.configure("TCombobox", fieldbackground="#262a30", foreground="#000")
        style.configure("TRadiobutton", background="#1c1f24", foreground="#dcdcdc")

    # ---- layout ----
    def _build_layout(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=0, minsize=290)
        root.columnconfigure(1, weight=1)
        root.columnconfigure(2, weight=0, minsize=320)
        root.rowconfigure(0, weight=1)
        root.rowconfigure(1, weight=0)

        self.left = ttk.Frame(root, padding=12)
        self.left.grid(row=0, column=0, sticky="nsew")
        self._build_left(self.left)

        self.center = ttk.Frame(root, padding=12)
        self.center.grid(row=0, column=1, sticky="nsew")
        self._build_center(self.center)

        self.right = ttk.Frame(root, padding=12)
        self.right.grid(row=0, column=2, sticky="nsew")
        self._build_right(self.right)

        self.status_var = tk.StringVar(value="")
        status = ttk.Label(root, textvariable=self.status_var, anchor="w",
                           padding=(12, 6))
        status.grid(row=1, column=0, columnspan=3, sticky="ew")

    def _build_left(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Files", style="Header.TLabel").pack(anchor="w")

        # setup JSON
        ttk.Label(parent, text="ACC setup JSON:").pack(anchor="w", pady=(8, 2))
        self.setup_path_var = tk.StringVar()
        row = ttk.Frame(parent); row.pack(fill="x")
        ttk.Entry(row, textvariable=self.setup_path_var, width=24).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self._pick_setup).pack(side="left", padx=4)
        ttk.Button(parent, text="Load setup", command=self._load_setup,
                   style="Accent.TButton").pack(fill="x", pady=(6, 4))

        # MoTeC telemetry (optional)
        ttk.Label(parent, text="MoTeC telemetry (.ld, .ldx, or .csv):"
                  ).pack(anchor="w", pady=(12, 2))
        self.csv_path_var = tk.StringVar()
        row = ttk.Frame(parent); row.pack(fill="x")
        ttk.Entry(row, textvariable=self.csv_path_var, width=24).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self._pick_csv).pack(side="left", padx=4)
        ttk.Button(parent, text="Load telemetry", command=self._load_csv).pack(
            fill="x", pady=(6, 4))
        ttk.Button(parent, text="Clear telemetry", command=self._clear_csv).pack(
            fill="x")

        ttk.Separator(parent).pack(fill="x", pady=12)

        # temperature
        ttk.Label(parent, text="Temperature compensation",
                  style="Header.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Base setup is calibrated for 20°C ambient.").pack(
            anchor="w", pady=(4, 0))
        row = ttk.Frame(parent); row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Target ambient (°C):").pack(side="left")
        self.temp_var = tk.StringVar(value=self.DEFAULT_TEMP)
        ttk.Entry(row, textvariable=self.temp_var, width=6).pack(side="left", padx=6)
        ttk.Button(parent, text="Apply temperature comp",
                   command=self._apply_temperature).pack(fill="x", pady=(6, 0))

        ttk.Separator(parent).pack(fill="x", pady=12)

        # car / state info
        ttk.Label(parent, text="Loaded", style="Header.TLabel").pack(anchor="w")
        self.car_var = tk.StringVar(value="(no setup loaded)")
        ttk.Label(parent, textvariable=self.car_var,
                  font=("Helvetica", 11, "bold")).pack(anchor="w", pady=(4, 0))
        self.tel_var = tk.StringVar(value="Telemetry: not loaded")
        ttk.Label(parent, textvariable=self.tel_var).pack(anchor="w")

    def _build_center(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Track (decorative)",
                  style="Header.TLabel").pack(anchor="w")
        row = ttk.Frame(parent); row.pack(fill="x", pady=(6, 8))
        ttk.Label(row, text="Track:").pack(side="left")
        self.track_var = tk.StringVar()
        cb = ttk.Combobox(row, textvariable=self.track_var,
                          values=list(TRACK_LAYOUTS.keys()),
                          state="readonly", width=22)
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", self._on_track_change)

        self.selected_corner_var = tk.StringVar(value="")

        # Decorative track map — shrunk so the telemetry chart gets room.
        self.map = TrackMapCanvas(parent, height=300)
        self.map.pack(fill="x")

        # ------ Telemetry chart + custom-range selector --------------------
        ttk.Separator(parent).pack(fill="x", pady=(10, 6))
        chart_hdr = ttk.Frame(parent); chart_hdr.pack(fill="x")
        ttk.Label(chart_hdr, text="Telemetry trace",
                  style="Header.TLabel").pack(side="left")
        ttk.Label(chart_hdr, text="  channel:").pack(side="left", padx=(16, 4))
        self.chart_channel_var = tk.StringVar(value="Speed")
        ch_combo = ttk.Combobox(
            chart_hdr, textvariable=self.chart_channel_var,
            values=list(TelemetryChart.SUPPORTED_CHANNELS),
            state="readonly", width=10,
        )
        ch_combo.pack(side="left")
        ch_combo.bind("<<ComboboxSelected>>",
                      lambda _e: self.chart.set_channel(self.chart_channel_var.get()))

        self.chart = TelemetryChart(parent, on_range_change=self._on_chart_range,
                                    height=170)
        self.chart.pack(fill="x", pady=(6, 4))

        # Numeric range entries + Analyze button
        range_row = ttk.Frame(parent); range_row.pack(fill="x", pady=(0, 4))
        ttk.Label(range_row, text="Range  start:").pack(side="left")
        self.range_start_var = tk.StringVar(value="")
        ttk.Entry(range_row, textvariable=self.range_start_var,
                  width=8).pack(side="left", padx=(2, 6))
        ttk.Label(range_row, text="end:").pack(side="left")
        self.range_end_var = tk.StringVar(value="")
        ttk.Entry(range_row, textvariable=self.range_end_var,
                  width=8).pack(side="left", padx=(2, 6))
        ttk.Label(range_row, text="m").pack(side="left")
        ttk.Button(range_row, text="Apply numbers to chart",
                   command=self._apply_range_numbers).pack(side="left", padx=8)
        ttk.Button(range_row, text="Auto-classify this range",
                   command=self._analyze_custom_range).pack(side="right")

        # ---- Driver-input row: Issue + Phase + "Diagnose with my input" --
        ttk.Separator(parent).pack(fill="x", pady=(8, 6))
        driver_lbl = ttk.Label(
            parent,
            text="Driver complaint — combine your call with the telemetry:",
            foreground="#cdb060",
        )
        driver_lbl.pack(anchor="w")

        di_row = ttk.Frame(parent); di_row.pack(fill="x", pady=(4, 2))
        ttk.Label(di_row, text="Issue:").pack(side="left")
        self.driver_issue_var = tk.StringVar(value="Understeer")
        for issue in ("Understeer", "Oversteer", "Unstable", "Bottoming"):
            ttk.Radiobutton(di_row, text=issue,
                            variable=self.driver_issue_var,
                            value=issue,
                            command=self._on_driver_issue_change).pack(
                side="left", padx=4)

        dp_row = ttk.Frame(parent); dp_row.pack(fill="x", pady=(0, 2))
        ttk.Label(dp_row, text="Phase:").pack(side="left")
        self.driver_phase_var = tk.StringVar(value="Mid")
        self._driver_phase_buttons = []
        for ph in ("Entry", "Mid", "Exit"):
            rb = ttk.Radiobutton(dp_row, text=ph,
                                 variable=self.driver_phase_var, value=ph)
            rb.pack(side="left", padx=4)
            self._driver_phase_buttons.append(rb)
        ttk.Button(dp_row, text="Diagnose with my input",
                   command=self._diagnose_driver_input,
                   style="Accent.TButton").pack(side="right")

        ttk.Label(parent,
                  text="Drag a region on the trace, pick the issue + phase "
                       "you felt, then click 'Diagnose with my input'. "
                       "The fix list on the right is re-ranked using the "
                       "telemetry for that exact range.",
                  foreground="#888", wraplength=600).pack(anchor="w",
                                                          pady=(4, 0))

    def _build_right(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Telemetry Analysis (MoTeC i2 Pro)",
                  style="Header.TLabel").pack(anchor="w")

        ttk.Button(parent, text="Run analysis on loaded telemetry",
                   command=self._run_analysis,
                   style="Accent.TButton").pack(fill="x", pady=(6, 4))

        lap_row = ttk.Frame(parent); lap_row.pack(fill="x", pady=(2, 4))
        ttk.Label(lap_row, text="Lap:").pack(side="left")
        self.lap_var = tk.StringVar(value="(no laps yet)")
        self.lap_combo = ttk.Combobox(lap_row, textvariable=self.lap_var,
                                      values=[], state="readonly", width=24)
        self.lap_combo.pack(side="left", padx=6)
        self.lap_combo.bind("<<ComboboxSelected>>",
                            lambda _e: self._run_analysis())

        # Detected-issues table
        ttk.Label(parent, text="Detected issues",
                  style="Header.TLabel").pack(anchor="w", pady=(8, 2))
        cols = ("corner", "phase", "issue", "min_sp", "g_lat", "brake")
        self.issues_tree = ttk.Treeview(parent, columns=cols,
                                        show="headings", height=8)
        widths = {"corner": 90, "phase": 55, "issue": 90,
                  "min_sp": 60, "g_lat": 55, "brake": 55}
        labels = {"corner": "Corner", "phase": "Phase", "issue": "Issue",
                  "min_sp": "Min Sp", "g_lat": "Pk G",  "brake": "Brk%"}
        for c in cols:
            self.issues_tree.heading(c, text=labels[c])
            anchor = "w" if c in ("corner", "phase", "issue") else "e"
            self.issues_tree.column(c, width=widths[c], anchor=anchor,
                                    stretch=(c == "corner"))
        self.issues_tree.pack(fill="x")
        self.issues_tree.bind("<<TreeviewSelect>>", self._on_issue_select)

        # Evidence + metrics for selected row
        self.validation_var = tk.StringVar(value="(run analysis to populate)")
        ttk.Label(parent, textvariable=self.validation_var,
                  wraplength=320, foreground="#aaa",
                  font=("Helvetica", 10)).pack(anchor="w", pady=(4, 0))

        # Driver61 fix list — re-ranked by telemetry once a row is selected.
        ttk.Label(parent, text="Driver61 fix (telemetry-ranked)",
                  style="Header.TLabel").pack(anchor="w", pady=(8, 2))
        self.rec_list = tk.Listbox(parent, height=5, exportselection=False,
                                   bg="#262a30", fg="#dcdcdc",
                                   selectbackground="#5cd0ff",
                                   selectforeground="#000",
                                   highlightthickness=0, borderwidth=0,
                                   font=("Menlo", 10))
        self.rec_list.pack(fill="x")
        self.rec_list.bind("<<ListboxSelect>>", self._on_fix_select)
        self.fix_reason_var = tk.StringVar(
            value="(select a fix to see its telemetry rationale)")
        ttk.Label(parent, textvariable=self.fix_reason_var, wraplength=320,
                  foreground="#88ddff",
                  font=("Helvetica", 9, "italic")).pack(anchor="w",
                                                        pady=(2, 4))

        row = ttk.Frame(parent); row.pack(fill="x", pady=4)
        ttk.Button(row, text="Apply selected fix",
                   command=self._apply_selected_telem
                   ).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(row, text="Apply 1st fix to ALL issues",
                   command=self._apply_all_detected
                   ).pack(side="left", expand=True, fill="x", padx=2)

        # queue + save/reset (unchanged)
        ttk.Label(parent, text="Adjustment queue",
                  style="Header.TLabel").pack(anchor="w", pady=(12, 2))
        self.queue = tk.Listbox(parent, height=6,
                                bg="#262a30", fg="#dcdcdc",
                                highlightthickness=0, borderwidth=0)
        self.queue.pack(fill="both", expand=True)

        ttk.Button(parent, text="Save modified_setup.json",
                   command=self._save, style="Accent.TButton"
                   ).pack(fill="x", pady=(10, 0))
        ttk.Button(parent, text="Reset all changes",
                   command=self._reset).pack(fill="x", pady=4)

        # Issue/phase state still used internally for the recommendation lookup,
        # but the user no longer chooses them — they're set by the selected row.
        self.issue_var = tk.StringVar(value="Understeer")
        self.phase_var = tk.StringVar(value="Entry")

        # Internal analysis state
        self._analysis_results: list[dict] = []
        self._lap_ranges: list[tuple[int, int]] = []
        self._selected_corner_dict: dict | None = None
        # Last-computed telemetry-ranked diagnosis (drives rec_list ordering
        # and the per-fix reason text).
        self._current_diagnosis: dict | None = None

    # ---- file actions ----
    def _pick_setup(self) -> None:
        p = filedialog.askopenfilename(
            title="Select ACC setup JSON",
            initialdir=os.path.dirname(os.path.abspath(__file__)),
            filetypes=[("ACC setup", "*.json"), ("All files", "*.*")],
        )
        if p:
            self.setup_path_var.set(p)

    @staticmethod
    def _default_motec_dir() -> str | None:
        """Find the ACC MoTeC log folder. Looks in ``~/Documents`` and
        ``~/OneDrive/Documents`` (Windows commonly redirects Documents
        into OneDrive). Returns the first one that exists, or None."""
        candidates = [
            os.path.expanduser(
                "~/Documents/Assetto Corsa Competizione/MoTeC"),
            os.path.expanduser(
                "~/OneDrive/Documents/Assetto Corsa Competizione/MoTeC"),
        ]
        # Windows: also check explicit USERPROFILE in case ~ doesn't expand.
        if os.name == "nt" and "USERPROFILE" in os.environ:
            up = os.environ["USERPROFILE"]
            candidates += [
                os.path.join(up, "Documents",
                             "Assetto Corsa Competizione", "MoTeC"),
                os.path.join(up, "OneDrive", "Documents",
                             "Assetto Corsa Competizione", "MoTeC"),
            ]
        for c in candidates:
            if os.path.isdir(c):
                return c
        return None

    def _pick_csv(self) -> None:
        acc_dir = self._default_motec_dir()
        initial = acc_dir or os.path.dirname(os.path.abspath(__file__))
        p = filedialog.askopenfilename(
            title="Select MoTeC log (.ld, .ldx) or CSV export",
            initialdir=initial,
            filetypes=[
                ("MoTeC binary log", "*.ld"),
                ("MoTeC index XML", "*.ldx"),
                ("MoTeC CSV", "*.csv"),
                ("All telemetry", "*.ld *.ldx *.csv"),
                ("All files", "*.*"),
            ],
        )
        if p:
            self.csv_path_var.set(p)

    def _load_setup(self) -> None:
        path = self.setup_path_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("Setup", "Pick a valid setup JSON first.")
            return
        try:
            self.mgr = SetupManager(path)
        except Exception as e:
            messagebox.showerror("Setup", f"Failed to load: {e}")
            return
        car = self.mgr.setup.get("carName", "unknown")
        self.car_var.set(f"Car: {car}")
        self._set_status(f"Loaded {os.path.basename(path)}")
        self._refresh_queue()
        if car != "porsche_992_gt3_r":
            messagebox.showwarning(
                "Car mismatch",
                f"Loaded car is '{car}'. Defaults are tuned for the "
                f"Porsche 992 GT3 R; per-car rates may differ.",
            )

    def _load_csv(self) -> None:
        path = self.csv_path_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror(
                "Telemetry",
                "Pick a valid MoTeC telemetry file first (.ld, .ldx, or .csv).")
            return
        try:
            self.tel = TelemetryAnalyzer(path)
        except Exception as e:
            messagebox.showerror("Telemetry", f"Failed to load: {e}")
            return
        self.tel_var.set(f"Telemetry: {len(self.tel.df)} rows, "
                         f"{len(self.tel.df.columns)} channels")
        self._set_status(f"Telemetry loaded — {os.path.basename(path)}")
        # Push the full telemetry into the chart right away so the user can
        # eyeball the trace before any analysis is run.
        self.chart.set_data(self.tel.df, self.chart_channel_var.get())
        self.chart.clear_selection()
        self._update_validation_label(
            "Drag on the trace to pick a range, or hit 'Run analysis' "
            "for full auto-detection."
        )

    def _clear_csv(self) -> None:
        self.tel = None
        self.tel_var.set("Telemetry: not loaded")
        self._update_validation_label("(load CSV to validate)")
        self._set_status("Telemetry cleared.")
        self.chart.set_data(None)

    # ---- temperature ----
    def _apply_temperature(self) -> None:
        if not self._require_setup():
            return
        try:
            t = float(self.temp_var.get().strip())
        except ValueError:
            messagebox.showerror("Temperature",
                                 "Enter a number, e.g. 28")
            return
        before_n = len(self.mgr.changes)
        self.mgr.changes.append("\n[PRE-RUN ADJUSTMENTS]")
        self.mgr.adjust_for_temperature(t)
        added = self.mgr.changes[before_n:]
        if added:
            for line in added:
                if line.strip():
                    self.queue.insert("end", line.splitlines()[0].strip())
        self._set_status(f"Temperature compensation applied for {t}°C.")

    # ---- track / corner ----
    def _on_track_change(self, _ev=None) -> None:
        track = self.track_var.get()
        self.current_track = track
        self.current_corner = None
        self.selected_corner_var.set("")
        self.map.set_track(track)
        self.map.set_issue_annotations({})

    # ---- chart range handlers ----
    def _on_chart_range(self, start_d: float, end_d: float) -> None:
        """User finished a drag on the chart — mirror to the entry fields."""
        self.range_start_var.set(f"{start_d:.0f}")
        self.range_end_var.set(f"{end_d:.0f}")

    def _apply_range_numbers(self) -> None:
        """Push numeric Start/End entries back onto the chart selection."""
        try:
            s = float(self.range_start_var.get().strip())
            e = float(self.range_end_var.get().strip())
        except ValueError:
            messagebox.showinfo("Range",
                                "Enter numeric distances (in metres).")
            return
        if e < s:
            s, e = e, s
        self.chart.set_selection(s, e)

    def _analyze_custom_range(self) -> None:
        """Run the analyzer on the chart's currently-selected distance range
        and replace the issues Treeview with the result."""
        if self.tel is None:
            messagebox.showinfo("Analyze range",
                                "Load a MoTeC telemetry file first.")
            return
        s, e = self.chart.get_selection()
        # If the chart has no selection, fall back to the entry fields.
        if s is None or e is None:
            try:
                s = float(self.range_start_var.get().strip())
                e = float(self.range_end_var.get().strip())
            except ValueError:
                messagebox.showinfo(
                    "Analyze range",
                    "Drag a region on the trace, or type Start/End in metres "
                    "and click 'Apply numbers to chart' first.",
                )
                return
        if e - s < 10:
            messagebox.showinfo("Analyze range",
                                "The selected range is too small "
                                "(less than 10 m).")
            return

        corner = self.tel.analyze_range(s, e)
        if corner is None:
            messagebox.showinfo("Analyze range",
                                "Not enough samples in the selected range.")
            return

        # Replace the analysis results with this single user-picked corner.
        self._analysis_results = [corner]
        self._populate_issues_tree([corner], scope_label="custom range")
        self.chart.set_selection(s, e)

        # Pre-select the first issue if any so the fix list is ready.
        children = self.issues_tree.get_children()
        if children:
            self.issues_tree.selection_set(children[0])
            self._on_issue_select()
            self._set_status(
                f"Custom range {s:.0f}-{e:.0f}m: {len(corner['issues'])} "
                f"issue(s) flagged."
            )
        else:
            self._set_status(
                f"Custom range {s:.0f}-{e:.0f}m: no issues detected."
            )
            self.validation_var.set(
                f"User range {s:.0f}-{e:.0f}m — no handling problems detected. "
                "Try widening the selection or pick a different region."
            )

    # ---- recommendation lookup helpers (kept for the apply machinery) ----
    def _phase_for_lookup(self) -> str:
        return (self.phase_var.get()
                if self.issue_var.get() in ("Understeer", "Oversteer") else "*")

    def _recommended_methods(self) -> list[tuple[str, str]]:
        return (RECOMMENDATIONS.get((self.issue_var.get(), self._phase_for_lookup()))
                or RECOMMENDATIONS.get((self.issue_var.get(), "*"), []))

    # ---- telemetry analysis ----
    def _map_corner_to_named(self, dist_start: float, dist_end: float) -> str:
        """Map a detected distance window to the closest named corner.

        Looks up the closest entry in ``TRACK_MAP`` for the current track,
        then prefers the matching ``TRACK_LAYOUTS`` name (so the marker
        actually highlights). Falls back to the TRACK_MAP name if the
        layouts dict doesn't have it. Returns ``""`` if nothing is close.
        """
        if not self.current_track:
            return ""
        named = TRACK_MAP.get(self.current_track, {})
        if not named:
            return ""
        midpoint = (dist_start + dist_end) / 2
        best, best_d = None, float("inf")
        for name, (s, e) in named.items():
            mid = (s + e) / 2
            d = abs(midpoint - mid)
            if d < best_d:
                best_d, best = d, name
        if best_d > 400 or best is None:
            return ""

        # Prefer a TRACK_LAYOUTS key — exact match, or same leading token
        # (e.g. "T8 Pouhon" ↔ "T8 Variante Ascari" share "T8") so the marker
        # gets highlighted even when the long names diverge.
        layout_corners = TRACK_LAYOUTS.get(self.current_track, {}).get("corners", {})
        if best in layout_corners:
            return best
        best_token = best.split()[0] if best else ""
        for layout_name in layout_corners:
            if layout_name.split()[0] == best_token:
                return layout_name
        # Layouts dict has no matching marker; return the TRACK_MAP name
        # anyway so the user still sees the corner identified.
        return best

    def _run_analysis(self) -> None:
        if self.tel is None:
            messagebox.showinfo("Analysis", "Load a MoTeC telemetry file first.")
            return

        # Populate the lap dropdown the first time, or when laps are unknown.
        self._lap_ranges = self.tel.detect_laps()
        labels = []
        for i, (s, e) in enumerate(self._lap_ranges, 1):
            ds = self.tel.df["Distance"].iloc[s]
            de = self.tel.df["Distance"].iloc[e]
            labels.append(f"Lap {i} ({ds:.0f}–{de:.0f}m, {e - s + 1} samples)")
        self.lap_combo.configure(values=labels)
        if not labels:
            self._set_status("Telemetry has no usable rows.")
            return
        if self.lap_var.get() not in labels:
            self.lap_var.set(labels[0])
        lap_idx = labels.index(self.lap_var.get())

        results = self.tel.analyze(lap_range=self._lap_ranges[lap_idx])
        self._analysis_results = results
        n_rows = self._populate_issues_tree(results,
                                            scope_label=f"lap {lap_idx + 1}")

        # Update the chart so the user can see the current lap.
        s, e = self._lap_ranges[lap_idx]
        self.chart.set_data(self.tel.df.iloc[s:e + 1].reset_index(drop=True),
                            self.chart_channel_var.get())
        self.chart.clear_selection()

        n_corners = len(results)
        self._set_status(f"Analysis: {n_corners} corners, {n_rows} issues "
                         f"flagged on lap {lap_idx + 1}.")
        if n_rows == 0:
            self.validation_var.set(
                "No handling issues detected on this lap. "
                "Try another lap, or drag on the trace to flag a "
                "specific corner manually."
            )

    def _populate_issues_tree(self, results: list[dict],
                              scope_label: str = "") -> int:
        """Replace the Treeview with rows from `results` and update the map
        annotations. Returns the number of rows inserted."""
        self.issues_tree.delete(*self.issues_tree.get_children())
        annotations: dict[str, str] = {}
        priority = {"Bottoming": 4, "Oversteer": 3,
                    "Understeer": 2, "Unstable": 1}
        n_rows = 0
        for c in results:
            named = self._map_corner_to_named(c["start_dist"], c["end_dist"])
            corner_label = (named
                            or f"{c['start_dist']:.0f}-{c['end_dist']:.0f}m")
            for phase, issue, _evidence in c["issues"]:
                m = c["metrics"]
                self.issues_tree.insert(
                    "", "end",
                    iid=f"{c['index']}|{phase}|{issue}",
                    values=(
                        corner_label, phase, issue,
                        f"{m['min_speed']:.0f}" if m.get("min_speed") else "—",
                        f"{m['peak_glat']:.2f}" if m.get("peak_glat") else "—",
                        f"{m['peak_brake']:.0f}" if m.get("peak_brake") else "—",
                    ),
                )
                n_rows += 1
                if (corner_label not in annotations
                        or priority.get(issue, 0)
                        > priority.get(annotations[corner_label], 0)):
                    annotations[corner_label] = issue
        # Filter map annotations to corners that actually have markers.
        if self.current_track and self.current_track in TRACK_LAYOUTS:
            track_corners = set(
                TRACK_LAYOUTS[self.current_track]["corners"].keys()
            )
            annotations = {k: v for k, v in annotations.items()
                           if k in track_corners}
        self.map.set_issue_annotations(annotations)
        return n_rows

    def _on_issue_select(self, _ev=None) -> None:
        sel = self.issues_tree.selection()
        if not sel:
            return
        try:
            idx_str, phase, issue = sel[0].split("|")
            corner_idx = int(idx_str)
        except (ValueError, IndexError):
            return
        corner = next((c for c in self._analysis_results
                       if c["index"] == corner_idx), None)
        if corner is None:
            return
        self._selected_corner_dict = corner

        self.issue_var.set(issue)
        if phase in ("Entry", "Mid", "Exit"):
            self.phase_var.set(phase)

        named = self._map_corner_to_named(corner["start_dist"], corner["end_dist"])
        self.current_corner = (named
                               or f"C{corner['index']} ({corner['start_dist']:.0f}m)")
        self.selected_corner_var.set(self.current_corner)

        # Re-rank the Driver61 fixes using the diagnose() engine so the
        # ordering reflects what the telemetry actually shows.
        diagnosis = self.tel.diagnose(corner, issue, phase) if self.tel else \
                    {"fixes": [], "signals": {}}
        self._current_diagnosis = diagnosis

        # Validation message: top-fix reason + headline metrics.
        m = corner["metrics"]
        top_reason = (diagnosis["fixes"][0]["reason"]
                      if diagnosis["fixes"] else "(no fix list for this combo)")
        self.validation_var.set(
            f"{self.current_corner} — {phase} {issue}    "
            f"peak brake {m.get('peak_brake', 0):.0f}%, "
            f"min speed {m.get('min_speed', 0):.0f}, "
            f"|G_Lat| {m.get('peak_glat', 0):.2f}g, "
            f"steer {m.get('peak_steer', 0):.1f}°.\n"
            f"Top fix: {top_reason}"
        )

        # Repopulate rec_list with the telemetry-ranked order.
        self.rec_list.delete(0, "end")
        for fx in diagnosis["fixes"]:
            pct = int(round(fx["score"] * 100))
            self.rec_list.insert("end", f"[{pct:>3}%] {fx['label']}")
        if diagnosis["fixes"]:
            self.rec_list.selection_set(0)
            self._on_fix_select()
        else:
            self.fix_reason_var.set("(no fixes for this combination)")

    def _on_fix_select(self, _ev=None) -> None:
        """Show the telemetry rationale for the highlighted fix."""
        if not self._current_diagnosis:
            self.fix_reason_var.set("")
            return
        sel = self.rec_list.curselection()
        if not sel:
            return
        idx = sel[0]
        fixes = self._current_diagnosis["fixes"]
        if idx < len(fixes):
            self.fix_reason_var.set("→ " + fixes[idx]["reason"])

    # ---- driver-input diagnosis ----
    def _on_driver_issue_change(self) -> None:
        """Disable the Phase radios when the issue is whole-corner only."""
        issue = self.driver_issue_var.get()
        state = ("normal" if issue in ("Understeer", "Oversteer")
                 else "disabled")
        for rb in self._driver_phase_buttons:
            rb.configure(state=state)

    def _diagnose_driver_input(self) -> None:
        """Driver-driven diagnosis: take the chart range + issue/phase
        radios, then run the analyzer + diagnose() and put one row into the
        issues Treeview so the user can apply a fix."""
        if self.tel is None:
            messagebox.showinfo("Diagnose",
                                "Load a MoTeC telemetry file first.")
            return
        s, e = self.chart.get_selection()
        if s is None or e is None:
            try:
                s = float(self.range_start_var.get().strip())
                e = float(self.range_end_var.get().strip())
            except ValueError:
                messagebox.showinfo(
                    "Diagnose",
                    "Drag a range on the trace (or fill Start/End and "
                    "'Apply numbers to chart') first.",
                )
                return
        if e - s < 10:
            messagebox.showinfo("Diagnose",
                                "The selected range is too small (<10m).")
            return

        corner = self.tel.analyze_range(s, e)
        if corner is None:
            messagebox.showinfo(
                "Diagnose",
                "Not enough samples in the selected range to analyze.")
            return

        issue = self.driver_issue_var.get()
        phase = (self.driver_phase_var.get()
                 if issue in ("Understeer", "Oversteer") else "Corner")

        # Overwrite the corner's classified issues with the driver's call so
        # the Treeview shows a single row reflecting their input.
        corner["issues"] = [(
            phase, issue,
            f"Driver-reported {issue.lower()} on {s:.0f}-{e:.0f}m.",
        )]
        self._analysis_results = [corner]
        n = self._populate_issues_tree([corner],
                                       scope_label="driver-input range")
        self.chart.set_selection(s, e)

        children = self.issues_tree.get_children()
        if children:
            self.issues_tree.selection_set(children[0])
            self._on_issue_select()  # this triggers diagnose()
        self._set_status(
            f"Diagnosed driver-reported {issue} @ {phase} on "
            f"{s:.0f}-{e:.0f}m — {n} fix list ranked by telemetry."
        )

    def _apply_selected_telem(self) -> None:
        """Apply the highlighted, telemetry-ranked Driver61 fix."""
        if not self._require_setup():
            return
        if not self._current_diagnosis or not self._current_diagnosis["fixes"]:
            messagebox.showinfo("Apply",
                                "Run a diagnosis first (auto-classify or "
                                "'Diagnose with my input').")
            return
        sel = self.rec_list.curselection()
        idx = sel[0] if sel else 0
        fixes = self._current_diagnosis["fixes"]
        if idx >= len(fixes):
            return
        fx = fixes[idx]
        # Snapshot driver-side context so the apply header reads right.
        self.issue_var.set(self._current_diagnosis["issue"])
        self.phase_var.set(self._current_diagnosis["phase"])
        self._apply_fix([(fx["label"], fx["method"])])
        self._set_status(
            f"Applied [{int(round(fx['score']*100))}%] {fx['label']} "
            f"for {self.current_corner}."
        )

    def _apply_all_detected(self) -> None:
        """Apply the TOP-RANKED fix (per telemetry) for every detected
        issue across all corners."""
        if not self._require_setup():
            return
        if not self._analysis_results:
            messagebox.showinfo("Apply",
                                "No analysis results yet — click Run analysis.")
            return
        original_issue = self.issue_var.get()
        original_phase = self.phase_var.get()
        original_corner = self.current_corner

        applied = 0
        for corner in self._analysis_results:
            named = self._map_corner_to_named(corner["start_dist"], corner["end_dist"])
            self.current_corner = (named
                                   or f"C{corner['index']} ({corner['start_dist']:.0f}m)")
            for phase, issue, _ev in corner["issues"]:
                self.issue_var.set(issue)
                if phase in ("Entry", "Mid", "Exit"):
                    self.phase_var.set(phase)
                diag = self.tel.diagnose(corner, issue, phase)
                if diag["fixes"]:
                    top = diag["fixes"][0]
                    self._apply_fix([(top["label"], top["method"])])
                    applied += 1

        self.issue_var.set(original_issue)
        self.phase_var.set(original_phase)
        self.current_corner = original_corner
        self._set_status(f"Auto-apply complete: {applied} telemetry-ranked "
                         f"fix(es) queued.")

    # ---- validation ----
    def _validate(self) -> None:
        if not self._require_setup():
            return
        if self.tel is None:
            self._update_validation_label("No CSV loaded.")
            return
        if not self.current_track or not self.current_corner:
            self._update_validation_label("Pick a corner first.")
            return

        d_start, d_end = TRACK_MAP.get(self.current_track, {}).get(
            self.current_corner, (None, None))
        if d_start is None:
            self._update_validation_label(
                f"No distance window for {self.current_corner}.")
            return

        corner_df = self.tel.slice_corner(d_start, d_end)
        if corner_df.empty:
            self._update_validation_label("No telemetry samples in this corner.")
            return
        phases = self.tel.split_phases(corner_df)
        issue = self.issue_var.get()
        phase = self._phase_for_lookup()

        if issue == "Understeer":
            ok, msg = self.tel.validate_understeer(phases[phase].df)
        elif issue == "Oversteer":
            ok, msg = self.tel.validate_oversteer(phases[phase].df)
        elif issue == "Unstable":
            ok, msg = self.tel.validate_instability(corner_df)
        else:
            ok, msg = self.tel.validate_bottoming(corner_df)
        prefix = "✓ CONFIRMED" if ok else "⚠ NOT confirmed"
        self._update_validation_label(f"{prefix} — {msg}", ok)

    # ---- apply ----
    def _apply_selected(self) -> None:
        if not self._require_setup() or not self._require_corner():
            return
        sel = self.rec_list.curselection()
        if not sel:
            messagebox.showinfo("Pick fix", "Select a recommendation first.")
            return
        recs = self._recommended_methods()
        if sel[0] >= len(recs):
            return
        label, method_name = recs[sel[0]]
        self._apply_fix([(label, method_name)])

    def _apply_all(self) -> None:
        if not self._require_setup() or not self._require_corner():
            return
        recs = self._recommended_methods()
        if not recs:
            return
        self._apply_fix(recs)

    def _apply_fix(self, recs: list[tuple[str, str]]) -> None:
        track = (self.current_track or "?").upper()
        corner = self.current_corner or "?"
        issue = self.issue_var.get()
        phase = self._phase_for_lookup()
        header = f"\n{track} — {corner} — {issue} @ {phase}"
        self.mgr.changes.append(header)
        for _, method_name in recs:
            getattr(self.mgr, method_name)()
        self._refresh_queue()
        self._set_status(f"Applied {len(recs)} adjustment(s) for "
                         f"{issue} @ {phase} on {corner}.")

    # ---- queue / save / reset ----
    def _refresh_queue(self) -> None:
        self.queue.delete(0, "end")
        if not self.mgr:
            return
        for line in self.mgr.changes:
            for sub in line.splitlines():
                if sub.strip():
                    self.queue.insert("end", sub.rstrip())

    def _save(self) -> None:
        if not self._require_setup():
            return
        if not self.mgr.changes:
            messagebox.showinfo("Save", "No changes to save yet.")
            return
        out = filedialog.asksaveasfilename(
            title="Save modified setup",
            initialdir=os.path.dirname(self.setup_path_var.get() or "."),
            initialfile="modified_setup.json",
            defaultextension=".json",
            filetypes=[("ACC setup", "*.json")],
        )
        if not out:
            return
        self.mgr.save(out)
        self._set_status(f"Saved → {out}")
        messagebox.showinfo("Saved", f"Modified setup written to:\n{out}")

    def _reset(self) -> None:
        if not self.mgr:
            return
        if not messagebox.askyesno(
                "Reset", "Discard all queued adjustments and reload the setup from disk?"):
            return
        self._load_setup()

    # ---- helpers ----
    def _require_setup(self) -> bool:
        if self.mgr is None:
            messagebox.showinfo("Setup", "Load a setup JSON first.")
            return False
        return True

    def _require_corner(self) -> bool:
        if not self.current_corner:
            messagebox.showinfo("Corner", "Select a corner on the map first.")
            return False
        return True

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _update_validation_label(self, text: str, ok: bool | None = None) -> None:
        colour = "#4caf50" if ok else ("#ff7070" if ok is False else "#aaa")
        for w in self.right.winfo_children():
            if isinstance(w, ttk.Label) and w.cget("textvariable") == str(self.validation_var):
                w.configure(foreground=colour)
        self.validation_var.set(text)


def main() -> None:
    root = tk.Tk()
    app = SetupOptimizerApp(root)

    # auto-pick the local base setup if present
    here = os.path.dirname(os.path.abspath(__file__))
    default_setup = os.path.join(here, "FRI3_992_BASE_v1.10.json")
    if os.path.isfile(default_setup):
        app.setup_path_var.set(default_setup)

    root.mainloop()


if __name__ == "__main__":
    main()
