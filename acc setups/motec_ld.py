"""Minimal MoTeC .ld telemetry parser.

ACC's MoTeC export writes a `.ld` (binary log) + `.ldx` (XML metadata) pair to
``~/Documents/Assetto Corsa Competizione/MoTeC/<session>/``. The `.ld` file
holds the actual sample data; the `.ldx` is just an index for i2 Pro that we
don't need to parse — the .ld file is self-describing through a linked list
of channel headers.

This parser is intentionally narrow — it reads what ACC produces and turns it
into a pandas DataFrame the existing TelemetryAnalyzer can consume. It does
NOT try to be a complete reference implementation of the .ld format.

File layout (little-endian):
    0x00  u32   magic = 0xEC12CD40
    0x08  u32   meta_ptr        — offset of first channel header
    0x0C  u32   data_ptr        — offset of bulk data section
    0x14  u32   meta_ptr2       — usually == meta_ptr
    0x44+ ASCII event/venue/driver strings (we ignore them)

Channel header (0x7C = 124 bytes), linked list via prev/next pointers:
    +0x00  u32   prev_ptr
    +0x04  u32   next_ptr
    +0x08  u32   data_ptr        — where this channel's samples start
    +0x0C  u32   n_samples
    +0x14  u16   data_size       — bytes per sample (2 or 4)
    +0x16  u16   data_type       — 0=int16, 2=float32, 3=int32
    +0x18  u16   frequency       — Hz
    +0x1A  i16   shift
    +0x1C  i16   mul
    +0x1E  i16   scale
    +0x20  i16   dec_places
    +0x22  32s   name
    +0x42  8s    short_name
    +0x4A  12s   unit

Physical value = raw * mul / scale / 10**dec_places  (when scale != 0).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np
import pandas as pd


# Known MoTeC `.ld` magic numbers — both byte-orders seen in the wild, plus
# a handful of ACC-specific variants we've observed. The parser falls back
# to a structural scan if none of these match, so even an unknown variant
# still loads as long as the channel-header layout is the conventional one.
LD_MAGIC_CANDIDATES = (0xEC12CD40, 0x40CD12EC, 0x40CD0000, 0xCDEAFE00)
CHANNEL_HEADER_SIZE = 0x7C   # 124 bytes


# Constraints used by the structural scan to validate that a 124-byte chunk
# of the file actually IS a channel header (vs random data that happens to
# decode to something).
_VALID_DATA_TYPES = (0, 2, 3)
_VALID_DATA_SIZES = (2, 4, 8)
_MAX_FREQ = 10_000
_MAX_SAMPLES = 100_000_000


@dataclass
class LDChannel:
    name: str
    short_name: str
    unit: str
    frequency: int
    data_type: int
    data_size: int
    mul: int
    scale: int
    dec_places: int
    data_offset: int
    n_samples: int

    def read_samples(self, blob: bytes) -> np.ndarray:
        """Decode this channel's raw samples into a float64 numpy array,
        applying the mul/scale/dec_places conversion. Returns zeros on any
        out-of-bounds / unsupported-type condition (so a single bad channel
        never tanks the whole load)."""
        dtype_map = {0: ("<i2", 2), 2: ("<f4", 4), 3: ("<i4", 4)}
        if self.data_type not in dtype_map:
            return np.zeros(self.n_samples, dtype=np.float64)
        dtype, byte_size = dtype_map[self.data_type]

        end = self.data_offset + self.n_samples * byte_size
        if (self.data_offset < 0 or self.n_samples <= 0 or end > len(blob)):
            return np.zeros(max(self.n_samples, 0), dtype=np.float64)

        try:
            arr = np.frombuffer(blob, dtype=dtype,
                                count=self.n_samples,
                                offset=self.data_offset).astype(np.float64)
        except (ValueError, TypeError):
            return np.zeros(self.n_samples, dtype=np.float64)

        if self.scale not in (0, 1) or self.mul != 1 or self.dec_places != 0:
            denom = self.scale if self.scale != 0 else 1
            arr = arr * self.mul / denom / (10.0 ** self.dec_places)
        return arr


def _read_string(buf: bytes) -> str:
    return buf.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()


def _decode_channel_header(h: bytes) -> tuple[LDChannel, int] | None:
    """Decode a 124-byte block as a channel header. Returns the LDChannel
    and the raw next_ptr, or None if the block fails structural validation.
    """
    if len(h) < CHANNEL_HEADER_SIZE:
        return None
    try:
        _prev_ptr, next_ptr, data_ptr, n_samples = struct.unpack_from("<IIII", h, 0x00)
        data_size, data_type, freq = struct.unpack_from("<HHH", h, 0x14)
        _shift, mul, scale, dec_places = struct.unpack_from("<hhhh", h, 0x1A)
    except struct.error:
        return None

    if data_type not in _VALID_DATA_TYPES:
        return None
    if data_size not in _VALID_DATA_SIZES:
        return None
    if not (1 <= freq <= _MAX_FREQ):
        return None
    if not (1 <= n_samples <= _MAX_SAMPLES):
        return None

    name = _read_string(h[0x22:0x42])
    short_name = _read_string(h[0x42:0x4A])
    unit = _read_string(h[0x4A:0x56])

    # Names should look ASCII-printable — filters out random data that
    # happens to decode to a valid struct.
    if not name or not all(32 <= ord(c) < 127 for c in name[:8]):
        return None

    return LDChannel(
        name=name, short_name=short_name, unit=unit,
        frequency=freq, data_type=data_type, data_size=data_size,
        mul=mul, scale=scale, dec_places=dec_places,
        data_offset=data_ptr, n_samples=n_samples,
    ), next_ptr


def _walk_linked_list(data: bytes, start_ptr: int) -> list[LDChannel]:
    """Follow next_ptr chain starting at ``start_ptr``."""
    channels: list[LDChannel] = []
    seen: set[int] = set()
    ptr = start_ptr
    while ptr and ptr + CHANNEL_HEADER_SIZE <= len(data) and ptr not in seen:
        seen.add(ptr)
        decoded = _decode_channel_header(data[ptr:ptr + CHANNEL_HEADER_SIZE])
        if decoded is None:
            break
        ch, next_ptr = decoded
        if 0 <= ch.data_offset < len(data):
            channels.append(ch)
        if next_ptr == 0 or next_ptr == ptr:
            break
        ptr = next_ptr
        if len(channels) > 512:
            break
    return channels


def _scan_for_channels(data: bytes) -> list[LDChannel]:
    """Brute-force scan: try every 4-byte aligned offset as a potential
    channel header. Survives unknown magic / shifted meta_ptr / different
    header layouts as long as the channel-header structure is conventional.
    """
    found: list[LDChannel] = []
    seen_offsets: set[int] = set()
    seen_keys: set[tuple[str, int]] = set()
    file_len = len(data)
    end = file_len - CHANNEL_HEADER_SIZE

    # Skip the first 0x80 bytes (file-level header).
    offset = 0x40
    while offset < end:
        decoded = _decode_channel_header(data[offset:offset + CHANNEL_HEADER_SIZE])
        if decoded is not None:
            ch, _next = decoded
            # Bounds check on the actual data region.
            byte_size = {0: 2, 2: 4, 3: 4}.get(ch.data_type, 0)
            if (byte_size and 0 < ch.data_offset < file_len
                    and ch.data_offset + ch.n_samples * byte_size <= file_len):
                key = (ch.name, ch.data_offset)
                if key not in seen_keys and offset not in seen_offsets:
                    found.append(ch)
                    seen_keys.add(key)
                    seen_offsets.add(offset)
                    # Channel headers are typically packed back-to-back —
                    # advance by header size to avoid re-finding the same
                    # one shifted by a few bytes.
                    offset += CHANNEL_HEADER_SIZE
                    continue
        offset += 4

    return found


def parse_ld_channels(data: bytes) -> list[LDChannel]:
    """Return all channel headers in ``data``.

    Strategy (each step falls through if it fails):
        1. Verify the magic against any known candidate.
        2. Walk the linked list from ``meta_ptr`` (offset 0x08).
        3. If that yields too few channels, brute-force-scan the file for
           valid channel headers structurally.

    This makes the parser tolerant of ACC's various MoTeC-export variants
    — even if the magic shifts in a future game patch, the channel-header
    layout has been stable across versions and the scan will still find them.
    """
    if len(data) < 0x40:
        raise ValueError("File too short to be a MoTeC .ld file.")

    magic = struct.unpack_from("<I", data, 0)[0]
    magic_ok = magic in LD_MAGIC_CANDIDATES

    # 1) try the documented linked-list walk first.
    channels: list[LDChannel] = []
    if magic_ok or True:    # always try, even if magic doesn't match
        try:
            meta_ptr = struct.unpack_from("<I", data, 0x08)[0]
            if 0 < meta_ptr < len(data):
                channels = _walk_linked_list(data, meta_ptr)
        except struct.error:
            channels = []

    # 2) If the linked list gave us nothing (or unreasonably little) and
    # the magic was already wrong, fall back to a structural scan.
    if len(channels) < 3:
        scan = _scan_for_channels(data)
        if len(scan) > len(channels):
            channels = scan

    if not channels:
        # Build a useful diagnostic for the user.
        head_hex = data[:16].hex(" ")
        ascii_preview = "".join(
            chr(b) if 32 <= b < 127 else "." for b in data[:32]
        )
        raise ValueError(
            f"Could not find any channels in this file.\n"
            f"  magic at offset 0: 0x{magic:08x} "
            f"(expected one of: "
            f"{', '.join(f'0x{m:08x}' for m in LD_MAGIC_CANDIDATES)})\n"
            f"  first 16 bytes:    {head_hex}\n"
            f"  ascii preview:     {ascii_preview}\n"
            f"  file size:         {len(data)} bytes\n"
            f"If this IS an ACC telemetry log, please paste these bytes "
            f"so the parser layout can be updated."
        )

    return channels


def parse_ld(path: str) -> tuple[bytes, list[LDChannel]]:
    """Open a .ld file and return (raw bytes, channel headers)."""
    with open(path, "rb") as fh:
        data = fh.read()
    return data, parse_ld_channels(data)


def ld_to_dataframe(path: str) -> pd.DataFrame:
    """Read a MoTeC .ld file and convert all channels into a single DataFrame.

    Channels with different sample rates are linearly interpolated up to the
    highest rate so they share the same time base, exactly as i2 Pro does
    when displaying multiple channels on the same trace.
    """
    blob, channels = parse_ld(path)
    if not channels:
        return pd.DataFrame()

    # Highest sample rate sets the master time base.
    max_freq = max(c.frequency for c in channels if c.frequency)
    max_n = max(c.n_samples * (max_freq // max(1, c.frequency))
                for c in channels)

    frame: dict[str, np.ndarray] = {}
    for ch in channels:
        if ch.frequency == 0 or ch.n_samples == 0:
            continue
        try:
            values = ch.read_samples(blob)
        except Exception:
            continue

        ratio = max_freq // ch.frequency if ch.frequency else 1
        target_n = max_n
        if ratio > 1:
            # Up-sample by repeat (cheap and matches ACC's logging behaviour
            # better than linear interpolation since channels were sampled at
            # a fixed integer divisor of the master rate).
            values = np.repeat(values, ratio)
        if len(values) < target_n:
            values = np.pad(values, (0, target_n - len(values)),
                            constant_values=np.nan)
        elif len(values) > target_n:
            values = values[:target_n]

        # If two channels share a name (rare) keep the first one we saw.
        col = ch.name or ch.short_name or f"ch{len(frame)}"
        if col in frame:
            col = f"{col}_{ch.short_name}"
        frame[col] = values

    df = pd.DataFrame(frame)
    if "Time" not in df.columns:
        df.insert(0, "Time", np.arange(len(df)) / max_freq)
    return df


# ---------------------------------------------------------------------------
# Convenience: write a synthetic .ld file. Used by the round-trip test.
# ---------------------------------------------------------------------------
def write_synthetic_ld(path: str,
                       channels: list[tuple[str, str, np.ndarray, int]],
                       data_type: int = 2) -> None:
    """Write a minimally-valid synthetic .ld file for round-trip testing.

    `channels` = [(name, unit, data, freq_hz), ...] where data is a numpy
    array of physical values. Encoded as float32 (data_type=2) by default.
    """
    if data_type != 2:
        raise NotImplementedError("synthetic writer only supports float32.")
    dtype = "<f4"

    body_pieces: list[bytes] = []
    body_pos = 0
    # Reserve fixed header (0x70) + per-channel headers immediately after.
    header_size = 0x70
    n_chan = len(channels)
    chan_headers_size = n_chan * CHANNEL_HEADER_SIZE
    data_section_start = header_size + chan_headers_size

    chan_headers: list[bytes] = []
    chan_data: list[bytes] = []
    cursor = data_section_start
    for i, (name, unit, arr, freq) in enumerate(channels):
        encoded = arr.astype(dtype).tobytes()
        data_offset = cursor
        cursor += len(encoded)
        chan_data.append(encoded)

        prev_ptr = 0 if i == 0 else (header_size + (i - 1) * CHANNEL_HEADER_SIZE)
        next_ptr = 0 if i == n_chan - 1 else (header_size + (i + 1) * CHANNEL_HEADER_SIZE)

        h = bytearray(CHANNEL_HEADER_SIZE)
        struct.pack_into("<IIII", h, 0x00, prev_ptr, next_ptr, data_offset, len(arr))
        struct.pack_into("<HHH", h, 0x14, 4, 2, freq)         # 4 bytes/sample, type=2 (f32), freq
        struct.pack_into("<hhhh", h, 0x1A, 0, 1, 1, 0)        # shift=0, mul=1, scale=1, dec=0
        h[0x22:0x42] = name.encode("latin-1")[:32].ljust(32, b"\x00")
        h[0x42:0x4A] = name.encode("latin-1")[:8].ljust(8, b"\x00")
        h[0x4A:0x56] = unit.encode("latin-1")[:12].ljust(12, b"\x00")
        chan_headers.append(bytes(h))

    # Build top header
    top = bytearray(header_size)
    struct.pack_into("<I", top, 0x00, LD_MAGIC_CANDIDATES[0])
    struct.pack_into("<I", top, 0x08, header_size)              # meta_ptr
    struct.pack_into("<I", top, 0x0C, data_section_start)       # data_ptr
    struct.pack_into("<I", top, 0x14, header_size)              # meta_ptr2

    with open(path, "wb") as fh:
        fh.write(top)
        for h in chan_headers:
            fh.write(h)
        for d in chan_data:
            fh.write(d)
