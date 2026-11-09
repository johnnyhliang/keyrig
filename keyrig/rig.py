"""The running instrument: ties MIDI, routing, presets and the synth together.

The TUI and headless mode both drive this class and nothing lower.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass

import mido

from .config import Config
from .gm import note_name
from .layers import (
    CC_MOD,
    CC_SUSTAIN,
    NUM_LAYERS,
    OCTAVE_MAX,
    OCTAVE_MIN,
    ROTARY_FAST_HZ,
    TRANSPOSE_MAX,
    TRANSPOSE_MIN,
    VELOCITY_CURVES,
    DrumLayer,
    Instrument,
    Layer,
    Router,
)
from .midi_input import MidiInput
from .presets import (
    T_FILTER_CUTOFF,
    T_LAYER_VOLUME,
    T_MASTER_VOLUME,
    T_OCTAVE_SHIFT,
    T_REVERB_LEVEL,
    T_ROTARY_SPEED,
    CCMapping,
    Fx,
    Preset,
    PresetStore,
)
from .synth_engine import (
    AudioDevice,
    CatalogEntry,
    GraphInfo,
    SynthEngine,
    find_default_soundfont,
    probe_pipewire_quantum,
    resolve_audio_device,
)

log = logging.getLogger("keyrig.rig")

_QUANTUM_REFRESH_SECONDS = 5.0
_ROTARY_STEP_SECONDS = 1 / 30


@dataclass
class DispatchStats:
    last_ms: float = 0.0
    peak_ms: float = 0.0


class Rig:
    def __init__(
        self,
        config: Config,
        engine: SynthEngine,
        store: PresetStore,
        midi_port: str | None = None,
        audio_device: str | None = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.store = store
        self.router = Router(engine, config.drum_channel - 1, config.pad_base_note)
        self.router.transpose = config.transpose
        self.router.key_curve = config.key_curve if config.key_curve in VELOCITY_CURVES else "Linear"
        self.router.pad_curve = config.pad_curve if config.pad_curve in VELOCITY_CURVES else "Linear"
        # Command-line overrides apply to this run only and are not saved.
        self.midi = MidiInput(self._on_midi, self._on_disconnect, midi_port or config.midi_port)
        self._audio_device = audio_device or config.audio_device
        self.fx = Fx()
        self.master_volume = config.master_volume
        self.preset_index = 0
        self.preset_name = ""
        self.last_event = ""
        self.last_event_time = 0.0
        self.dispatch = DispatchStats()
        self.graph_quantum: GraphInfo | None = None
        self.learning = False
        self.learned: tuple[int, int] | None = None
        self._mapping_index: dict[tuple[int, int], CCMapping] = {}
        self._stopping = threading.Event()
        self._housekeeping: threading.Thread | None = None

    # Lifecycle

    def load_soundfonts(self) -> None:
        main = self.config.soundfont or find_default_soundfont()
        if main:
            self.engine.load_soundfont(main)
        for path in self.config.extra_soundfonts:
            self.engine.load_soundfont(path)

    def start(self) -> None:
        self.load_soundfonts()
        self.store.load()
        self._rebuild_mapping_index()
        self._apply_master()
        index = self.store.index_of(self.config.last_preset)
        self.load_preset(index if index is not None else 0)
        self.engine.start_audio(resolve_audio_device(self._audio_device))
        self.midi.start()
        self._housekeeping = threading.Thread(target=self._housekeep, name="keyrig-housekeeping", daemon=True)
        self._housekeeping.start()
        threading.Thread(target=self._spin_rotary, name="keyrig-rotary", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
        self.midi.stop()
        self.router.panic()
        self.config.master_volume = self.master_volume
        self.config.save_state()
        self.engine.delete()

    def _housekeep(self) -> None:
        while not self._stopping.is_set():
            device = self.engine.audio_device
            self.graph_quantum = (
                probe_pipewire_quantum() if device is not None and device.driver == "pipewire" else None
            )
            self._stopping.wait(_QUANTUM_REFRESH_SECONDS)

    def _spin_rotary(self) -> None:
        while not self._stopping.wait(_ROTARY_STEP_SECONDS):
            self.router.step_rotary(_ROTARY_STEP_SECONDS)

    # MIDI

    def _on_midi(self, message: mido.Message, received: float) -> None:
        kind = message.type
        if kind == "note_on" and message.velocity > 0:
            self.router.note_on(message.channel, message.note, message.velocity)
            self._record_dispatch(received)
            self._activity(f"ch{message.channel + 1} on {note_name(message.note)} v{message.velocity}")
        elif kind in ("note_on", "note_off"):
            self.router.note_off(message.channel, message.note)
            self._record_dispatch(received)
            self._activity(f"ch{message.channel + 1} off {note_name(message.note)}")
        elif kind == "control_change":
            self._on_cc(message.channel, message.control, message.value)
            self._activity(f"ch{message.channel + 1} CC{message.control}={message.value}")
        elif kind == "pitchwheel":
            if message.channel != self.router.drum_channel:
                self.router.pitch_bend(message.pitch)
            self._activity(f"ch{message.channel + 1} bend {message.pitch}")
        elif kind == "program_change":
            self._activity(f"ch{message.channel + 1} PC{message.program}")
            if self.config.program_change_presets and message.program < len(self.store.presets):
                self.load_preset(message.program)

    def _record_dispatch(self, received: float) -> None:
        elapsed = (time.perf_counter() - received) * 1000
        self.dispatch.last_ms = elapsed
        self.dispatch.peak_ms = max(self.dispatch.peak_ms, elapsed)

    def _activity(self, description: str) -> None:
        self.last_event = description
        self.last_event_time = time.monotonic()

    def _on_cc(self, channel: int, control: int, value: int) -> None:
        # A pedal is never a "knob", so holding it during learn must not bind it.
        if self.learning and control != CC_SUSTAIN:
            self.learned = (channel, control)
            self.learning = False
            return
        mapping = self._mapping_index.get((channel, control))
        if mapping is not None:
            self._apply_mapping(mapping, value)
        elif channel != self.router.drum_channel:
            if control == CC_SUSTAIN:
                self.router.sustain(value)
            elif control == CC_MOD:
                self.router.mod_wheel(value)

    def _on_disconnect(self) -> None:
        # Note-offs from the lost device will never arrive.
        self.router.panic()

    def panic(self) -> None:
        self.router.panic()
        self._activity("PANIC")

    # Learn mode and mappings

    def begin_learn(self) -> None:
        self.learned = None
        self.learning = True

    def cancel_learn(self) -> None:
        self.learning = False
        self.learned = None

    def assign_learned(self, target: str, layer: int) -> CCMapping | None:
        if self.learned is None:
            return None
        channel, control = self.learned
        mapping = CCMapping(channel, control, target, layer)
        self.store.set_mapping(mapping)
        self._rebuild_mapping_index()
        self.learned = None
        return mapping

    def remove_mapping(self, mapping: CCMapping) -> None:
        self.store.remove_mapping(mapping.channel, mapping.cc)
        self._rebuild_mapping_index()

    def _rebuild_mapping_index(self) -> None:
        self._mapping_index = {(m.channel, m.cc): m for m in self.store.mappings}

    def _apply_mapping(self, mapping: CCMapping, value: int) -> None:
        with self.router.lock:
            if mapping.target == T_MASTER_VOLUME:
                self.set_master(value)
            elif mapping.target == T_REVERB_LEVEL:
                self.fx.reverb_level = value / 127
                self._apply_reverb()
            elif mapping.target == T_ROTARY_SPEED:
                self.router.set_rotary_fast(value >= 64)
            else:
                layers = range(NUM_LAYERS) if mapping.layer == 0 else [mapping.layer - 1]
                for index in layers:
                    if mapping.target == T_LAYER_VOLUME:
                        self.set_layer_value(index, "volume", value)
                    elif mapping.target == T_FILTER_CUTOFF:
                        self.set_layer_value(index, "cutoff", value)
                    elif mapping.target == T_OCTAVE_SHIFT:
                        span = OCTAVE_MAX - OCTAVE_MIN
                        self.set_layer_value(index, "octave", round(value / 127 * span) + OCTAVE_MIN)

    # Presets

    def load_preset(self, index: int) -> None:
        if not self.store.presets:
            return
        index %= len(self.store.presets)
        preset = self.store.presets[index]
        with self.router.lock:
            self.router.load(
                copy.deepcopy(preset.layers), copy.deepcopy(preset.drum), smooth=self.config.smooth_preset_changes
            )
            self.fx = copy.copy(preset.fx)
            self._apply_reverb()
            self.engine.set_chorus(self.fx.chorus_on)
            self.preset_index = index
            self.preset_name = preset.name
            self.config.last_preset = preset.name

    def cycle_preset(self, delta: int) -> None:
        self.load_preset(self.preset_index + delta)

    def save_preset(self, name: str) -> None:
        with self.router.lock:
            preset = Preset(
                name=name,
                layers=copy.deepcopy(self.router.layers),
                drum=copy.deepcopy(self.router.drum),
                fx=copy.copy(self.fx),
            )
            self.preset_index = self.store.store(preset)
            self.preset_name = name
            self.config.last_preset = name

    # Layer parameters

    @property
    def layers(self) -> list[Layer]:
        return self.router.layers

    @property
    def drum(self) -> DrumLayer:
        return self.router.drum

    def toggle_layer(self, index: int) -> None:
        self.router.set_layer_enabled(index, not self.layers[index].enabled)

    def toggle_drum(self) -> None:
        self.router.set_drum_enabled(not self.drum.enabled)

    def toggle_layer_pedal(self, index: int) -> None:
        self.router.set_layer_pedal(index, not self.layers[index].pedal)

    def toggle_layer_rotary(self, index: int) -> None:
        self.router.set_layer_rotary(index, not self.layers[index].rotary)

    @property
    def rotary_fast(self) -> bool:
        return self.router.rotary_target_hz == ROTARY_FAST_HZ

    def toggle_rotary_speed(self) -> None:
        self.router.set_rotary_fast(not self.rotary_fast)

    def set_transpose(self, semitones: int) -> None:
        with self.router.lock:
            self.router.transpose = max(TRANSPOSE_MIN, min(TRANSPOSE_MAX, semitones))
            self.config.transpose = self.router.transpose

    def cycle_velocity_curve(self, which: str, direction: int) -> None:
        names = list(VELOCITY_CURVES)
        with self.router.lock:
            current = self.router.key_curve if which == "key" else self.router.pad_curve
            chosen = names[(names.index(current) + direction) % len(names)]
            if which == "key":
                self.router.key_curve = self.config.key_curve = chosen
            else:
                self.router.pad_curve = self.config.pad_curve = chosen

    def set_layer_value(self, index: int, field: str, value: int) -> None:
        with self.router.lock:
            layer = self.layers[index]
            channel = self.router.channel(index)
            if field in ("volume", "cutoff"):
                value = max(0, min(127, value))
                if field == "volume":
                    layer.volume = value
                    self.engine.cc(channel, 7, value)
                else:
                    layer.cutoff = value
                    self.engine.set_cutoff(channel, value)
            elif field in ("attack", "release"):
                setattr(layer, field, max(0, value))
                self.router.apply_envelope(index)
            elif field == "octave":
                layer.octave = max(OCTAVE_MIN, min(OCTAVE_MAX, value))
            elif field == "low":
                layer.low = max(0, min(layer.high, value))
            elif field == "high":
                layer.high = max(layer.low, min(127, value))

    def set_instrument(self, index: int, entry: CatalogEntry) -> None:
        with self.router.lock:
            self.layers[index].instrument = Instrument(entry.soundfont, entry.bank, entry.program, entry.name)
            self.router.apply_layer(index)

    def set_drum_kit(self, entry: CatalogEntry) -> None:
        with self.router.lock:
            self.drum.soundfont = entry.soundfont
            self.drum.kit = entry.program
            self.drum.name = entry.name
            self.router.apply_drum()

    def set_drum_volume(self, value: int) -> None:
        with self.router.lock:
            self.drum.volume = max(0, min(127, value))
            self.engine.cc(9, 7, self.drum.volume)

    def step_instrument(self, index: int, delta: int) -> None:
        entries = self.engine.melodic_entries()
        if entries:
            current = self.layers[index].instrument
            position = _find_entry(entries, current.soundfont, current.bank, current.program)
            self.set_instrument(index, entries[(position + delta) % len(entries)])

    def step_drum_kit(self, delta: int) -> None:
        kits = self.engine.drum_kits()
        if kits:
            position = _find_entry(kits, self.drum.soundfont, 128, self.drum.kit)
            self.set_drum_kit(kits[(position + delta) % len(kits)])

    # Globals

    def _apply_master(self) -> None:
        self.engine.set_gain(self.master_volume / 127 * self.config.max_gain)

    def _apply_reverb(self) -> None:
        self.engine.set_reverb(self.fx.reverb_on, self.fx.reverb_room, self.fx.reverb_level)

    def set_master(self, value: int) -> None:
        self.master_volume = max(0, min(127, value))
        self.config.master_volume = self.master_volume
        self._apply_master()

    def set_reverb(self, on: bool | None = None, room: float | None = None, level: float | None = None) -> None:
        with self.router.lock:
            if on is not None:
                self.fx.reverb_on = on
            if room is not None:
                self.fx.reverb_room = max(0.0, min(1.0, room))
            if level is not None:
                self.fx.reverb_level = max(0.0, min(1.0, level))
            self._apply_reverb()

    def set_chorus(self, on: bool) -> None:
        with self.router.lock:
            self.fx.chorus_on = on
            self.engine.set_chorus(on)

    # Devices

    def select_midi_port(self, preference: str) -> None:
        self.config.midi_port = preference
        self.config.save_state()
        self.midi.select(preference)

    def select_audio_device(self, device: AudioDevice) -> bool:
        previous = self.engine.audio_device
        ok = self.engine.start_audio([device])
        if ok:
            self.config.audio_device = device.id
            self.config.save_state()
        elif previous is not None:
            self.engine.start_audio([previous])  # a bad choice must not leave you silent
        self.graph_quantum = None
        return ok


def _find_entry(entries: list[CatalogEntry], soundfont: str, bank: int, program: int) -> int:
    for position, entry in enumerate(entries):
        if entry.bank == bank and entry.program == program and (not soundfont or entry.soundfont == soundfont):
            return position
    return 0
