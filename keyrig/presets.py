"""Named presets and CC mappings, stored together in presets.toml."""

from __future__ import annotations

import copy
import logging
import os
import shutil
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from .layers import NUM_LAYERS, OCTAVE_MAX, OCTAVE_MIN, DrumLayer, Instrument, Layer

log = logging.getLogger("keyrig.presets")

T_LAYER_VOLUME = "layer_volume"
T_MASTER_VOLUME = "master_volume"
T_REVERB_LEVEL = "reverb_level"
T_FILTER_CUTOFF = "filter_cutoff"
T_OCTAVE_SHIFT = "octave_shift"
T_ROTARY_SPEED = "rotary_speed"
GLOBAL_TARGETS = {T_MASTER_VOLUME, T_REVERB_LEVEL, T_ROTARY_SPEED}
LAYER_TARGETS = {T_LAYER_VOLUME, T_FILTER_CUTOFF, T_OCTAVE_SHIFT}

TARGET_LABELS = {
    T_LAYER_VOLUME: "Layer volume",
    T_MASTER_VOLUME: "Master volume",
    T_REVERB_LEVEL: "Reverb level",
    T_FILTER_CUTOFF: "Filter cutoff",
    T_OCTAVE_SHIFT: "Octave shift",
    T_ROTARY_SPEED: "Rotary speed (slow/fast)",
}


@dataclass
class Fx:
    reverb_on: bool = True
    reverb_room: float = 0.4
    reverb_level: float = 0.4
    chorus_on: bool = False


@dataclass
class CCMapping:
    channel: int  # 0-based
    cc: int
    target: str
    layer: int = 0  # 1-4; 0 means all layers. Ignored for global targets.

    @property
    def source_label(self) -> str:
        return f"CC{self.cc} ch{self.channel + 1}"

    @property
    def target_label(self) -> str:
        return target_label(self.target, self.layer)


def target_label(target: str, layer: int) -> str:
    label = TARGET_LABELS[target]
    if target in GLOBAL_TARGETS:
        return label
    return f"{label}: {'all layers' if layer == 0 else f'layer {layer}'}"


@dataclass
class Preset:
    name: str
    layers: list[Layer] = field(default_factory=lambda: [Layer() for _ in range(NUM_LAYERS)])
    drum: DrumLayer = field(default_factory=DrumLayer)
    fx: Fx = field(default_factory=Fx)


def _clamp(value, low, high, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return type(default)(max(low, min(high, value)))


def _layer_from(raw: dict) -> Layer:
    low = _clamp(raw.get("low"), 0, 127, 0)
    high = _clamp(raw.get("high"), 0, 127, 127)
    return Layer(
        enabled=raw.get("enabled") is True,
        instrument=Instrument(
            soundfont=str(raw.get("soundfont", "")),
            bank=_clamp(raw.get("bank"), 0, 128, 0),
            program=_clamp(raw.get("program"), 0, 127, 0),
            name=str(raw.get("instrument", "")),
        ),
        volume=_clamp(raw.get("volume"), 0, 127, 100),
        octave=_clamp(raw.get("octave"), OCTAVE_MIN, OCTAVE_MAX, 0),
        low=min(low, high),
        high=max(low, high),
        cutoff=_clamp(raw.get("cutoff"), 0, 127, 127),
        attack=_clamp(raw.get("attack"), 0, 20000, 0),
        release=_clamp(raw.get("release"), 0, 20000, 0),
        pedal=raw.get("pedal", True) is True,
        rotary=raw.get("rotary", False) is True,
    )


def _layer_to(layer: Layer) -> dict:
    return {
        "enabled": layer.enabled,
        "instrument": layer.instrument.name,
        "soundfont": layer.instrument.soundfont,
        "bank": layer.instrument.bank,
        "program": layer.instrument.program,
        "volume": layer.volume,
        "octave": layer.octave,
        "low": layer.low,
        "high": layer.high,
        "cutoff": layer.cutoff,
        "attack": layer.attack,
        "release": layer.release,
        "pedal": layer.pedal,
        "rotary": layer.rotary,
    }


def _preset_from(raw: dict) -> Preset:
    layers = [_layer_from(item) for item in raw.get("layer", []) if isinstance(item, dict)][:NUM_LAYERS]
    layers += [Layer() for _ in range(NUM_LAYERS - len(layers))]
    drum_raw = raw.get("drum", {}) if isinstance(raw.get("drum"), dict) else {}
    fx_raw = raw.get("fx", {}) if isinstance(raw.get("fx"), dict) else {}
    defaults = Fx()
    return Preset(
        name=str(raw["name"]),
        layers=layers,
        drum=DrumLayer(
            enabled=drum_raw.get("enabled", True) is True,
            soundfont=str(drum_raw.get("soundfont", "")),
            kit=_clamp(drum_raw.get("kit"), 0, 127, 0),
            name=str(drum_raw.get("name", "")),
            volume=_clamp(drum_raw.get("volume"), 0, 127, 110),
        ),
        fx=Fx(
            reverb_on=fx_raw.get("reverb_on", defaults.reverb_on) is True,
            reverb_room=_clamp(fx_raw.get("reverb_room"), 0.0, 1.0, defaults.reverb_room),
            reverb_level=_clamp(fx_raw.get("reverb_level"), 0.0, 1.0, defaults.reverb_level),
            chorus_on=fx_raw.get("chorus_on", defaults.chorus_on) is True,
        ),
    )


def _preset_to(preset: Preset) -> dict:
    return {
        "name": preset.name,
        "fx": {
            "reverb_on": preset.fx.reverb_on,
            "reverb_room": round(preset.fx.reverb_room, 3),
            "reverb_level": round(preset.fx.reverb_level, 3),
            "chorus_on": preset.fx.chorus_on,
        },
        "drum": {
            "enabled": preset.drum.enabled,
            "soundfont": preset.drum.soundfont,
            "kit": preset.drum.kit,
            "name": preset.drum.name,
            "volume": preset.drum.volume,
        },
        "layer": [_layer_to(layer) for layer in preset.layers],
    }


def _mapping_from(raw: dict) -> CCMapping | None:
    target = raw.get("target")
    if target not in TARGET_LABELS:
        return None
    channel, cc = raw.get("channel"), raw.get("cc")
    if not isinstance(channel, int) or not isinstance(cc, int):
        return None
    if not (1 <= channel <= 16 and 0 <= cc <= 127):
        return None
    return CCMapping(channel - 1, cc, target, _clamp(raw.get("layer"), 0, NUM_LAYERS, 0))


GRAND = Instrument(program=0, name="Acoustic Grand Piano")
ORGAN = Instrument(program=19, name="Church Organ")
EPIANO = Instrument(program=4, name="Electric Piano 1")


def _with_layers(name: str, fx: Fx, *layers: Layer) -> Preset:
    padded = list(layers) + [Layer() for _ in range(NUM_LAYERS - len(layers))]
    return Preset(name=name, layers=padded, fx=fx)


def starter_presets() -> list[Preset]:
    return [
        _with_layers(
            "Church Organ + Grand",
            Fx(reverb_on=True, reverb_room=0.85, reverb_level=0.7),
            Layer(enabled=True, instrument=copy.copy(ORGAN), volume=110),
            Layer(enabled=True, instrument=copy.copy(GRAND), volume=68),
        ),
        _with_layers(
            "Piano Only",
            Fx(reverb_on=True, reverb_room=0.4, reverb_level=0.3),
            Layer(enabled=True, instrument=copy.copy(GRAND), volume=115),
        ),
        _with_layers(
            "Split: Organ low / Piano high",
            Fx(reverb_on=True, reverb_room=0.6, reverb_level=0.45),
            Layer(enabled=True, instrument=copy.copy(ORGAN), volume=100, low=0, high=59),
            Layer(enabled=True, instrument=copy.copy(GRAND), volume=115, low=60, high=127),
        ),
        _with_layers(
            "Electric Piano",
            Fx(reverb_on=True, reverb_room=0.35, reverb_level=0.3, chorus_on=True),
            Layer(enabled=True, instrument=copy.copy(EPIANO), volume=112),
        ),
    ]


class PresetStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.presets: list[Preset] = []
        self.mappings: list[CCMapping] = []
        self.warning = ""

    def load(self) -> None:
        if not self.path.exists():
            self.presets = starter_presets()
            self.save()
            return
        try:
            with self.path.open("rb") as handle:
                raw = tomllib.load(handle)
            presets = [_preset_from(item) for item in raw.get("preset", [])]
        except (tomllib.TOMLDecodeError, OSError, KeyError, TypeError) as exc:
            # Keep the user's broken file, since the next save would replace it.
            backup = self.path.with_name(f"{self.path.name}.broken-{int(time.time())}")
            shutil.copy2(self.path, backup)
            self.warning = f"{self.path.name} was unreadable ({exc}); copy kept as {backup.name}"
            log.warning(self.warning)
            self.presets = starter_presets()
            self.mappings = []
            return
        self.presets = presets or starter_presets()
        self.mappings = [m for m in map(_mapping_from, raw.get("mapping", [])) if m is not None]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "mapping": [
                {"channel": m.channel + 1, "cc": m.cc, "target": m.target, "layer": m.layer}
                for m in self.mappings
            ],
            "preset": [_preset_to(p) for p in self.presets],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(tomli_w.dumps(data))
        os.replace(tmp, self.path)

    def index_of(self, name: str) -> int | None:
        return next((i for i, p in enumerate(self.presets) if p.name == name), None)

    def store(self, preset: Preset) -> int:
        """Add a preset or overwrite the one with the same name; returns its index."""
        index = self.index_of(preset.name)
        if index is None:
            self.presets.append(preset)
            index = len(self.presets) - 1
        else:
            self.presets[index] = preset
        self.save()
        return index

    def set_mapping(self, mapping: CCMapping) -> None:
        self.mappings = [
            m for m in self.mappings if (m.channel, m.cc) != (mapping.channel, mapping.cc)
        ] + [mapping]
        self.save()

    def remove_mapping(self, channel: int, cc: int) -> None:
        self.mappings = [m for m in self.mappings if (m.channel, m.cc) != (channel, cc)]
        self.save()
