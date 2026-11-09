"""Live levels and spectrum of keyrig's own audio, plus output health checks.

Audio is read through a PipeWire tap created with nothing connected, then wired
only to keyrig's output ports. It must never autoconnect: PipeWire would attach
it to the default source, which is a microphone or instrument input.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from .synth_engine import _run

log = logging.getLogger("keyrig.meter")

RATE = 48000
FRAMES = 2048
BYTES_PER_FRAME = 8  # stereo, 32-bit float
LINK_TIMEOUT_SECONDS = 3.0
NO_DATA_TIMEOUT_SECONDS = 2.0
OUTPUT_CHECK_SECONDS = 2.0

FFT_SIZE = 8192  # 5.9 Hz per bin: separates the overtones of notes down to about F#1
SPECTRUM_LOW_HZ = 27.5  # A0, the lowest piano note
SPECTRUM_HIGH_HZ = 20000.0
SPECTRUM_FLOOR_DB = -80.0
DOMINANT_MIN_DB = -60.0

PEAK_FALL_DB_PER_SECOND = 40.0
HOLD_SECONDS = 1.5
HOLD_FALL_DB_PER_SECOND = 20.0
BAND_FALL_PER_SECOND = 0.9
CAP_HOLD_SECONDS = 0.5
CAP_FALL_PER_SECOND = 0.5
CLIP_SECONDS = 2.0
CLIP_LEVEL = 0.999

STATE_LIVE = "live"
STATE_WAITING = "connecting"
STATE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class MeterFrame:
    state: str = STATE_WAITING
    peak: tuple[float, float] = (0.0, 0.0)
    hold: tuple[float, float] = (0.0, 0.0)
    bands: tuple[float, ...] = ()
    clipped: bool = False
    caps: tuple[float, ...] = ()  # falling peak marker for each band
    dominant_hz: float | None = None  # the loudest partial right now


@dataclass(frozen=True)
class OutputStatus:
    connected: bool
    volume: float | None = None
    muted: bool = False


def tap_command(name: str) -> list[str]:
    """pw-record, unlinked (target 0 and no autoconnect), writing raw audio to stdout."""
    return [
        "pw-record", "--target", "0",
        "-P", f"{{ node.name={name} node.description=keyrig-meter node.autoconnect=false }}",
        "--rate", str(RATE), "--channels", "2", "--format", "f32",
        "--raw", "--latency", "20ms", "-",
    ]


def tap_environment() -> dict[str, str]:
    """keyrig's routing variables would override the tap's own name and target if inherited."""
    return {k: v for k, v in os.environ.items() if k not in ("PIPEWIRE_NODE", "PIPEWIRE_PROPS")}


def _blackman_harris(size: int) -> np.ndarray:
    """A window with very low leakage, so a loud partial cannot hide a quiet overtone."""
    phase = 2 * np.pi * np.arange(size) / size
    return 0.35875 - 0.48829 * np.cos(phase) + 0.14128 * np.cos(2 * phase) - 0.01168 * np.cos(3 * phase)


_WINDOW = _blackman_harris(FFT_SIZE)
_FULL_SCALE = _WINDOW.sum() / 2  # the magnitude of a full-scale sine through the window
_HZ_PER_BIN = RATE / FFT_SIZE


def column_for_frequency(hz: float, count: int) -> int:
    """The display column for a frequency; may fall outside 0..count-1."""
    return int(np.floor(count * np.log(hz / SPECTRUM_LOW_HZ) / np.log(SPECTRUM_HIGH_HZ / SPECTRUM_LOW_HZ)))


def column_frequency(column: int, count: int) -> float:
    """The centre frequency of a display column."""
    ratio = SPECTRUM_HIGH_HZ / SPECTRUM_LOW_HZ
    return float(SPECTRUM_LOW_HZ * ratio ** ((column + 0.5) / count))


@lru_cache(maxsize=8)
def _column_bins(count: int) -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    """Fractional FFT bin of each column's centre, and the columns wide enough to span bins."""
    edges = np.geomspace(SPECTRUM_LOW_HZ, SPECTRUM_HIGH_HZ, count + 1) / _HZ_PER_BIN
    centres = np.sqrt(edges[:-1] * edges[1:])
    wide = []
    for column, (low, high) in enumerate(zip(edges[:-1], edges[1:])):
        first, last = int(np.ceil(low)), int(np.floor(high))
        if last >= first:
            wide.append((column, first, last + 1))
    return centres, wide


def _dominant_frequency(magnitudes: np.ndarray) -> float | None:
    first = int(np.ceil(SPECTRUM_LOW_HZ / _HZ_PER_BIN))
    last = min(len(magnitudes) - 2, int(SPECTRUM_HIGH_HZ / _HZ_PER_BIN))
    peak = first + int(np.argmax(magnitudes[first:last]))
    if magnitudes[peak] < 10 ** (DOMINANT_MIN_DB / 20):
        return None
    # A parabola through the log magnitudes places the peak between bins.
    a, b, c = np.log(magnitudes[peak - 1:peak + 2] + 1e-12)
    curvature = a - 2 * b + c
    offset = 0.5 * (a - c) / curvature if curvature else 0.0
    return float((peak + offset) * _HZ_PER_BIN)


def analyse(
    block: np.ndarray, history: np.ndarray, count: int
) -> tuple[tuple[float, float], list[float], float | None]:
    """Peaks of the newest stereo block, plus a flat-response spectrum of the recent audio.

    Returns ((left, right), one 0-1 level per display column, loudest frequency).
    """
    peaks = (float(np.abs(block[:, 0]).max()), float(np.abs(block[:, 1]).max()))
    magnitudes = np.abs(np.fft.rfft(history.mean(axis=1) * _WINDOW)) / _FULL_SCALE
    centres, wide = _column_bins(count)
    levels = np.interp(centres, np.arange(len(magnitudes)), magnitudes)
    for column, first, stop in wide:
        levels[column] = max(levels[column], magnitudes[first:stop].max())
    bands = np.clip(1 - 20 * np.log10(levels + 1e-12) / SPECTRUM_FLOOR_DB, 0.0, 1.0)
    return peaks, bands.tolist(), _dominant_frequency(magnitudes)


@dataclass
class Ballistics:
    """Makes raw measurements readable: instant attack, smooth fall, held peaks."""

    peak: list[float] = field(default_factory=lambda: [0.0, 0.0])
    hold: list[float] = field(default_factory=lambda: [0.0, 0.0])
    hold_time: list[float] = field(default_factory=lambda: [0.0, 0.0])
    bands: list[float] = field(default_factory=list)
    caps: list[float] = field(default_factory=list)
    cap_times: list[float] = field(default_factory=list)
    clip_time: float = -1e9
    last_time: float | None = None

    def update(
        self, peaks: tuple[float, float], bands: list[float], now: float, dominant_hz: float | None = None
    ) -> MeterFrame:
        elapsed = 0.0 if self.last_time is None else max(0.0, now - self.last_time)
        self.last_time = now
        for side in (0, 1):
            fallen = self.peak[side] * 10 ** (-PEAK_FALL_DB_PER_SECOND * elapsed / 20)
            self.peak[side] = max(peaks[side], fallen)
            if peaks[side] >= self.hold[side]:
                self.hold[side] = peaks[side]
                self.hold_time[side] = now
            elif now - self.hold_time[side] > HOLD_SECONDS:
                self.hold[side] *= 10 ** (-HOLD_FALL_DB_PER_SECOND * elapsed / 20)
        if max(peaks) >= CLIP_LEVEL:
            self.clip_time = now
        if len(self.bands) != len(bands):
            self.bands = [0.0] * len(bands)
            self.caps = [0.0] * len(bands)
            self.cap_times = [now] * len(bands)
        self.bands = [max(new, old - BAND_FALL_PER_SECOND * elapsed) for new, old in zip(bands, self.bands)]
        for index, level in enumerate(self.bands):
            if level >= self.caps[index]:
                self.caps[index] = level
                self.cap_times[index] = now
            elif now - self.cap_times[index] > CAP_HOLD_SECONDS:
                self.caps[index] = max(level, self.caps[index] - CAP_FALL_PER_SECOND * elapsed)
        return MeterFrame(
            STATE_LIVE, (self.peak[0], self.peak[1]), (self.hold[0], self.hold[1]),
            tuple(self.bands), now - self.clip_time < CLIP_SECONDS, tuple(self.caps), dominant_hz,
        )


def parse_output_status(objects: list[dict], node_name: str) -> tuple[bool, int | None]:
    """From pw-dump output: is keyrig linked to a sink, and which sink node?"""
    nodes = {}
    keyrig_id = None
    for obj in objects:
        info = obj.get("info")
        if isinstance(info, dict) and obj.get("type", "").endswith(":Node"):
            nodes[obj["id"]] = info.get("props", {})
            if info.get("props", {}).get("node.name") == node_name:
                keyrig_id = obj["id"]
    if keyrig_id is None:
        return False, None
    for obj in objects:
        info = obj.get("info")
        if isinstance(info, dict) and obj.get("type", "").endswith(":Link"):
            target = info.get("input-node-id")
            if info.get("output-node-id") == keyrig_id and nodes.get(target, {}).get("media.class") == "Audio/Sink":
                return True, target
    return False, None


def parse_volume(text: str) -> tuple[float | None, bool]:
    match = re.search(r"Volume:\s*([\d.]+)", text)
    return (float(match.group(1)) if match else None), "MUTED" in text


def probe_output(node_name: str) -> OutputStatus | None:
    output = _run(["pw-dump"])
    if output is None:
        return None
    try:
        objects = json.loads(output)
    except json.JSONDecodeError:
        return None
    connected, sink = parse_output_status(objects, node_name)
    if not connected or sink is None:
        return OutputStatus(False)
    volume, muted = parse_volume(_run(["wpctl", "get-volume", str(sink)]) or "")
    return OutputStatus(True, volume, muted)


class AudioMeter:
    """Background tap and analysis. Read `frame` and `output` from any thread."""

    def __init__(self, node_name: str, generation: Callable[[], int | None]) -> None:
        self._node = node_name
        self._tap = f"{node_name}-meter"
        self._generation = generation  # changes when the audio stream is recreated; None if not PipeWire
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.band_count = 32
        self.frame = MeterFrame()
        self.output: OutputStatus | None = None

    def start(self) -> None:
        for target, name in ((self._run_tap, "keyrig-meter"), (self._run_checks, "keyrig-output-check")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)

    def _run_checks(self) -> None:
        while not self._stop.is_set():
            self.output = probe_output(self._node) if self._generation() is not None else None
            self._stop.wait(OUTPUT_CHECK_SECONDS)

    def _run_tap(self) -> None:
        while not self._stop.is_set():
            generation = self._generation()
            if generation is None or shutil.which("pw-record") is None or shutil.which("pw-link") is None:
                self.frame = MeterFrame(STATE_UNAVAILABLE)
            else:
                self.frame = MeterFrame(STATE_WAITING)
                try:
                    self._session(generation)
                except Exception:
                    log.exception("Level meter failed")
                self.frame = MeterFrame(STATE_WAITING)
            self._stop.wait(1.0)

    def _session(self, generation: int) -> None:
        process = subprocess.Popen(
            tap_command(self._tap),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=tap_environment(),
        )
        try:
            if self._link():
                self._read(process, generation)
        finally:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()

    def _link(self) -> bool:
        """Wire the tap to keyrig's outputs, waiting for both sets of ports to appear."""
        pending = ["FL", "FR"]
        deadline = time.monotonic() + LINK_TIMEOUT_SECONDS
        while pending and time.monotonic() < deadline and not self._stop.is_set():
            for channel in list(pending):
                result = subprocess.run(
                    ["pw-link", f"{self._node}:output_{channel}", f"{self._tap}:input_{channel}"],
                    capture_output=True, timeout=2,
                )
                if result.returncode == 0:
                    pending.remove(channel)
            if pending:
                self._stop.wait(0.1)
        return not pending

    def _read(self, process: subprocess.Popen, generation: int) -> None:
        fd = process.stdout.fileno()
        chunk = FRAMES * BYTES_PER_FRAME
        pending = bytearray()
        ballistics = Ballistics()
        last_data = time.monotonic()
        history = np.zeros((FFT_SIZE, 2), dtype=np.float32)
        while not self._stop.is_set() and self._generation() == generation:
            ready, _, _ = select.select([fd], [], [], 0.25)
            now = time.monotonic()
            if not ready:
                if now - last_data > NO_DATA_TIMEOUT_SECONDS:
                    return
                continue
            data = os.read(fd, 65536)
            if not data:
                return
            last_data = now
            pending.extend(data)
            whole = len(pending) // chunk
            if whole == 0:
                continue
            block = bytes(pending[(whole - 1) * chunk:whole * chunk])  # only the newest block matters
            del pending[:whole * chunk]
            samples = np.frombuffer(block, dtype=np.float32).reshape(-1, 2)
            history = np.concatenate((history[FRAMES:], samples))
            peaks, bands, dominant = analyse(samples, history, self.band_count)
            self.frame = ballistics.update(peaks, bands, now, dominant)
