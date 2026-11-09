"""User configuration (config.toml) and runtime state (state.toml).

config.toml is hand-edited and keyrig only ever creates it, so comments survive.
state.toml is what the UI changes (chosen ports, master volume, last preset).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

DEFAULT_CONFIG_TEXT = """\
# keyrig configuration. Changes apply the next time keyrig starts.

# Main General MIDI SoundFont. Empty means search the usual system locations.
soundfont = ""

# Additional SoundFonts; their presets appear in the instrument picker.
extra_soundfonts = []

# Audio tuning. See "Latency tuning" in the README.
sample_rate = 48000
period_size = 128     # frames per audio period; lower = less latency, more xrun risk
periods = 2           # ALSA buffer count (PipeWire uses period_size only)
polyphony = 128       # lower on a Raspberry Pi if you hear dropouts
cpu_cores = 1         # synth render threads

# Output level at master volume 127. Lower this if stacked layers clip.
max_gain = 1.0

# MIDI channel (1-16) whose notes go to the drum layer instead of the key layers.
drum_channel = 10

# Note of pad 1 (bottom left) in the 4x4 pad display. 16 consecutive notes are shown.
pad_base_note = 36

# When true, incoming program-change N loads preset N+1.
program_change_presets = false

# When true, notes you are holding keep their old sound through a preset change
# until you let go. When false, a preset change releases them immediately.
smooth_preset_changes = true
"""


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "keyrig"


def _as_int(raw: dict, key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


@dataclass
class Config:
    soundfont: str = ""
    extra_soundfonts: list[str] = field(default_factory=list)
    sample_rate: int = 48000
    period_size: int = 128
    periods: int = 2
    polyphony: int = 128
    cpu_cores: int = 1
    max_gain: float = 1.0
    drum_channel: int = 10
    pad_base_note: int = 36
    program_change_presets: bool = False
    smooth_preset_changes: bool = True

    # state.toml
    midi_port: str = "auto"
    audio_device: str = "auto"
    master_volume: int = 90
    last_preset: str = ""
    transpose: int = 0
    key_curve: str = "Linear"
    pad_curve: str = "Linear"

    config_dir: Path = field(default_factory=default_config_dir)
    warnings: list[str] = field(default_factory=list)

    @property
    def presets_path(self) -> Path:
        return self.config_dir / "presets.toml"

    @property
    def state_path(self) -> Path:
        return self.config_dir / "state.toml"

    @property
    def log_path(self) -> Path:
        return self.config_dir / "keyrig.log"

    def save_state(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "midi_port": self.midi_port,
            "audio_device": self.audio_device,
            "master_volume": self.master_volume,
            "last_preset": self.last_preset,
            "transpose": self.transpose,
            "key_curve": self.key_curve,
            "pad_curve": self.pad_curve,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(tomli_w.dumps(data))
        os.replace(tmp, self.state_path)


def _read_toml(path: Path, warnings: list[str]) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        warnings.append(f"Ignoring unreadable {path.name}: {exc}")
        return {}


def load_config(config_dir: Path | None = None) -> Config:
    directory = config_dir or default_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "config.toml"
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG_TEXT)

    warnings: list[str] = []
    raw = _read_toml(config_path, warnings)
    state = _read_toml(directory / "state.toml", warnings)

    extras = raw.get("extra_soundfonts", [])
    if not isinstance(extras, list):
        extras = []
    soundfont = raw.get("soundfont", "")
    max_gain = raw.get("max_gain", 1.0)
    if isinstance(max_gain, bool) or not isinstance(max_gain, (int, float)):
        max_gain = 1.0

    return Config(
        soundfont=soundfont if isinstance(soundfont, str) else "",
        extra_soundfonts=[str(item) for item in extras],
        sample_rate=_as_int(raw, "sample_rate", 48000, 8000, 192000),
        period_size=_as_int(raw, "period_size", 128, 64, 8192),
        periods=_as_int(raw, "periods", 2, 2, 64),
        polyphony=_as_int(raw, "polyphony", 128, 16, 4096),
        cpu_cores=_as_int(raw, "cpu_cores", 1, 1, 64),
        max_gain=max(0.0, min(10.0, float(max_gain))),
        drum_channel=_as_int(raw, "drum_channel", 10, 1, 16),
        pad_base_note=_as_int(raw, "pad_base_note", 36, 0, 112),
        program_change_presets=raw.get("program_change_presets", False) is True,
        smooth_preset_changes=raw.get("smooth_preset_changes", True) is True,
        transpose=_as_int(state, "transpose", 0, -12, 12),
        key_curve=str(state.get("key_curve", "Linear")),
        pad_curve=str(state.get("pad_curve", "Linear")),
        midi_port=str(state.get("midi_port", "auto")),
        audio_device=str(state.get("audio_device", "auto")),
        master_volume=_as_int(state, "master_volume", 90, 0, 127),
        last_preset=str(state.get("last_preset", "")),
        config_dir=directory,
        warnings=warnings,
    )
