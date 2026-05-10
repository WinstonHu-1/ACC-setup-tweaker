"""
ACC Setup Optimizer
-------------------
Interactive tool that validates a driver-reported handling issue against MoTeC
telemetry, then applies a Driver61-guided fix to an ACC setup.json file.

ACC JSON wheel-index convention:
    index 0 = Front Left   (FL)
    index 1 = Front Right  (FR)
    index 2 = Rear Left    (RL)
    index 3 = Rear Right   (RR)

Most numeric fields in the ACC JSON are CLICK INDICES, not engineering units.
A click is the smallest in-game adjustment step. This tool adjusts click
indices; the underlying engineering delta (psi, mm, degrees, etc.) depends on
the car. Adjustments are intentionally conservative (1-2 clicks) so that the
race engineer can iterate.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Car registry — base_setups/<carName>.json
# ---------------------------------------------------------------------------
# Each file in base_setups/ is a "factory" setup for one car. A user can
# add a new car by uploading any setup with a `carName` field; we copy it
# into base_setups/<carName>.json. The GUI's car dropdown is built from
# this directory.
BASE_SETUPS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "base_setups"
)

# Pretty display names for ACC car codes. Falls back to title-casing the
# code if the car isn't listed here.
CAR_DISPLAY_NAMES: dict[str, str] = {
    "porsche_992_gt3_r":     "Porsche 992 GT3 R",
    "porsche_991ii_gt3_r":   "Porsche 991 II GT3 R",
    "ferrari_296_gt3":       "Ferrari 296 GT3",
    "ferrari_488_gt3_evo":   "Ferrari 488 GT3 Evo",
    "ferrari_488_gt3":       "Ferrari 488 GT3",
    "audi_r8_lms_evo_ii":    "Audi R8 LMS Evo II",
    "audi_r8_lms_evo":       "Audi R8 LMS Evo",
    "audi_r8_lms":           "Audi R8 LMS",
    "bmw_m4_gt3":            "BMW M4 GT3",
    "bmw_m6_gt3":            "BMW M6 GT3",
    "amg_gt3_evo":           "Mercedes-AMG GT3 Evo",
    "amg_gt3":               "Mercedes-AMG GT3",
    "mclaren_720s_gt3_evo":  "McLaren 720S GT3 Evo",
    "mclaren_720s_gt3":      "McLaren 720S GT3",
    "lamborghini_huracan_gt3_evo2": "Lamborghini Huracán GT3 Evo 2",
    "lamborghini_huracan_gt3_evo":  "Lamborghini Huracán GT3 Evo",
    "honda_nsx_gt3_evo":     "Honda NSX GT3 Evo",
    "lexus_rc_f_gt3":        "Lexus RC F GT3",
    "aston_martin_v8_vantage_gt3":  "Aston Martin V8 Vantage GT3",
    "nissan_gt_r_gt3_2018":  "Nissan GT-R GT3 (2018)",
    "ford_mustang_gt3":      "Ford Mustang GT3",
}


def car_display_name(car_id: str) -> str:
    """Pretty name for a car-id. Falls back to a title-cased version."""
    if car_id in CAR_DISPLAY_NAMES:
        return CAR_DISPLAY_NAMES[car_id]
    return car_id.replace("_", " ").title()


def list_base_setups() -> dict[str, str]:
    """Return ``{car_id: path_to_base_setup_json}`` for everything in
    base_setups/. Files without a carName field are skipped."""
    out: dict[str, str] = {}
    if not os.path.isdir(BASE_SETUPS_DIR):
        return out
    for fname in sorted(os.listdir(BASE_SETUPS_DIR)):
        if not fname.lower().endswith(".json"):
            continue
        path = os.path.join(BASE_SETUPS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        car_id = data.get("carName")
        if not car_id:
            continue
        # Prefer the first file we find for a given car_id (sorted order).
        out.setdefault(car_id, path)
    return out


def add_base_setup(setup_path: str) -> str:
    """Register a setup as the base for its declared car. Copies the file
    into base_setups/<carName>.json and returns the car_id.

    Raises ValueError if the file has no carName, or OSError on copy fail.
    """
    with open(setup_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    car_id = data.get("carName")
    if not car_id:
        raise ValueError("Setup has no 'carName' field — can't register it.")

    os.makedirs(BASE_SETUPS_DIR, exist_ok=True)
    dest = os.path.join(BASE_SETUPS_DIR, f"{car_id}.json")
    if os.path.abspath(setup_path) != os.path.abspath(dest):
        shutil.copy2(setup_path, dest)
    return car_id


# ---------------------------------------------------------------------------
# Track database
# ---------------------------------------------------------------------------
# Distance windows are approximate "from brake-marker to corner exit" ranges
# along the racing line. Extend this dict as needed.
TRACK_MAP: dict[str, dict[str, tuple[int, int]]] = {
    "monza": {
        "T1 Variante del Rettifilo": (550, 900),
        "T4 Variante della Roggia": (2300, 2600),
        "T6 Lesmo 1": (3050, 3250),
        "T7 Lesmo 2": (3300, 3550),
        "T8 Variante Ascari": (4400, 4800),
        "T11 Parabolica": (5200, 5700),
    },
    "spa": {
        "T1 La Source": (150, 350),
        "T5 Les Combes": (2050, 2350),
        "T8 Pouhon": (3450, 3750),
        "T13 Stavelot": (4900, 5150),
        "T15 Bus Stop": (6500, 6800),
    },
    "nurburgring": {
        "T1 Castrol-S": (200, 500),
        "T6 Dunlop-Kehre": (2200, 2500),
        "T10 Schumacher-S": (3700, 4000),
    },
}


# ---------------------------------------------------------------------------
# Telemetry analyzer
# ---------------------------------------------------------------------------
@dataclass
class PhaseSlice:
    """Telemetry slice for a single corner phase (Entry / Mid / Exit)."""
    name: str
    df: pd.DataFrame = field(repr=False)

    @property
    def empty(self) -> bool:
        return self.df.empty


class TelemetryAnalyzer:
    """Loads a MoTeC i2 Pro CSV export and exposes:

    - per-lap slicing (`detect_laps`)
    - per-corner auto-detection from brake events (`auto_detect_corners`)
    - per-phase metrics (`split_phases`, `corner_metrics`)
    - issue classification: understeer, oversteer, instability, bottoming.

    i2 Pro CSV format handled here:
        - leading metadata block (Time, Format, Sample Rate, Beacon Markers …)
        - row of channel names
        - row of units (e.g. ``s, m, %, %, deg, g …``) — auto-skipped
        - data rows
    """

    # ---- phase detection thresholds (per spec) ----
    BRAKE_ENTRY_THRESHOLD = 10.0      # %  — defines Entry phase
    THROTTLE_MID_THRESHOLD = 10.0     # %  — Mid is steer-peak with throttle below this
    THROTTLE_EXIT_THRESHOLD = 20.0    # %  — Exit phase begins above this

    # ---- auto-corner detection ----
    BRAKE_CORNER_THRESHOLD = 20.0     # %  — what counts as a "corner" brake event
    THROTTLE_OUT_THRESHOLD = 80.0     # %  — extend corner end to where throttle returns above this
    MIN_CORNER_GAP_M = 60.0           # m  — merge corners closer than this

    # ---- channel-name aliases ----
    # Keys are canonical names used internally. Values are normalised lookup
    # tokens (lowercased, with whitespace/underscores/punctuation stripped).
    # Example: ``"Steered Angle [deg]"`` → ``"steeredangledeg"``.
    CHANNEL_ALIASES: dict[str, list[str]] = {
        "Distance":     ["distance", "lapdistance", "dist"],
        "Time":         ["time", "elapsedtime", "logtime"],
        "Lap":          ["lap", "lapnumber", "lapno", "lapcount", "currentlap"],
        "Speed":        ["speed", "groundspeed", "speedkmh", "speedkph",
                         "vehiclespeed", "carspeed"],
        "Steer":        ["steer", "steerangle", "steeredangle", "steering",
                         "steeringangle", "steeringposition", "steerposition"],
        "G_Lat":        ["glat", "lateralacceleration", "gforcelat",
                         "lateralg", "accellat", "gflat", "lat_g"],
        "G_Lon":        ["glon", "longitudinalacceleration", "gforcelon",
                         "longitudinalg", "accellon", "gflon"],
        "Brake":        ["brake", "brakepos", "brakeposition", "brakepedal",
                         "brakeapplied"],
        "Throttle":     ["throttle", "throttlepos", "throttleposition",
                         "throttlepedal", "tps"],
        "RPM":          ["rpm", "enginerpm", "engspeed", "enginespeed"],
        "Gear":         ["gear", "currentgear", "selectedgear"],
        # 4-corner suspension travel (i2 Pro typically uses LF/RF/LR/RR).
        "Susp_Travel_FL": ["susptravelfl", "susptravellf", "spftravelfl",
                           "suspensiontravelfl", "suspensiontravellf",
                           "shockposfl", "shockposlf"],
        "Susp_Travel_FR": ["susptravelfr", "susptravelrf", "spftravelfr",
                           "suspensiontravelfr", "suspensiontravelrf",
                           "shockposfr", "shockposrf"],
        "Susp_Travel_RL": ["susptravelrl", "susptravellr", "spftravelrl",
                           "suspensiontravelrl", "suspensiontravellr",
                           "shockposrl", "shockposlr"],
        "Susp_Travel_RR": ["susptravelrr", "spftravelrr",
                           "suspensiontravelrr", "shockposrr"],
    }

    def __init__(self, path: str | list[str]) -> None:
        """Accept any of:
        - a single path: ``.csv`` (i2 Pro export), ``.ld`` (ACC binary log),
          or ``.ldx`` (XML index — we read the sibling .ld)
        - a list of paths: each loaded separately, then concatenated. Time
          and Distance are offset across files so they're continuous.

        With multiple files, two extra columns are added:
            ``__file_idx``    — 0-based index of the source file
            ``__source_file`` — basename of the source file
        These are used by ``detect_laps`` to keep file boundaries from
        merging into one giant lap.
        """
        if isinstance(path, str):
            paths = [path]
        else:
            paths = list(path)
        if not paths:
            raise ValueError("No telemetry files provided.")

        self.source_paths = paths
        self.csv_path = paths[0] if len(paths) == 1 \
                        else f"{len(paths)} files"

        per_file_dfs: list[pd.DataFrame] = []
        time_acc = 0.0
        dist_acc = 0.0
        for i, p in enumerate(paths):
            df_raw = self._load_one(p)
            df = self._normalise_one(df_raw)
            if df.empty:
                continue

            df["__file_idx"] = i
            df["__source_file"] = os.path.basename(p)

            # Offset Time and Distance so they're monotonic across files.
            if "Time" in df.columns:
                df["Time"] = df["Time"] - df["Time"].iloc[0] + time_acc
                time_acc = float(df["Time"].iloc[-1]) + 0.1
            if "Distance" in df.columns:
                df["Distance"] = (
                    df["Distance"] - df["Distance"].iloc[0] + dist_acc
                )
                dist_acc = float(df["Distance"].iloc[-1]) + 1.0

            per_file_dfs.append(df)

        if not per_file_dfs:
            raise ValueError("None of the provided files yielded any rows.")

        self.df = (per_file_dfs[0] if len(per_file_dfs) == 1
                   else pd.concat(per_file_dfs, ignore_index=True))

    @staticmethod
    def _load_one(path: str) -> pd.DataFrame:
        """Dispatch on file extension and return a raw DataFrame (no
        normalisation, no unit fix-ups)."""
        ext = os.path.splitext(path)[1].lower()
        if ext == ".ld":
            return TelemetryAnalyzer._load_ld_static(path)
        if ext == ".ldx":
            ld_path = os.path.splitext(path)[0] + ".ld"
            if not os.path.isfile(ld_path):
                raise FileNotFoundError(
                    f".ldx file picked but its companion "
                    f"{os.path.basename(ld_path)} is missing. Drop both "
                    f"files in the same folder."
                )
            return TelemetryAnalyzer._load_ld_static(ld_path)
        return TelemetryAnalyzer._load_csv_static(path)

    @staticmethod
    def _load_ld_static(path: str) -> pd.DataFrame:
        from motec_ld import ld_to_dataframe
        return ld_to_dataframe(path)

    # ---- loading ----------------------------------------------------------
    @staticmethod
    def _normalise(name: str) -> str:
        """Strip i2 Pro's whitespace/units/punctuation from a column name."""
        out = []
        in_brackets = False
        for ch in name:
            if ch in "[(":
                in_brackets = True
                continue
            if ch in "])":
                in_brackets = False
                continue
            if in_brackets:
                continue
            if ch.isalnum():
                out.append(ch.lower())
        return "".join(out)

    def _load_ld(self, path: str) -> pd.DataFrame:
        return self._load_ld_static(path)

    def _load_csv(self, path: str) -> pd.DataFrame:
        return self._load_csv_static(path)

    @staticmethod
    def _load_csv_static(path: str) -> pd.DataFrame:
        """Find the channel-name header row in a MoTeC i2 Pro CSV.

        i2 Pro CSVs emit a `"Key","Value"` metadata preamble (mostly 2-field
        rows like ``"Sample Time","12:34:56"`` — note the dangerously-named
        "Sample Time"), then the wide channel-name row, then optionally a
        units row, then numeric data.

        Strategy: compare line widths, not keywords. Metadata rows have a
        handful of commas; the channel header and the data rows have many.
        We pick the first line whose comma count is close to the maximum.
        """
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        if not lines:
            return pd.DataFrame()

        comma_counts = [line.count(",") for line in lines]
        max_commas = max(comma_counts) if comma_counts else 0

        # If the file has no preamble at all, just hand it to pandas.
        if max_commas < 3:
            return pd.read_csv(path, low_memory=False, on_bad_lines="skip")

        # Header row = first row that's at least 80% as wide as the widest
        # row in the file (and has at least 5 fields, to skip stray
        # metadata rows that happen to contain commas).
        threshold = max(5, int(max_commas * 0.8))
        header_row = 0
        for i, c in enumerate(comma_counts):
            if c >= threshold:
                header_row = i
                break

        # Sniff a units row if the line right after the header is mostly
        # non-numeric (e.g. ``"s","m","%","kph",…``).
        skip_units = 0
        if header_row + 1 < len(lines):
            tokens = [t.strip().strip('"')
                      for t in lines[header_row + 1].split(",")]

            def _is_num(s: str) -> bool:
                try:
                    float(s.replace(",", ""))
                    return True
                except ValueError:
                    return False

            numeric = sum(1 for t in tokens if _is_num(t))
            if numeric < max(1, len(tokens) // 3):
                skip_units = 1

        # Tolerate occasional malformed rows ACC's exporter sometimes emits
        # at lap boundaries.
        try:
            df = pd.read_csv(path, skiprows=header_row, header=0,
                             skip_blank_lines=True, on_bad_lines="skip",
                             low_memory=False)
        except Exception:
            df = pd.read_csv(path, skiprows=header_row, header=0,
                             skip_blank_lines=True, on_bad_lines="skip",
                             low_memory=False, engine="python")

        if skip_units and len(df) > 0:
            df = df.iloc[1:].reset_index(drop=True)
        return df

    def _normalise_one(self, df: pd.DataFrame) -> pd.DataFrame:
        """Rename CSV/LD columns to our canonical names, coerce to numeric,
        normalise units (m/s² → g, m/s → km/h), and synthesise a Distance
        channel from Speed × dt when ACC didn't log one directly.

        DataFrame-in, DataFrame-out — does not touch ``self.df``, so it can
        be reused by the multi-file constructor for each file before they
        get concatenated.
        """
        rename: dict[str, str] = {}
        existing_cols = set(df.columns)
        norm_to_orig = {self._normalise(c): c for c in df.columns}

        for canonical, aliases in self.CHANNEL_ALIASES.items():
            if canonical in existing_cols or canonical in rename.values():
                continue
            for alias in aliases:
                if alias in norm_to_orig and norm_to_orig[alias] != canonical:
                    rename[norm_to_orig[alias]] = canonical
                    break

        if rename:
            df = df.rename(columns=rename)
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated(keep="first")]

        for col in df.columns:
            series = df[col]
            if isinstance(series, pd.DataFrame):
                series = series.iloc[:, 0]
            df[col] = pd.to_numeric(series, errors="coerce")

        # Unit normalisation: g and km/h.
        for g_col in ("G_Lat", "G_Lon"):
            if g_col in df.columns:
                peak = float(df[g_col].abs().max() or 0)
                if peak > 5:
                    df[g_col] = df[g_col] / 9.80665
        if "Speed" in df.columns:
            peak_speed = float(df["Speed"].abs().max() or 0)
            if peak_speed < 110:    # m/s
                df["Speed"] = df["Speed"] * 3.6

        # Distance synthesis from Speed × dt.
        if ("Distance" not in df.columns
                and "Time" in df.columns
                and "Speed" in df.columns):
            dt = df["Time"].diff().fillna(0)
            speed_ms = df["Speed"] / 3.6
            df["Distance"] = (speed_ms * dt).cumsum()

        if "Distance" in df.columns:
            df = df.dropna(subset=["Distance"]).reset_index(drop=True)
        else:
            df = df.dropna(how="all").reset_index(drop=True)

        # Susp_Travel min-of-four convenience column for bottoming detect.
        susp_cols = [c for c in df.columns if c.startswith("Susp_Travel")]
        if susp_cols and "Susp_Travel" not in df.columns:
            df["Susp_Travel"] = df[susp_cols].min(axis=1)
        return df

    # ---- lap detection ----------------------------------------------------
    def detect_laps(self) -> list[tuple[int, int]]:
        """Return ``[(start_idx, end_idx), …]`` per lap.

        With multiple files combined, file boundaries become hard
        lap-splits — each file gets its own lap range(s) so the user can
        select laps from different files independently. Within each file
        the Lap channel (or Distance reset) further splits laps.
        """
        # Split by source file first if multi-file is in play.
        if ("__file_idx" in self.df.columns
                and self.df["__file_idx"].nunique() > 1):
            laps: list[tuple[int, int]] = []
            for _file_idx, group in self.df.groupby("__file_idx"):
                s = int(group.index.min())
                e = int(group.index.max())
                laps.extend(self._detect_laps_in_range(s, e))
            return laps
        return self._detect_laps_in_range(0, len(self.df) - 1)

    def _detect_laps_in_range(self, s: int, e: int
                              ) -> list[tuple[int, int]]:
        """Detect laps within rows ``[s, e]``. Uses the Lap channel if
        present, else looks for Distance resets within the slice."""
        sub = self.df.iloc[s:e + 1]
        if "Lap" in sub.columns and sub["Lap"].notna().any():
            laps = []
            for lap_num, idxs in sub.groupby("Lap").groups.items():
                if pd.notna(lap_num):
                    laps.append((int(min(idxs)), int(max(idxs))))
            return sorted(laps, key=lambda g: g[0])

        if "Distance" not in sub.columns:
            return [(s, e)]
        diffs = sub["Distance"].diff().fillna(0)
        drops = list(diffs.index[diffs < -100].astype(int))
        if not drops:
            return [(s, e)]
        laps, prev = [], s
        for d in drops:
            laps.append((prev, int(d) - 1))
            prev = int(d)
        laps.append((prev, e))
        return laps

    # ---- auto corner detection -------------------------------------------
    def auto_detect_corners(
        self,
        lap_range: tuple[int, int] | None = None,
        min_brake_pct: float | None = None,
        min_gap_m: float | None = None,
    ) -> list[dict]:
        """Auto-detect corners from brake events.

        Returns a list of dicts: ``{index, start_idx, end_idx, start_dist,
        end_dist, peak_brake, min_speed, peak_glat, peak_steer}``.
        Each corner runs from brake-onset to where throttle returns above
        ``THROTTLE_OUT_THRESHOLD`` (i.e. genuine corner exit).
        """
        if "Brake" not in self.df.columns or "Distance" not in self.df.columns:
            return []
        min_brake = min_brake_pct if min_brake_pct is not None else self.BRAKE_CORNER_THRESHOLD
        gap = min_gap_m if min_gap_m is not None else self.MIN_CORNER_GAP_M

        s_idx = lap_range[0] if lap_range else 0
        e_idx = lap_range[1] if lap_range else len(self.df) - 1
        sub = self.df.iloc[s_idx:e_idx + 1]

        # Find contiguous regions where Brake > min_brake.
        above = (sub["Brake"] > min_brake).to_numpy()
        runs: list[tuple[int, int]] = []
        in_run = False
        run_s = 0
        for i, val in enumerate(above):
            if val and not in_run:
                run_s = i
                in_run = True
            elif not val and in_run:
                runs.append((run_s, i - 1))
                in_run = False
        if in_run:
            runs.append((run_s, len(above) - 1))

        # Translate local indices back to global, then extend each run forward
        # until the throttle returns to the "out" threshold (real corner exit).
        throttle = self.df["Throttle"] if "Throttle" in self.df.columns else None
        extended: list[tuple[int, int]] = []
        for ls, le in runs:
            gs = s_idx + ls
            ge = s_idx + le
            if throttle is not None:
                # walk forward up to ~300 samples or end of lap
                horizon = min(ge + 400, e_idx)
                tail = throttle.iloc[ge:horizon + 1]
                hits = tail.index[tail > self.THROTTLE_OUT_THRESHOLD]
                if len(hits):
                    ge = int(hits[0])
            extended.append((gs, ge))

        # Merge corners closer than `gap` metres.
        merged: list[tuple[int, int]] = []
        dist = self.df["Distance"]
        for s, e in extended:
            if merged:
                ps, pe = merged[-1]
                if dist.iloc[s] - dist.iloc[pe] < gap:
                    merged[-1] = (ps, e)
                    continue
            merged.append((s, e))

        # Build the output dicts.
        out: list[dict] = []
        for i, (s, e) in enumerate(merged, start=1):
            window = self.df.iloc[s:e + 1]
            out.append({
                "index":      i,
                "start_idx":  int(s),
                "end_idx":    int(e),
                "start_dist": float(dist.iloc[s]),
                "end_dist":   float(dist.iloc[e]),
                "peak_brake": float(window["Brake"].max()),
                "min_speed":  float(window["Speed"].min())
                              if "Speed" in window.columns else None,
                "peak_glat":  float(window["G_Lat"].abs().max())
                              if "G_Lat" in window.columns else None,
                "peak_steer": float(window["Steer"].abs().max())
                              if "Steer" in window.columns else None,
            })
        return out

    # ---- per-corner metrics + classification -----------------------------
    def corner_metrics(self, corner: dict) -> dict:
        """Engineering metrics for a single auto-detected corner."""
        s, e = corner["start_idx"], corner["end_idx"]
        window = self.df.iloc[s:e + 1]
        m = {
            "length_m":   float(self.df["Distance"].iloc[e] - self.df["Distance"].iloc[s]),
            "peak_brake": float(window["Brake"].max()) if "Brake" in window else None,
            "min_speed":  float(window["Speed"].min()) if "Speed" in window else None,
            "peak_glat":  float(window["G_Lat"].abs().max()) if "G_Lat" in window else None,
            "peak_steer": float(window["Steer"].abs().max()) if "Steer" in window else None,
        }
        if "Time" in window.columns and len(window) > 1:
            m["duration_s"] = float(window["Time"].iloc[-1] - window["Time"].iloc[0])
        return m

    def classify_corner(self, corner: dict) -> dict[str, list[tuple[str, str]]]:
        """Run all four issue classifiers on a corner.

        Returns ``{phase: [(issue, evidence), …]}`` where ``phase`` is one of
        ``"Entry"``, ``"Mid"``, ``"Exit"``, or ``"Corner"`` for whole-corner
        diagnostics (instability, bottoming).
        """
        s, e = corner["start_idx"], corner["end_idx"]
        corner_df = self.df.iloc[s:e + 1].reset_index(drop=True)
        phases = self.split_phases(corner_df)

        out: dict[str, list[tuple[str, str]]] = {}
        for phase_name, phase in phases.items():
            phase_issues: list[tuple[str, str]] = []
            ok, msg = self.validate_understeer(phase.df)
            if ok:
                phase_issues.append(("Understeer", msg))
            ok, msg = self.validate_oversteer(phase.df)
            if ok:
                phase_issues.append(("Oversteer", msg))
            if phase_issues:
                out[phase_name] = phase_issues

        # Whole-corner diagnostics.
        whole: list[tuple[str, str]] = []
        ok, msg = self.validate_instability(corner_df)
        if ok:
            whole.append(("Unstable", msg))
        ok, msg = self.validate_bottoming(corner_df)
        if ok:
            whole.append(("Bottoming", msg))
        if whole:
            out["Corner"] = whole

        return out

    # ---- diagnostic signals (used by the driver-input diagnose pipeline) ----
    def compute_signals(self, corner: dict, phase: str) -> dict:
        """Pull a set of physically-meaningful diagnostic signals out of the
        telemetry for a given corner + phase. Returned dict has only the
        keys we could actually compute from the available channels.

        Keys (all optional):
            min_FL/FR/RL/RR        — minimum susp travel mm in the phase
            front_roll_at_apex     — |FL-FR| susp travel at peak G_Lat (mm)
            rear_roll_at_apex      — |RL-RR| at peak G_Lat (mm)
            front_dive_rate        — peak |dSusp_F/dt| during braking (mm/s proxy)
            front_rebound_rate     — peak |dSusp_F/dt| in first 0.3s after brake-off
            g_per_deg              — peak |G_Lat| / peak |steer| (lateral-grip yield)
            counter_steer_events   — sign reversals of steer while still cornering
            brake_steer_rate       — mean |dSteer/dt| while Brake>30%
            steer_input_rms        — RMS of |dSteer/dt| during the phase
            throttle_app_rate      — mean dThrottle/dt during exit (driver smoothness)
            min_speed              — min vehicle speed (m/s or kph, whatever is logged)
            peak_glat              — peak |G_Lat| (g)
        """
        s, e = corner["start_idx"], corner["end_idx"]
        full = self.df.iloc[s:e + 1].reset_index(drop=True)
        if full.empty:
            return {}

        # Phase-specific slice.
        phase_slices = self.split_phases(full)
        if phase in phase_slices:
            phase_df = phase_slices[phase].df
        else:
            phase_df = full   # "Corner"-level (Unstable / Bottoming) or "*"

        sig: dict = {}

        # --- Suspension travel: bottoming + roll asymmetry ---
        for tag, col in (("FL", "Susp_Travel_FL"), ("FR", "Susp_Travel_FR"),
                         ("RL", "Susp_Travel_RL"), ("RR", "Susp_Travel_RR")):
            if col in full.columns and full[col].notna().any():
                sig[f"min_{tag}"] = float(full[col].min())

        if {"Susp_Travel_FL", "Susp_Travel_FR"} <= set(full.columns) \
                and "G_Lat" in full.columns and full["G_Lat"].abs().max() > 0:
            apex_idx = full["G_Lat"].abs().idxmax()
            sig["front_roll_at_apex"] = float(
                abs(full.loc[apex_idx, "Susp_Travel_FL"]
                    - full.loc[apex_idx, "Susp_Travel_FR"])
            )
        if {"Susp_Travel_RL", "Susp_Travel_RR"} <= set(full.columns) \
                and "G_Lat" in full.columns and full["G_Lat"].abs().max() > 0:
            apex_idx = full["G_Lat"].abs().idxmax()
            sig["rear_roll_at_apex"] = float(
                abs(full.loc[apex_idx, "Susp_Travel_RL"]
                    - full.loc[apex_idx, "Susp_Travel_RR"])
            )

        # --- Front dive on brakes / front rise on brake-off ---
        front_cols = [c for c in ("Susp_Travel_FL", "Susp_Travel_FR")
                      if c in full.columns]
        if "Brake" in full.columns and front_cols:
            f_avg = full[front_cols].mean(axis=1)
            d_susp = f_avg.diff().abs()
            brake_mask = full["Brake"] > 30
            if brake_mask.any():
                sig["front_dive_rate"] = float(d_susp[brake_mask].max() or 0)
            # Front rebound: max |dSusp/dt| in the ~0.3s after the brake
            # transitions from >30% to <5%.
            transitions = full.index[
                (full["Brake"].shift(1).fillna(0) > 30) & (full["Brake"] < 5)
            ]
            if len(transitions):
                first_off = int(transitions[0])
                horizon_end = min(first_off + 15, len(full))
                if horizon_end > first_off + 1:
                    rebound = f_avg.iloc[first_off:horizon_end].diff().abs()
                    if not rebound.empty:
                        sig["front_rebound_rate"] = float(rebound.max() or 0)

        # --- Yaw efficiency: how much G we get per degree of steering ---
        if "G_Lat" in full.columns and "Steer" in full.columns:
            peak_g = float(full["G_Lat"].abs().max())
            peak_steer = float(full["Steer"].abs().max())
            if peak_steer > 0.5:
                sig["g_per_deg"] = peak_g / peak_steer

        # --- Counter-steer events (rear losing grip mid/exit) ---
        if "Steer" in full.columns and "G_Lat" in full.columns and len(full) > 5:
            steer = full["Steer"].to_numpy()
            glat = full["G_Lat"].abs().to_numpy()
            zc = 0
            for i in range(1, len(steer)):
                if steer[i - 1] * steer[i] < 0 and glat[i] > 0.4:
                    zc += 1
            sig["counter_steer_events"] = zc

        # --- Brake stability + steering smoothness ---
        if "Brake" in full.columns and "Steer" in full.columns:
            brake_phase = full[full["Brake"] > 30]
            if len(brake_phase) > 5:
                sig["brake_steer_rate"] = float(
                    brake_phase["Steer"].diff().abs().mean() or 0
                )
        if "Steer" in phase_df.columns and len(phase_df) > 5:
            sig["steer_input_rms"] = float(
                ((phase_df["Steer"].diff() ** 2).mean()) ** 0.5
            )

        # --- Throttle application smoothness on exit ---
        if "Throttle" in full.columns:
            on_thr = full[full["Throttle"] > 20]
            if len(on_thr) > 5:
                sig["throttle_app_rate"] = float(
                    on_thr["Throttle"].diff().mean() or 0
                )

        # --- Headlines ---
        if "Speed" in full.columns:
            sig["min_speed"] = float(full["Speed"].min())
        if "G_Lat" in full.columns:
            sig["peak_glat"] = float(full["G_Lat"].abs().max())

        return sig

    # ---- per-fix telemetry support ----------------------------------------
    # The keys are matched as substrings (case-insensitive) against fix
    # labels from RECOMMENDATIONS in setup_optimizer.py. Each entry returns
    # ``(score, reason_template, signal_check)`` where:
    #   score          = base support score (0..1) when no specific signal
    #   reason         = human-readable reason string
    #   signal_check   = optional callable (signals, issue, phase)
    #                    → (bonus_score, reason_override or None).
    # The final score is base + bonus, clipped to [0, 1].

    @staticmethod
    def _support_for_fix(label: str, signals: dict, issue: str, phase: str
                         ) -> tuple[float, str]:
        """Return ``(confidence_in_0_to_1, reason_string)`` for one fix
        given the diagnostic signals for the corner.

        The function is deterministic and explainable — every score gets
        a reason the user can read. This is the heart of the
        "interpretation" the user wants: telemetry signal → fix priority.
        """
        ll = label.lower()

        # ---- Tyre pressure fixes — front ----
        if "front tyre pressure" in ll:
            base = 0.40
            g_per_deg = signals.get("g_per_deg")
            if g_per_deg is not None and g_per_deg < 0.30:
                return min(1.0, base + 0.40), (
                    f"Lateral G yields only {g_per_deg:.2f}g per degree of "
                    f"steering — front grip is saturating. Reducing pressure "
                    f"enlarges the contact patch, the highest-confidence "
                    f"first move."
                )
            return base, ("Front pressure is the lowest-risk first lever for "
                          "front-grip complaints.")

        # ---- Tyre pressure fixes — rear ----
        if "rear tyre pressure" in ll:
            base = 0.40
            cs = signals.get("counter_steer_events", 0)
            if cs >= 2:
                return min(1.0, base + 0.40), (
                    f"{cs} counter-steer event(s) detected — rear is "
                    f"slipping. Lower rear pressure → bigger contact patch "
                    f"→ less snap."
                )
            return base, ("Standard first move when the rear is breaking away.")

        # ---- Front anti-roll bar — soften ----
        if "front anti-roll bar" in ll and "less" in ll:
            roll = signals.get("front_roll_at_apex")
            if roll is not None and roll < 5.0:
                return 0.80, (
                    f"Front roll at apex only {roll:.1f}mm — front is too "
                    f"stiff and skating. Softening the front ARB will let "
                    f"the outside front load up and find grip."
                )
            if roll is not None and roll > 12.0:
                return 0.05, (
                    f"Front already rolling {roll:.1f}mm at apex — softening "
                    f"the front ARB further would just add more roll, "
                    f"unlikely to gain grip."
                )
            return 0.30, ("Standard mid-corner understeer move; modest "
                          "without supporting roll data.")

        # ---- Rear anti-roll bar — stiffen ----
        if "rear anti-roll bar" in ll and "more" in ll:
            roll = signals.get("rear_roll_at_apex")
            if roll is not None and roll > 8.0:
                return 0.75, (
                    f"Rear rolls {roll:.1f}mm at apex — stiffening will "
                    f"shift more lateral load to the rear and rotate the "
                    f"car."
                )
            return 0.30, "Adds rear load transfer to free up the front."

        # ---- Rear anti-roll bar — soften ----
        if "rear anti-roll bar" in ll and "less" in ll:
            cs = signals.get("counter_steer_events", 0)
            if cs >= 2:
                return 0.65, (
                    f"{cs} counter-steers — rear is breaking loose on exit. "
                    f"Softer rear ARB lets the inside rear stay loaded for "
                    f"power-down."
                )
            return 0.30, "Helps power-down by keeping the rear planted."

        # ---- Front bump (more/increase) ----
        if "front bump" in ll and ("more" in ll or "increase" in ll):
            min_f = min(filter(None, [signals.get("min_FL"),
                                       signals.get("min_FR")]),
                        default=None)
            dive = signals.get("front_dive_rate")
            if min_f is not None and min_f < 5.0:
                return 0.85, (
                    f"Front suspension reaching {min_f:.1f}mm — needs more "
                    f"bump damping to control entry dive."
                )
            if dive is not None and dive > 5.0:
                return 0.65, (
                    f"Front dive rate {dive:.1f} on brakes — stiffer bump "
                    f"will steady the front under load."
                )
            return 0.30, "Steadies front platform under brakes."

        # ---- Front rebound (more/increase) ----
        if "rebound" in ll and "front" in ll:
            reb = signals.get("front_rebound_rate")
            if reb is not None and reb > 5.0:
                return 0.65, (
                    f"Front rises rapidly off the brakes ({reb:.1f}) — more "
                    f"rebound holds the front loaded into turn-in."
                )
            return 0.35, "Keeps front planted as you release the brake."

        # ---- Front ride height — reduce ----
        if "ride height" in ll and "front" in ll and "reduce" in ll:
            return 0.30, ("Lowers front, improving aero rake and front "
                          "downforce.")

        # ---- Increase ride height (bottoming fix) ----
        if "ride height" in ll and "increase" in ll:
            min_any = min(filter(None, [signals.get("min_FL"),
                                         signals.get("min_FR"),
                                         signals.get("min_RL"),
                                         signals.get("min_RR")]),
                          default=None)
            if min_any is not None and min_any < 2.0:
                return 0.95, (
                    f"Suspension hitting {min_any:.1f}mm — definite "
                    f"bottoming. Raise ride height first."
                )
            if min_any is not None and min_any < 6.0:
                return 0.55, (
                    f"Suspension getting close to bottom ({min_any:.1f}mm) "
                    f"— extra clearance recommended."
                )
            return 0.40, "Adds clearance over kerbs and bumps."

        # ---- Bumpstop range / fast bump ----
        if "bumpstop" in ll:
            min_any = min(filter(None, [signals.get("min_FL"),
                                         signals.get("min_FR"),
                                         signals.get("min_RL"),
                                         signals.get("min_RR")]),
                          default=None)
            if min_any is not None and min_any < 4.0:
                return 0.65, (f"Travel down to {min_any:.1f}mm — wider "
                              f"bumpstop window absorbs the impact.")
            return 0.40, "Softens the contact when riding kerbs."
        if "fast bump" in ll and ("less" in ll or "reduce" in ll):
            return 0.45, "Lets the suspension yield to sharp kerb impacts."

        # ---- Caster / camber / toe / brake bias / wing / preload ----
        if "caster" in ll:
            cs = signals.get("counter_steer_events", 0)
            if cs >= 2:
                return 0.45, ("More caster increases self-aligning torque "
                              "→ helps the rear feel more stable in your "
                              "counter-steer events.")
            return 0.30, "Adds straight-line stability and dynamic camber."

        if "camber" in ll and "front" in ll:
            g_per_deg = signals.get("g_per_deg")
            if g_per_deg is not None and g_per_deg < 0.32:
                return 0.50, (f"Front lateral grip low ({g_per_deg:.2f} g/°) "
                              f"— more negative front camber adds peak grip "
                              f"under roll.")
            return 0.30, "Aligns the outside-front contact patch under roll."

        if "camber" in ll and "rear" in ll:
            cs = signals.get("counter_steer_events", 0)
            if cs >= 2:
                return 0.55, (f"{cs} counter-steers — more rear negative "
                              f"camber expands the rear grip envelope.")
            return 0.30, "Improves rear lateral grip under roll."

        if "brake bias" in ll and "rearward" in ll:
            return 0.35, ("Rearward bias reduces front lock-up and saves "
                          "the front tyre for the corner.")

        if "rear wing" in ll and ("more" in ll or "increase" in ll):
            cs = signals.get("counter_steer_events", 0)
            if cs >= 3:
                return 0.65, (f"{cs} counter-steers — more rear downforce "
                              f"settles high-speed yaw.")
            return 0.30, "Adds rear stability at speed."

        if "preload" in ll and ("decrease" in ll or "less" in ll):
            return 0.30, ("Diff opens more easily → less driven inside-rear "
                          "tyre on exit.")

        if "toe out" in ll and "more" in ll and "front" in ll:
            return 0.30, "Sharper turn-in bite."

        if "front toe" in ll and "less" in ll:
            return 0.30, "Calms the initial yaw input — helps entry stability."

        if "less toe" in ll or "less overall toe" in ll:
            return 0.30, ("Reduces scrub & yaw twitches — calmer car at "
                          "speed.")

        if "more camber" in ll and "all" in ll:
            return 0.30, "More compliance to roll across the whole car."

        # Fallback
        return 0.25, "Driver61-recommended; no specific telemetry signature."

    def diagnose(self, corner: dict, issue: str, phase: str) -> dict:
        """Combine driver input (issue + phase) with the telemetry for a
        corner and return a ranked list of Driver61 fixes.

        Returns:
            ``{
                "issue": str, "phase": str,
                "signals": {…},
                "fixes": [
                    {"label": str, "method": str,
                     "score": 0..1, "reason": str, "rank": int},
                    …
                ],
            }``

        The score is a confidence in [0, 1]:
            ≥ 0.7  → telemetry strongly supports this fix
            0.4–0.7 → moderate confidence
            < 0.4  → low confidence; included for completeness
        """
        # Late import to avoid circular dep — RECOMMENDATIONS is in this module.
        recs = (RECOMMENDATIONS.get((issue, phase))
                or RECOMMENDATIONS.get((issue, "*"), []))
        if not recs:
            return {"issue": issue, "phase": phase, "signals": {}, "fixes": []}

        signals = self.compute_signals(corner, phase)
        fixes = []
        for label, method in recs:
            score, reason = self._support_for_fix(label, signals, issue, phase)
            fixes.append({
                "label": label, "method": method,
                "score": float(score), "reason": reason,
            })
        # Stable ranking: telemetry score descending, then preserve original
        # Driver61 priority for ties.
        fixes_indexed = list(enumerate(fixes))
        fixes_indexed.sort(key=lambda kv: (-kv[1]["score"], kv[0]))
        ranked = []
        for new_rank, (_orig, fx) in enumerate(fixes_indexed, 1):
            fx["rank"] = new_rank
            ranked.append(fx)
        return {
            "issue": issue, "phase": phase,
            "signals": signals, "fixes": ranked,
        }

    def analyze_range(self, dist_start: float, dist_end: float,
                      label: str = "") -> dict | None:
        """Analyze a user-picked distance range as if it were a single corner.

        Returns the same shape as one entry of `analyze()`, or `None` if the
        range has too few samples to be meaningful.
        """
        if "Distance" not in self.df.columns:
            return None
        if dist_end < dist_start:
            dist_start, dist_end = dist_end, dist_start
        mask = ((self.df["Distance"] >= dist_start)
                & (self.df["Distance"] <= dist_end))
        idxs = self.df.index[mask]
        if len(idxs) < 5:
            return None

        s, e = int(idxs.min()), int(idxs.max())
        window = self.df.iloc[s:e + 1]
        corner = {
            "index":      0,
            "label":      label or f"User range {dist_start:.0f}-{dist_end:.0f}m",
            "start_idx":  s,
            "end_idx":    e,
            "start_dist": float(self.df["Distance"].iloc[s]),
            "end_dist":   float(self.df["Distance"].iloc[e]),
            "peak_brake": float(window["Brake"].max())
                          if "Brake" in window.columns else None,
            "min_speed":  float(window["Speed"].min())
                          if "Speed" in window.columns else None,
            "peak_glat":  float(window["G_Lat"].abs().max())
                          if "G_Lat" in window.columns else None,
            "peak_steer": float(window["Steer"].abs().max())
                          if "Steer" in window.columns else None,
        }
        corner["metrics"] = self.corner_metrics(corner)
        classified = self.classify_corner(corner)
        flat: list[tuple[str, str, str]] = []
        for phase, items in classified.items():
            for issue, evidence in items:
                flat.append((phase, issue, evidence))
        corner["issues"] = flat
        return corner

    def analyze(self, lap_range: tuple[int, int] | None = None) -> list[dict]:
        """One-shot analysis: detect corners, compute metrics, classify issues.

        Returns a list of corner dicts augmented with ``metrics`` and
        ``issues`` (a flat list of ``(phase, issue, evidence)`` tuples ready
        for display).
        """
        results = []
        for corner in self.auto_detect_corners(lap_range=lap_range):
            corner["metrics"] = self.corner_metrics(corner)
            classified = self.classify_corner(corner)
            flat: list[tuple[str, str, str]] = []
            for phase, items in classified.items():
                for issue, evidence in items:
                    flat.append((phase, issue, evidence))
            corner["issues"] = flat
            results.append(corner)
        return results

    # ---- slicing ----------------------------------------------------------
    def slice_corner(self, dist_start: float, dist_end: float) -> pd.DataFrame:
        mask = (self.df["Distance"] >= dist_start) & (self.df["Distance"] <= dist_end)
        return self.df.loc[mask].reset_index(drop=True)

    def split_phases(self, corner_df: pd.DataFrame) -> dict[str, PhaseSlice]:
        """Split a corner slice into Entry / Mid / Exit per the spec."""
        if corner_df.empty:
            return {p: PhaseSlice(p, corner_df) for p in ("Entry", "Mid", "Exit")}

        steer = corner_df["Steer"].abs()
        brake = corner_df["Brake"]
        throttle = corner_df["Throttle"]

        entry_mask = brake > self.BRAKE_ENTRY_THRESHOLD
        # Mid-corner: steer is near maximum AND we're off-throttle.
        steer_peak = steer.max() if len(steer) else 0.0
        mid_mask = (steer >= 0.85 * steer_peak) & (throttle < self.THROTTLE_MID_THRESHOLD)
        exit_mask = throttle > self.THROTTLE_EXIT_THRESHOLD

        return {
            "Entry": PhaseSlice("Entry", corner_df.loc[entry_mask].reset_index(drop=True)),
            "Mid":   PhaseSlice("Mid",   corner_df.loc[mid_mask].reset_index(drop=True)),
            "Exit":  PhaseSlice("Exit",  corner_df.loc[exit_mask].reset_index(drop=True)),
        }

    # ---- validation -------------------------------------------------------
    def validate_understeer(self, phase_df: pd.DataFrame) -> tuple[bool, str]:
        """Understeer = increasing steer demand without rising lateral G.

        Physically: the front tyres have saturated their grip envelope, so
        adding more steering angle no longer translates into more cornering
        force — the car pushes wide.
        """
        if phase_df.empty or len(phase_df) < 5:
            return False, "Not enough samples in this phase to validate."

        steer = phase_df["Steer"].abs()
        glat = phase_df["G_Lat"].abs()

        # Compare first-third vs last-third of the phase.
        third = max(1, len(phase_df) // 3)
        steer_delta = steer.iloc[-third:].mean() - steer.iloc[:third].mean()
        glat_delta = glat.iloc[-third:].mean() - glat.iloc[:third].mean()

        confirmed = steer_delta > 0.05 * max(1.0, steer.max()) and glat_delta <= 0.05
        msg = (f"Steer Δ={steer_delta:+.2f}, G_Lat Δ={glat_delta:+.2f}g "
               f"(steer rising, lateral grip flat → understeer "
               f"{'CONFIRMED' if confirmed else 'NOT confirmed'})")
        return confirmed, msg

    def validate_oversteer(self, phase_df: pd.DataFrame) -> tuple[bool, str]:
        """Oversteer = driver applies COUNTERSTEER (steer reduces / reverses)
        while lateral G remains high. Rear has lost grip before the front.
        """
        if phase_df.empty or len(phase_df) < 5:
            return False, "Not enough samples in this phase to validate."

        steer = phase_df["Steer"]
        glat = phase_df["G_Lat"].abs()

        third = max(1, len(phase_df) // 3)
        steer_drop = steer.abs().iloc[:third].mean() - steer.abs().iloc[-third:].mean()
        glat_avg = glat.mean()

        confirmed = steer_drop > 0.05 * max(1.0, steer.abs().max()) and glat_avg > 0.5
        msg = (f"Steer reduction={steer_drop:+.2f} (counter-steer), "
               f"avg |G_Lat|={glat_avg:.2f}g — oversteer "
               f"{'CONFIRMED' if confirmed else 'NOT confirmed'}")
        return confirmed, msg

    def validate_instability(self, corner_df: pd.DataFrame) -> tuple[bool, str]:
        """Instability = high-frequency steering corrections. Look at the
        standard deviation of steer rate (derivative)."""
        if corner_df.empty or len(corner_df) < 10:
            return False, "Not enough samples to validate."
        steer_rate = corner_df["Steer"].diff().abs()
        rms = (steer_rate ** 2).mean() ** 0.5
        confirmed = rms > 0.05  # heuristic — tune per car/wheel
        return confirmed, f"Steer-rate RMS={rms:.3f} → {'unstable' if confirmed else 'stable'}"

    def validate_bottoming(self, corner_df: pd.DataFrame) -> tuple[bool, str]:
        """Bottoming = suspension travel reaches its mechanical limit (0 mm
        or whatever the channel's minimum is)."""
        if "Susp_Travel" not in corner_df.columns or corner_df["Susp_Travel"].dropna().empty:
            return False, "Susp_Travel channel not found in telemetry."
        st = corner_df["Susp_Travel"].dropna()
        # Heuristic: bottoming if min travel within 2% of channel's global min.
        global_min = self.df["Susp_Travel"].min()
        confirmed = st.min() <= global_min * 1.02 if global_min > 0 else st.min() <= 0.5
        return confirmed, f"Min Susp_Travel in corner = {st.min():.2f} (channel min={global_min:.2f})"


# ---------------------------------------------------------------------------
# Setup manager
# ---------------------------------------------------------------------------
class SetupManager:
    """Reads, modifies, and writes an ACC setup.json. Tracks every adjustment
    for the engineering report at the end of the session."""

    # Severity → click multiplier for the user's complaint sliders.
    # The mapping is intentionally non-linear so a 1/5 mild call produces
    # a single click (Driver61's recommended unit step) and a 5/5 severe
    # call only goes up to 4 clicks (multi-mm ride-height moves are rare
    # even on truly broken setups).
    SEVERITY_TO_CLICKS: dict[int, int] = {
        1: 1, 2: 1, 3: 2, 4: 3, 5: 4,
    }

    def __init__(self, json_path: str) -> None:
        self.json_path = json_path
        with open(json_path, "r", encoding="utf-8") as fh:
            self.setup: dict[str, Any] = json.load(fh)
        self.original = copy.deepcopy(self.setup)
        self.changes: list[str] = []
        # Click multiplier honoured by every _adj_* call. Driven by the
        # severity slider in the GUI via `adjustment_scale` below — defaults
        # to 1 so legacy callers keep their old single-click behaviour.
        self._click_multiplier: int = 1
        # Indices into `self.changes` for the active temperature-comp entry.
        # `set_temperature` rewrites these in place rather than appending,
        # so the queue stays clean while a slider is being dragged.
        self._temp_change_indices: list[int] = []

    @contextlib.contextmanager
    def adjustment_scale(self, multiplier: int):
        """Within the with-block, multiply every click delta by `multiplier`.
        Restored on exit. Use ``SEVERITY_TO_CLICKS[severity]`` to translate
        a 1-5 severity rating into a multiplier.
        """
        old = self._click_multiplier
        self._click_multiplier = max(1, int(multiplier))
        try:
            yield
        finally:
            self._click_multiplier = old

    # ---- low-level helpers -----------------------------------------------
    def _adj_array(self, path: list[str], indices: list[int], delta: int,
                   label: str, reason: str) -> None:
        """Adjust selected indices in an array-valued field by `delta`
        clicks (scaled by the active severity multiplier)."""
        scaled = delta * self._click_multiplier
        node = self.setup
        for key in path[:-1]:
            node = node[key]
        arr = node[path[-1]]
        before = list(arr)
        for i in indices:
            arr[i] = max(0, arr[i] + scaled)   # clicks can't go below 0
        after = list(arr)
        sev_note = (f"  [severity ×{self._click_multiplier}]"
                    if self._click_multiplier > 1 else "")
        self.changes.append(
            f"  • {label}: {before} → {after}  ({scaled:+d} clicks on idx {indices}){sev_note}\n"
            f"      Why: {reason}"
        )

    def _adj_scalar(self, path: list[str], delta: int, label: str, reason: str) -> None:
        scaled = delta * self._click_multiplier
        node = self.setup
        for key in path[:-1]:
            node = node[key]
        before = node[path[-1]]
        node[path[-1]] = max(0, before + scaled)
        after = node[path[-1]]
        sev_note = (f"  [severity ×{self._click_multiplier}]"
                    if self._click_multiplier > 1 else "")
        self.changes.append(
            f"  • {label}: {before} → {after}  ({scaled:+d} clicks){sev_note}\n"
            f"      Why: {reason}"
        )

    # ---- semantic helpers (Front = idx 0,1; Rear = idx 2,3) --------------
    FRONT = [0, 1]
    REAR = [2, 3]

    # Temperature compensation: ACC's optimum HOT pressure window is roughly
    # constant per car. As ambient air gets warmer, the tyre absorbs more
    # heat over a stint, so the COLD pressure has to start lower to land in
    # the same hot window. The base setup file is calibrated for 20°C ambient.
    BASELINE_AMBIENT_C = 20.0
    DEG_C_PER_CLICK = 1.5  # generic GT3 default

    # Per-car overrides for temperature sensitivity. The 992 GT3 R runs
    # slightly cooler tyres than the Audi/BMW field (rear-engine weight
    # distribution loads the rears more evenly, less heat spike on warm-ups),
    # so it needs a marginally smaller pressure correction per °C than the
    # generic GT3 number — i.e. a slightly LARGER °C-per-click value.
    CAR_TEMP_RATES: dict[str, float] = {
        "porsche_992_gt3_r": 1.7,
    }

    def adjust_for_temperature(
        self,
        target_temp_c: float,
        baseline_temp_c: float | None = None,
        deg_c_per_click: float | None = None,
    ) -> None:
        """Compensate cold tyre pressures for ambient temperature delta.

        Physics: a hotter track day pumps more energy into the tyre, raising
        its operating pressure. To keep the HOT pressure in the manufacturer
        window we lower the COLD starting pressure. The reverse is true on a
        cold day. Adjusts the top-level tyre pressures AND the pit-stop
        strategy pressures so an in-race tyre change uses the same target.
        """
        baseline = baseline_temp_c if baseline_temp_c is not None else self.BASELINE_AMBIENT_C
        if deg_c_per_click is not None:
            rate = deg_c_per_click
        else:
            car = self.setup.get("carName", "")
            rate = self.CAR_TEMP_RATES.get(car, self.DEG_C_PER_CLICK)
        delta = target_temp_c - baseline
        clicks = -int(round(delta / rate))  # warmer → fewer clicks (lower psi)

        if clicks == 0:
            self.changes.append(
                f"  • Tyre temperature compensation: target {target_temp_c:.1f}°C "
                f"vs {baseline:.1f}°C baseline (Δ={delta:+.1f}°C) — within click "
                f"resolution, no pressure change."
            )
            return

        arr = self.setup["basicSetup"]["tyres"]["tyrePressure"]
        before = list(arr)
        for i in range(4):
            arr[i] = max(0, arr[i] + clicks)
        after = list(arr)

        # Mirror the change into every pit-stop tyre pressure entry.
        pit_lines: list[str] = []
        strategy = self.setup.get("basicSetup", {}).get("strategy", {})
        for idx, stop in enumerate(strategy.get("pitStrategy", []) or []):
            pit_arr = stop.get("tyres", {}).get("tyrePressure")
            if isinstance(pit_arr, list) and len(pit_arr) == 4:
                pit_before = list(pit_arr)
                for i in range(4):
                    pit_arr[i] = max(0, pit_arr[i] + clicks)
                pit_lines.append(f"      Pit stop #{idx + 1}: {pit_before} → {list(pit_arr)}")

        direction = "warmer" if delta > 0 else "cooler"
        msg = (
            f"  • Tyre temperature compensation: {before} → {after}  "
            f"({clicks:+d} clicks on all 4)\n"
            f"      Why: target {target_temp_c:.1f}°C is {abs(delta):.1f}°C "
            f"{direction} than the {baseline:.1f}°C baseline. At ~{rate:.1f}°C "
            f"per click, hotter air → hotter tyres → start COLD pressures lower "
            f"so HOT pressures land in the optimum window (and vice versa)."
        )
        if pit_lines:
            msg += "\n" + "\n".join(pit_lines)
        self.changes.append(msg)

    def set_temperature(self,
                        target_temp_c: float,
                        baseline_temp_c: float | None = None,
                        deg_c_per_click: float | None = None) -> None:
        """Auto-set tyre pressures from BASE setup pressures + temperature
        delta. Idempotent — calling repeatedly with the same target gives
        the same result, so it's safe to bind to a slider that fires on
        every drag step.

        Logic:
            base = self.original.tyrePressure  (the loaded setup, treated as
                                                 the 20°C calibration)
            clicks = -round((target - baseline) / rate)
            new_pressure[i] = max(0, base[i] + clicks)

        Replaces any prior temperature entry in `self.changes` so the queue
        only ever shows one active temperature line.
        """
        baseline = (baseline_temp_c if baseline_temp_c is not None
                    else self.BASELINE_AMBIENT_C)
        if deg_c_per_click is not None:
            rate = deg_c_per_click
        else:
            car = self.setup.get("carName", "")
            rate = self.CAR_TEMP_RATES.get(car, self.DEG_C_PER_CLICK)

        delta = target_temp_c - baseline
        clicks = -int(round(delta / rate))

        # Read base pressures from the snapshot we took at load time.
        base_pressures = list(
            self.original["basicSetup"]["tyres"]["tyrePressure"])
        new_pressures = [max(0, p + clicks) for p in base_pressures]

        # Apply absolutely (overwrite, don't accumulate).
        arr = self.setup["basicSetup"]["tyres"]["tyrePressure"]
        for i in range(4):
            arr[i] = new_pressures[i]

        # Mirror to every pit-stop strategy entry.
        for stop in (self.setup.get("basicSetup", {})
                     .get("strategy", {})
                     .get("pitStrategy", []) or []):
            pit_arr = stop.get("tyres", {}).get("tyrePressure")
            if isinstance(pit_arr, list) and len(pit_arr) == 4:
                for i in range(4):
                    pit_arr[i] = new_pressures[i]

        # Strip prior temperature-comp lines from the change log.
        if self._temp_change_indices:
            for idx in sorted(self._temp_change_indices, reverse=True):
                if 0 <= idx < len(self.changes):
                    del self.changes[idx]
            self._temp_change_indices.clear()

        # Re-append a fresh entry only when there's an actual delta.
        if clicks != 0:
            direction = "warmer" if delta > 0 else "cooler"
            self._temp_change_indices.append(len(self.changes))
            self.changes.append(
                f"\n[TEMP COMP @ {target_temp_c:.0f}°C — "
                f"{abs(delta):.0f}°C {direction} than {baseline:.0f}°C base]"
            )
            self._temp_change_indices.append(len(self.changes))
            self.changes.append(
                f"  • Tyre pressures: {base_pressures} → {new_pressures}  "
                f"({clicks:+d} clicks all 4)\n"
                f"      Why: target {target_temp_c:.1f}°C is {abs(delta):.1f}°C "
                f"{direction} than the {baseline:.0f}°C base; "
                f"@ ~{rate:.1f}°C per click."
            )

    def reduce_front_tyre_pressure(self) -> None:
        self._adj_array(
            ["basicSetup", "tyres", "tyrePressure"], self.FRONT, -1,
            "Front tyre pressure",
            "Lower fronts → larger contact patch → more peak grip & better "
            "thermal load distribution at the front axle.",
        )

    def reduce_rear_tyre_pressure(self) -> None:
        self._adj_array(
            ["basicSetup", "tyres", "tyrePressure"], self.REAR, -1,
            "Rear tyre pressure",
            "Lower rears → bigger contact patch & lower lateral stiffness → "
            "rear axle generates more grip and is less prone to snap.",
        )

    def more_front_toe_out(self) -> None:
        # In ACC, the toe array stores click indices where lower = more toe out
        # at the front. We decrement to add toe-out.
        self._adj_array(
            ["basicSetup", "alignment", "toe"], self.FRONT, -1,
            "Front toe-out",
            "More toe-out → outside front tyre is already pointed into the "
            "corner at turn-in → faster initial steering response.",
        )

    def less_front_toe_out(self) -> None:
        self._adj_array(
            ["basicSetup", "alignment", "toe"], self.FRONT, +1,
            "Front toe (less toe-out)",
            "Less toe-out → reduced initial yaw moment on turn-in → calmer "
            "front end, less of a yaw spike that can provoke entry oversteer.",
        )

    def less_overall_toe(self) -> None:
        self._adj_array(
            ["basicSetup", "alignment", "toe"], [0, 1, 2, 3], +1,
            "Toe (all corners, less aggressive)",
            "Reducing absolute toe values → less scrub & less yaw twitchiness "
            "→ more straight-line and high-speed stability.",
        )

    def more_front_camber(self) -> None:
        self._adj_array(
            ["basicSetup", "alignment", "camber"], self.FRONT, +1,
            "Front camber (more negative)",
            "More negative camber compensates for body roll → outside front "
            "tyre stays flatter on the road at high G → more lateral grip.",
        )

    def more_rear_camber(self) -> None:
        self._adj_array(
            ["basicSetup", "alignment", "camber"], self.REAR, +1,
            "Rear camber (more negative)",
            "More negative rear camber → rear contact patch optimised under "
            "roll → more lateral grip at the rear axle.",
        )

    def more_camber_all(self) -> None:
        self._adj_array(
            ["basicSetup", "alignment", "camber"], [0, 1, 2, 3], +1,
            "Camber (all four, more negative)",
            "More negative camber across the car → flatter contact patches in "
            "the cornering attitude → broader grip envelope → more stability.",
        )

    def more_caster(self) -> None:
        delta = 1 * self._click_multiplier
        before_lf = self.setup["basicSetup"]["alignment"]["casterLF"]
        before_rf = self.setup["basicSetup"]["alignment"]["casterRF"]
        self.setup["basicSetup"]["alignment"]["casterLF"] = before_lf + delta
        self.setup["basicSetup"]["alignment"]["casterRF"] = before_rf + delta
        sev_note = (f"  [severity ×{self._click_multiplier}]"
                    if self._click_multiplier > 1 else "")
        self.changes.append(
            f"  • Caster: ({before_lf}, {before_rf}) → "
            f"({before_lf + delta}, {before_rf + delta})  "
            f"(+{delta} click each){sev_note}\n"
            "      Why: More caster → more dynamic camber under steering → "
            "more front grip mid/exit AND stronger self-centring → straight-line stability."
        )

    def increase_front_rebound(self) -> None:
        self._adj_array(
            ["advancedSetup", "dampers", "reboundSlow"], self.FRONT, +1,
            "Front rebound (slow)",
            "Stiffer front rebound → front of car stays loaded longer after "
            "brake release → more front grip carried into turn-in.",
        )

    def increase_front_bump(self) -> None:
        self._adj_array(
            ["advancedSetup", "dampers", "bumpSlow"], self.FRONT, +1,
            "Front bump (slow)",
            "Stiffer front bump → resists front-end dive → less weight "
            "transfer to the front under load → more rear grip under braking/turn-in.",
        )

    def reduce_front_ride_height(self) -> None:
        self._adj_array(
            ["advancedSetup", "aeroBalance", "rideHeight"], self.FRONT, -1,
            "Front ride height",
            "Lower front → front roll centre drops & more aero rake → more "
            "front downforce and grip.",
        )

    def increase_ride_height_all(self) -> None:
        self._adj_array(
            ["advancedSetup", "aeroBalance", "rideHeight"], [0, 1, 2, 3], +1,
            "Ride height (all corners)",
            "Higher ride height → more clearance over kerbs/bumps → no "
            "bottoming-out on the floor or splitter.",
        )

    def less_front_arb(self) -> None:
        self._adj_scalar(
            ["advancedSetup", "mechanicalBalance", "aRBFront"], -1,
            "Front anti-roll bar",
            "Softer front ARB → more independent front wheel travel & better "
            "mechanical grip under roll → reduces mid-corner understeer.",
        )

    def more_rear_arb(self) -> None:
        self._adj_scalar(
            ["advancedSetup", "mechanicalBalance", "aRBRear"], +1,
            "Rear anti-roll bar",
            "Stiffer rear ARB → rear axle takes more load transfer → looser "
            "rear → reduces understeer balance.",
        )

    def less_rear_arb(self) -> None:
        self._adj_scalar(
            ["advancedSetup", "mechanicalBalance", "aRBRear"], -1,
            "Rear anti-roll bar",
            "Softer rear ARB → rear axle keeps more independent grip on power "
            "→ helps with corner-exit oversteer / wheelspin.",
        )

    def brake_bias_rearward(self) -> None:
        # Lower brakeBias % = more rear bias. Conventional ACC convention.
        self._adj_scalar(
            ["advancedSetup", "mechanicalBalance", "brakeBias"], -1,
            "Brake bias",
            "More rearward bias → less front locking under trail-braking → "
            "front tyre stays in its grip window at corner exit (helps exit understeer).",
        )

    def increase_rear_wing(self) -> None:
        self._adj_scalar(
            ["advancedSetup", "aeroBalance", "rearWing"], +1,
            "Rear wing",
            "More rear wing → more rear downforce → more high-speed rear "
            "stability → cures high-speed instability under direction changes.",
        )

    def decrease_diff_preload(self) -> None:
        self._adj_scalar(
            ["advancedSetup", "drivetrain", "preload"], -1,
            "Differential preload",
            "Lower preload → diff opens more easily on throttle → inside rear "
            "tyre is less driven → reduces corner-exit oversteer/wheelspin.",
        )

    def increase_bumpstop_range(self) -> None:
        self._adj_array(
            ["advancedSetup", "mechanicalBalance", "bumpStopWindow"], [0, 1, 2, 3], +1,
            "Bumpstop range (window)",
            "Larger bumpstop window → more travel before hitting the stop → "
            "no harsh contact when riding kerbs.",
        )

    def reduce_fast_bump(self) -> None:
        self._adj_array(
            ["advancedSetup", "dampers", "bumpFast"], [0, 1, 2, 3], -1,
            "Fast bump (all corners)",
            "Softer fast bump → suspension absorbs sharp kerb/bump impacts "
            "instead of transmitting them to the chassis → car settles faster.",
        )

    # ---- track-baseline tuning (theoretical-fastest direction) ------------
    # Keys are the same track strings as TRACK_LAYOUTS in the GUI. Each
    # entry maps a target name to (json_path, indices_or_None, label).
    # Indices = None ⇒ scalar field; otherwise apply to those array indices.
    TUNE_TARGETS: dict[str, tuple[list[str], list[int] | None, str]] = {
        "rearWing":         (["advancedSetup", "aeroBalance", "rearWing"],
                             None, "Rear wing"),
        "rideHeight":       (["advancedSetup", "aeroBalance", "rideHeight"],
                             [0, 1, 2, 3], "Ride height (all)"),
        "frontRideHeight":  (["advancedSetup", "aeroBalance", "rideHeight"],
                             [0, 1], "Front ride height"),
        "rearRideHeight":   (["advancedSetup", "aeroBalance", "rideHeight"],
                             [2, 3], "Rear ride height"),
        "aRBFront":         (["advancedSetup", "mechanicalBalance", "aRBFront"],
                             None, "Front anti-roll bar"),
        "aRBRear":          (["advancedSetup", "mechanicalBalance", "aRBRear"],
                             None, "Rear anti-roll bar"),
        "preload":          (["advancedSetup", "drivetrain", "preload"],
                             None, "Differential preload"),
        "brakeBias":        (["advancedSetup", "mechanicalBalance", "brakeBias"],
                             None, "Brake bias"),
        "frontBumpSlow":    (["advancedSetup", "dampers", "bumpSlow"],
                             [0, 1], "Front bump (slow)"),
        "frontReboundSlow": (["advancedSetup", "dampers", "reboundSlow"],
                             [0, 1], "Front rebound (slow)"),
        "bumpStopWindow":   (["advancedSetup", "mechanicalBalance",
                              "bumpStopWindow"], [0, 1, 2, 3],
                             "Bumpstop window"),
    }

    def apply_track_tuning(self, track_key: str) -> int:
        """Move the setup toward the engineering baseline for this track.

        Each track's profile is a small list of click deltas on aero,
        mechanical balance, dampers and diff — the same parameters i2 Pro
        engineers tweak when moving between low-downforce (Monza) and
        high-downforce (Hungaroring) circuits. Conservative numbers
        (1-4 clicks) so it nudges, not slams, the setup.

        Returns the number of adjustments queued in `self.changes`.
        """
        profile = TRACK_TUNING_PROFILES.get(track_key.lower())
        if not profile:
            return 0

        self.changes.append(
            f"\n[TRACK BASELINE — {track_key.upper()}: "
            f"{profile['label']}]"
        )
        applied = 0
        for target_key, delta, reason in profile["adjustments"]:
            if delta == 0:
                continue
            spec = self.TUNE_TARGETS.get(target_key)
            if spec is None:
                continue
            path, indices, label = spec
            if indices is None:
                self._adj_scalar(path, delta, label, reason)
            else:
                self._adj_array(path, indices, delta, label, reason)
            applied += 1
        return applied

    # ---- save ------------------------------------------------------------
    def save(self, out_path: str) -> None:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(self.setup, fh, indent=4)


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------
# Maps (issue, phase) → ordered list of (label, SetupManager method name).
# The order reflects Driver61's recommended priority — apply the first item
# whose change doesn't conflict with an earlier session adjustment.
RECOMMENDATIONS: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("Understeer", "Entry"): [
        ("Reduce front tyre pressures", "reduce_front_tyre_pressure"),
        ("More front toe-out",          "more_front_toe_out"),
        ("More front camber",           "more_front_camber"),
        ("Increase front rebound",      "increase_front_rebound"),
        ("Reduce front ride height",    "reduce_front_ride_height"),
    ],
    ("Understeer", "Mid"): [
        ("Reduce front tyre pressures", "reduce_front_tyre_pressure"),
        ("Less front anti-roll bar",    "less_front_arb"),
        ("More rear anti-roll bar",     "more_rear_arb"),
        ("Reduce front ride height",    "reduce_front_ride_height"),
    ],
    ("Understeer", "Exit"): [
        ("Reduce front tyre pressures", "reduce_front_tyre_pressure"),
        ("More caster",                 "more_caster"),
        ("Brake bias rearward",         "brake_bias_rearward"),
    ],
    ("Oversteer", "Entry"): [
        ("Reduce rear tyre pressures",  "reduce_rear_tyre_pressure"),
        ("Less front toe-out",          "less_front_toe_out"),
        ("More rear camber",            "more_rear_camber"),
        ("Increase front bump",         "increase_front_bump"),
    ],
    ("Oversteer", "Mid"): [
        ("Reduce rear tyre pressures",  "reduce_rear_tyre_pressure"),
        ("Less front anti-roll bar",    "less_front_arb"),
        ("More rear anti-roll bar",     "more_rear_arb"),
        ("Increase front bump",         "increase_front_bump"),
    ],
    ("Oversteer", "Exit"): [
        ("Reduce rear tyre pressures",  "reduce_rear_tyre_pressure"),
        ("More rear camber",            "more_rear_camber"),
        ("Less rear anti-roll bar",     "less_rear_arb"),
        ("Decrease diff preload",       "decrease_diff_preload"),
    ],
    ("Unstable", "*"): [
        ("Reduce rear tyre pressures",  "reduce_rear_tyre_pressure"),
        ("Less toe (overall)",          "less_overall_toe"),
        ("More camber (all)",           "more_camber_all"),
        ("More caster",                 "more_caster"),
        ("Increase rear wing",          "increase_rear_wing"),
    ],
    ("Bottoming", "*"): [
        ("Increase ride height",        "increase_ride_height_all"),
        ("Increase bumpstop range",     "increase_bumpstop_range"),
        ("Reduce fast bump",            "reduce_fast_bump"),
    ],
}


# ---------------------------------------------------------------------------
# Track tuning profiles — "theoretical-fastest" baselines per ACC track.
# ---------------------------------------------------------------------------
# Each profile is a list of (target, delta_clicks, reason) tuples. Targets
# resolve via SetupManager.TUNE_TARGETS to a JSON path + indices. The deltas
# are intentionally small (1-4 clicks) so the result NUDGES the setup
# toward the track's known engineering preference rather than rewriting it.
#
# Engineering rationale per category:
#   - Top-speed tracks (long straights)    → less wing, lower diff preload
#   - Slow technical tracks                → more wing, stiffer ARBs
#   - Bumpy / kerby tracks                 → higher RH, wider bumpstop window
#   - Smooth tracks                        → lower RH for aero gain
#   - Heavy braking zones                  → more rearward brake bias
TRACK_TUNING_PROFILES: dict[str, dict] = {
    "monza": {
        "label": "Low downforce — long straights, three chicanes",
        "adjustments": [
            ("rearWing",  -4, "Long straights — every wing click costs ~0.5 km/h top speed."),
            ("aRBFront",  -1, "Soften the front for chicane bite without snap."),
            ("preload",   -2, "Lower preload helps the diff open in T1/T4/Ascari."),
            ("brakeBias", -1, "Heavy late braking — rearward bias prevents front lock."),
        ],
    },
    "spa": {
        "label": "Medium downforce — fast flowing, Eau Rouge stability",
        "adjustments": [
            ("aRBRear",         +1, "Stiffer rear for Pouhon and Blanchimont yaw stability."),
            ("frontRideHeight", +1, "Eau Rouge compression needs front clearance."),
        ],
    },
    "hungaroring": {
        "label": "High downforce — slow, twisty, almost no straights",
        "adjustments": [
            ("rearWing", +4, "Almost no straights — maximum downforce wins."),
            ("aRBFront", +1, "Stiffer front for sharper direction changes."),
            ("preload",  -2, "Lower preload for tight T1/T2 rotation."),
        ],
    },
    "imola": {
        "label": "Medium-high downforce — chicanes and big kerbs",
        "adjustments": [
            ("rearWing",       +1, "Tamburello and Acque Minerali need stable rear."),
            ("bumpStopWindow", +2, "Variante Alta + Rivazza kerbs."),
            ("frontBumpSlow",  +1, "Tosa heavy braking — control front dive."),
        ],
    },
    "silverstone": {
        "label": "Medium-high downforce — fast sweepers, smooth surface",
        "adjustments": [
            ("rearWing",   +1, "Maggotts/Becketts/Stowe load up the rear at speed."),
            ("aRBRear",    +1, "High-speed yaw stability through Copse."),
            ("rideHeight", -1, "Smooth surface — close to the deck for aero."),
        ],
    },
    "brands hatch": {
        "label": "High downforce — undulating GP loop, heavy kerbs",
        "adjustments": [
            ("rearWing",        +2, "Twisty GP loop — downforce wins."),
            ("rearRideHeight",  +1, "Paddock Hill compression."),
            ("bumpStopWindow",  +1, "Druids / Surtees kerbs."),
        ],
    },
    "nurburgring": {
        "label": "Medium-high downforce — twisty Sectors 2/3",
        "adjustments": [
            ("rearWing", +2, "Sectors 2 and 3 are very twisty."),
            ("aRBFront", +1, "Direction changes through Mercedes Arena."),
        ],
    },
    "zandvoort": {
        "label": "High downforce — banked corners, heavy kerbs",
        "adjustments": [
            ("rearWing",        +3, "Banking + tight Hugenholtz/Arie Luyendyk."),
            ("rearRideHeight",  +1, "Banking compression at Arie Luyendyk."),
            ("bumpStopWindow",  +2, "Heavy kerb usage."),
        ],
    },
    "suzuka": {
        "label": "Medium-high downforce — esses + 130R",
        "adjustments": [
            ("rearWing",        +1, "Esses + Spoon need a stable rear."),
            ("aRBFront",        +1, "Stiffer front for the esses."),
            ("frontRideHeight", +1, "130R compression."),
        ],
    },
    "misano": {
        "label": "High downforce — slow corners, big kerbs",
        "adjustments": [
            ("rearWing",       +2, "Mostly slow technical sections."),
            ("aRBFront",       +1, "Tight direction changes through Variante."),
            ("bumpStopWindow", +2, "Heavy kerb usage."),
        ],
    },
    "paul ricard": {
        "label": "Medium downforce — long Mistral straight",
        "adjustments": [
            ("rearWing",   -2, "Mistral straight — drag costs lap time."),
            ("rideHeight", -1, "Smooth surface."),
            ("preload",    -1, "Tight infield section."),
        ],
    },
    "barcelona": {
        "label": "Medium-high downforce — abrasive, fast final corners",
        "adjustments": [
            ("rearWing", +1, "Long high-speed final sector."),
            ("aRBRear",  +1, "Stable rear through T9 Campsa."),
        ],
    },
    "red bull ring": {
        "label": "Medium downforce — short, twisty, uphill",
        "adjustments": [
            ("rearWing", +1, "Short lap, twisty sectors."),
            ("aRBRear",  +1, "Uphill traction out of T3."),
            ("preload",  -1, "T3 + T9 are very tight."),
        ],
    },
    "bathurst": {
        "label": "Medium downforce — Mountain section is very bumpy",
        "adjustments": [
            ("rideHeight",     +2, "Very bumpy mountain section."),
            ("bumpStopWindow", +2, "The Cutting + Skyline kerbs."),
            ("aRBRear",        +1, "High-speed Conrod stability."),
        ],
    },
    "cota": {
        "label": "Medium-high downforce — technical esses",
        "adjustments": [
            ("rearWing", +2, "Slow infield needs downforce."),
            ("aRBFront", +1, "T2-T6 esses sequence."),
        ],
    },
    "donington": {
        "label": "Medium-high downforce — technical with fast bits",
        "adjustments": [
            ("rearWing", +1, "Old Hairpin and Coppice need downforce."),
            ("aRBRear",  +1, "Craner Curves stability."),
        ],
    },
    "indianapolis": {
        "label": "Medium downforce — road course inside the oval",
        "adjustments": [
            ("aRBRear", +1, "Long oval section needs stable rear."),
        ],
    },
    "kyalami": {
        "label": "Medium downforce — fast, undulating",
        "adjustments": [
            ("rearWing",   +1, "Crowthorne and Mineshaft fast corners."),
            ("rideHeight", +1, "Some elevation/compression."),
        ],
    },
    "laguna seca": {
        "label": "Medium-high downforce — Corkscrew compression",
        "adjustments": [
            ("rearWing",       +2, "Tight infield corners."),
            ("bumpStopWindow", +2, "Corkscrew compression hits suspension hard."),
        ],
    },
    "oulton park": {
        "label": "High downforce — undulating, kerb-heavy",
        "adjustments": [
            ("rearWing",       +2, "Tight Druids/Lodge layout."),
            ("bumpStopWindow", +1, "Cascades + Knickerbrook elevation."),
        ],
    },
    "snetterton": {
        "label": "Medium downforce — fast, smooth",
        "adjustments": [
            ("rideHeight", -1, "Smooth surface."),
            ("aRBRear",    +1, "High-speed Bomb Hole stability."),
        ],
    },
    "watkins glen": {
        "label": "Medium downforce — fast Boot section + chicane",
        "adjustments": [
            ("rearWing", +1, "Boot section + final chicane."),
            ("aRBRear",  +1, "Esses stability."),
        ],
    },
    "valencia": {
        "label": "High downforce — twisty, slow infield (Ricardo Tormo)",
        "adjustments": [
            ("rearWing", +3, "Very twisty layout."),
            ("aRBFront", +1, "Lots of direction changes."),
            ("preload",  -1, "Tight slow corners."),
        ],
    },
    "zolder": {
        "label": "Medium-high downforce — narrow, technical",
        "adjustments": [
            ("rearWing", +2, "Tight Sterrewachtbocht and Lucienbocht."),
            ("aRBFront", +1, "Quick chicanes."),
        ],
    },
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _menu(title: str, options: list[str]) -> int:
    print(f"\n=== {title} ===")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        raw = input("Select: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print("Invalid selection.")


def _prompt_path(label: str, default: str | None = None,
                 allow_skip: bool = False) -> str | None:
    """Prompt for a file path. Press Enter to accept the bracketed default.
    If allow_skip is True, typing 'skip' (or Enter with no default) returns None.
    """
    hint = ""
    if default:
        hint = f"  [press Enter for default: {default}]"
    elif allow_skip:
        hint = "  [type 'skip' or press Enter to skip]"
    while True:
        raw = input(f"{label}{hint}\n  > ").strip()
        if not raw:
            if default and os.path.isfile(default):
                return default
            if allow_skip:
                return None
            print("  (No default available — type a path, or 'skip' if allowed.)")
            continue
        if raw.lower() == "skip" and allow_skip:
            return None
        if os.path.isfile(raw):
            return raw
        print(f"  File not found: {raw!r}. Try again.")


def run_cli() -> None:
    print("=" * 60)
    print(" ACC Setup Optimizer — Driver61 + MoTeC validation")
    print("=" * 60)

    here = os.path.dirname(os.path.abspath(__file__))
    default_setup = os.path.join(here, "FRI3_992_BASE_v1.10.json")
    default_csv = None
    for f in os.listdir(here):
        if f.lower().endswith(".csv"):
            default_csv = os.path.join(here, f)
            break

    setup_path = _prompt_path(
        "Path to ACC setup JSON:",
        default_setup if os.path.isfile(default_setup) else None,
    )
    csv_path = _prompt_path(
        "Path to MoTeC CSV (optional — skip to apply Driver61 fixes without telemetry validation):",
        default_csv,
        allow_skip=True,
    )

    print("\nLoading setup…")
    mgr = SetupManager(setup_path)
    car = mgr.setup.get("carName", "unknown")
    print(f"  Setup car: {car}")
    if car != "porsche_992_gt3_r":
        print(f"  ⚠  This optimizer's defaults are tuned for the Porsche 992 GT3 R; "
              f"loaded car is '{car}'. Adjustments will still apply, but per-car "
              f"rates may differ.")

    tel: TelemetryAnalyzer | None = None
    if csv_path:
        print("Loading telemetry…")
        tel = TelemetryAnalyzer(csv_path)
        print(f"  Telemetry rows: {len(tel.df)}, channels: {list(tel.df.columns)}")
    else:
        print("  Skipping telemetry — fixes will be applied without data validation.")

    # ---- temperature compensation (base setup is calibrated for 20°C) ----
    print(f"\n— Temperature compensation —")
    print(f"  Base setup is calibrated for {SetupManager.BASELINE_AMBIENT_C:.0f}°C ambient.")
    raw = input("  Current/target ambient temperature in °C [Enter to skip]: ").strip()
    if raw:
        try:
            mgr.changes.append("\n[PRE-RUN ADJUSTMENTS]")
            mgr.adjust_for_temperature(float(raw))
            print("  " + mgr.changes[-1].splitlines()[0].strip())
        except ValueError:
            print(f"  Could not parse {raw!r} as a number — skipping.")

    while True:
        # 1) track + corner
        track_keys = list(TRACK_MAP.keys())
        ti = _menu("Track", track_keys + ["Done — save & quit"])
        if ti == len(track_keys):
            break
        track = track_keys[ti]
        corners = list(TRACK_MAP[track].keys())
        ci = _menu(f"Corner @ {track}", corners)
        corner_name = corners[ci]
        d_start, d_end = TRACK_MAP[track][corner_name]
        print(f"  Distance window: {d_start}m – {d_end}m")

        corner_df = None
        phases = None
        if tel is not None:
            corner_df = tel.slice_corner(d_start, d_end)
            if corner_df.empty:
                print("  No telemetry samples in that distance window. Skipping.")
                continue
            phases = tel.split_phases(corner_df)
            print("  Phase sample counts: "
                  + ", ".join(f"{p}={len(s.df)}" for p, s in phases.items()))
        else:
            print("  (No telemetry loaded — skipping data validation.)")

        # 2) issue
        issues = ["Understeer", "Oversteer", "Unstable", "Bottoming"]
        ii = _menu("Driver-reported issue", issues)
        issue = issues[ii]

        # 3) phase (only relevant for U/O)
        if issue in ("Understeer", "Oversteer"):
            pi = _menu("Where does it happen?", ["Entry", "Mid", "Exit"])
            phase = ("Entry", "Mid", "Exit")[pi]
        else:
            phase = "*"

        # 4) data validation (only if telemetry is loaded)
        if tel is not None:
            print("\n— Telemetry validation —")
            if issue == "Understeer":
                ok, msg = tel.validate_understeer(phases[phase].df)
            elif issue == "Oversteer":
                ok, msg = tel.validate_oversteer(phases[phase].df)
            elif issue == "Unstable":
                ok, msg = tel.validate_instability(corner_df)
            else:  # Bottoming
                ok, msg = tel.validate_bottoming(corner_df)
            print("  " + msg)

            if not ok:
                cont = input("Telemetry does NOT confirm the issue. Apply fix anyway? [y/N] ").strip().lower()
                if cont != "y":
                    print("  Skipped.")
                    continue

        # 5) recommendation menu
        recs = RECOMMENDATIONS.get((issue, phase)) or RECOMMENDATIONS.get((issue, "*"), [])
        if not recs:
            print("  No Driver61 recommendation registered for this combination.")
            continue
        labels = [r[0] for r in recs] + ["Apply ALL", "Cancel"]
        ri = _menu(f"{issue} @ {phase} — Driver61 fixes", labels)
        if labels[ri] == "Cancel":
            continue

        header = f"\n{track.upper()} — {corner_name} — {issue} @ {phase}"
        mgr.changes.append(header)
        if labels[ri] == "Apply ALL":
            for _, method_name in recs:
                getattr(mgr, method_name)()
        else:
            method_name = recs[ri][1]
            getattr(mgr, method_name)()
        print("  Adjustment queued.")

    # save
    if not mgr.changes:
        print("\nNo changes made. Exiting.")
        return

    out_path = os.path.join(os.path.dirname(setup_path), "modified_setup.json")
    mgr.save(out_path)
    print("\n" + "=" * 60)
    print(" RACE ENGINEERING REPORT")
    print("=" * 60)
    for line in mgr.changes:
        print(line)
    print(f"\nModified setup written to: {out_path}")


if __name__ == "__main__":
    try:
        run_cli()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(0)
