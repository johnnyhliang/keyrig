"""Layer model and the note router that fans MIDI out to FluidSynth channels."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

NUM_LAYERS = 4
NUM_PADS = 16
# Two banks of layer channels. A preset change moves to the other bank, so the
# previous preset's notes can ring out on their own channels with their own sound.
BANK_BASES = (0, 4)
DRUM_SYNTH_CHANNEL = 9
SYNTH_CHANNELS = (*range(BANK_BASES[1] + NUM_LAYERS), DRUM_SYNTH_CHANNEL)
OCTAVE_MIN, OCTAVE_MAX = -3, 3
TRANSPOSE_MIN, TRANSPOSE_MAX = -12, 12
CC_MOD, CC_VOLUME, CC_SUSTAIN = 1, 7, 64
PAD_FLASH_SECONDS = 0.18

ROTARY_SLOW_HZ = 0.8
ROTARY_FAST_HZ = 6.7
ROTARY_SPEED_UP_SECONDS = 0.9  # time constants: real rotors take a while to change speed
ROTARY_SLOW_DOWN_SECONDS = 1.8

#: Velocity curves: output = input ^ exponent. Below 1 makes soft playing louder.
VELOCITY_CURVES = {"Linear": 1.0, "Light": 0.7, "Lighter": 0.5, "Heavy": 1.4}


def apply_velocity_curve(velocity: int, curve: str) -> int:
    exponent = VELOCITY_CURVES.get(curve, 1.0)
    return max(1, min(127, round(127 * (velocity / 127) ** exponent)))


@dataclass
class Instrument:
    soundfont: str = ""  # file basename; empty means the main SoundFont
    bank: int = 0
    program: int = 0
    name: str = ""


@dataclass
class Layer:
    enabled: bool = False
    instrument: Instrument = field(default_factory=Instrument)
    volume: int = 100
    octave: int = 0
    low: int = 0
    high: int = 127
    cutoff: int = 127
    attack: int = 0  # milliseconds; 0 keeps the patch's own attack
    release: int = 0  # milliseconds; 0 keeps the patch's own release
    pedal: bool = True  # whether this layer responds to the sustain pedal
    rotary: bool = False


@dataclass
class DrumLayer:
    enabled: bool = True
    soundfont: str = ""
    kit: int = 0
    name: str = ""
    volume: int = 110


@dataclass
class Snapshot:
    held: list[int]
    pads: list[float]  # brightness 0.0-1.0, index 0 is the bottom-left pad
    sustain: bool = False
    mod: int = 0
    bend: int = 0
    sounding: frozenset[int] = frozenset()  # layer indexes with a key down, plus the drum channel
    lowest_sounded: int | None = None  # lowest note actually played, after octave and transpose


class Router:
    """Owns everything sounding. One lock serialises the MIDI thread and the UI."""

    def __init__(self, engine, drum_channel: int = 9, pad_base: int = 36) -> None:
        self.lock = threading.RLock()
        self.engine = engine
        self.drum_channel = drum_channel  # 0-based
        self.pad_base = pad_base
        self.layers = [Layer() for _ in range(NUM_LAYERS)]
        self.drum = DrumLayer()
        self.transpose = 0
        self.key_curve = "Linear"
        self.pad_curve = "Linear"
        self.rotary_hz = ROTARY_SLOW_HZ
        self.rotary_target_hz = ROTARY_SLOW_HZ
        self._bank = BANK_BASES[0]
        self._tail_layers = [Layer() for _ in range(NUM_LAYERS)]  # what the other bank last played
        # (source channel, source note) -> [(synth channel, sounded note)].
        # Releases follow this map, so editing a layer mid-note cannot strand a voice.
        self._active: dict[tuple[int, int], list[tuple[int, int]]] = {}
        self._sustain = False
        self._mod = 0
        self._bend = 0
        self._pad_held: dict[int, int] = {}
        self._pad_hit: dict[int, tuple[float, int]] = {}

    def channel(self, index: int) -> int:
        """The FluidSynth channel currently used by a layer."""
        return self._bank + index

    @property
    def _other_bank(self) -> int:
        return BANK_BASES[1] if self._bank == BANK_BASES[0] else BANK_BASES[0]

    def _layer_on_channel(self, synth_channel: int) -> Layer | None:
        if self._bank <= synth_channel < self._bank + NUM_LAYERS:
            return self.layers[synth_channel - self._bank]
        if self._other_bank <= synth_channel < self._other_bank + NUM_LAYERS:
            return self._tail_layers[synth_channel - self._other_bank]
        return None

    # Notes

    def note_on(self, channel: int, note: int, velocity: int) -> None:
        with self.lock:
            key = (channel, note)
            self._release(key)
            targets: list[tuple[int, int]] = []
            if channel == self.drum_channel:
                velocity = apply_velocity_curve(velocity, self.pad_curve)
                pad = note - self.pad_base
                if 0 <= pad < NUM_PADS:
                    self._pad_held[pad] = velocity
                    self._pad_hit[pad] = (time.monotonic(), velocity)
                if self.drum.enabled:
                    self.engine.noteon(DRUM_SYNTH_CHANNEL, note, velocity)
                    targets.append((DRUM_SYNTH_CHANNEL, note))
            else:
                velocity = apply_velocity_curve(velocity, self.key_curve)
                for index, layer in enumerate(self.layers):
                    if layer.enabled and layer.low <= note <= layer.high:
                        sounded = note + 12 * layer.octave + self.transpose
                        if 0 <= sounded <= 127:
                            self.engine.noteon(self.channel(index), sounded, velocity)
                            targets.append((self.channel(index), sounded))
            self._active[key] = targets

    def note_off(self, channel: int, note: int) -> None:
        with self.lock:
            key = (channel, note)
            self._release(key)
            self._active.pop(key, None)
            if channel == self.drum_channel:
                self._pad_held.pop(note - self.pad_base, None)

    def _release(self, key: tuple[int, int]) -> None:
        for synth_channel, sounded in self._active.get(key, ()):
            self.engine.noteoff(synth_channel, sounded)

    # Performance controllers go to both banks: layers switched on mid-performance
    # already have the right state, and a ringing tail still follows the pedal.

    def _pedal_value(self, synth_channel: int, value: int) -> int:
        layer = self._layer_on_channel(synth_channel)
        return value if layer is not None and layer.pedal else 0

    def _layer_channels(self) -> range:
        return range(BANK_BASES[1] + NUM_LAYERS)

    def sustain(self, value: int) -> None:
        with self.lock:
            self._sustain = value >= 64
            for channel in self._layer_channels():
                self.engine.cc(channel, CC_SUSTAIN, self._pedal_value(channel, value))

    def mod_wheel(self, value: int) -> None:
        with self.lock:
            self._mod = value
            for channel in self._layer_channels():
                self.engine.cc(channel, CC_MOD, value)

    def pitch_bend(self, value: int) -> None:
        with self.lock:
            self._bend = value
            for channel in self._layer_channels():
                self.engine.pitch_bend(channel, value)

    # Silencing

    def _silence(self, synth_channel: int) -> None:
        # FluidSynth keeps note-offs sustaining while CC64 is down, so the pedal
        # has to be lifted for all-notes-off to take effect on this channel.
        self.engine.cc(synth_channel, CC_SUSTAIN, 0)
        self.engine.all_notes_off(synth_channel)
        if self._sustain and self._pedal_value(synth_channel, 127):
            self.engine.cc(synth_channel, CC_SUSTAIN, 127)
        for key, targets in self._active.items():
            self._active[key] = [t for t in targets if t[0] != synth_channel]

    def silence_all(self) -> None:
        """All-notes-off everywhere. Physically held keys stay shown as held."""
        with self.lock:
            for channel in SYNTH_CHANNELS:
                self._silence(channel)

    def panic(self) -> None:
        """Hard stop: cut every voice and forget all note and controller state."""
        with self.lock:
            self._sustain = False
            self._mod = 0
            self._bend = 0
            for channel in SYNTH_CHANNELS:
                self.engine.cc(channel, CC_SUSTAIN, 0)
                self.engine.all_sounds_off(channel)
                self.engine.cc(channel, CC_MOD, 0)
                self.engine.pitch_bend(channel, 0)
            self._active.clear()
            self._pad_held.clear()

    # Layer changes

    def set_layer_enabled(self, index: int, enabled: bool) -> None:
        with self.lock:
            self.layers[index].enabled = enabled
            self._silence(self.channel(index))

    def set_drum_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.drum.enabled = enabled
            self._silence(DRUM_SYNTH_CHANNEL)

    def set_layer_pedal(self, index: int, enabled: bool) -> None:
        with self.lock:
            self.layers[index].pedal = enabled
            self.engine.cc(self.channel(index), CC_SUSTAIN, 127 if self._sustain and enabled else 0)

    def set_layer_rotary(self, index: int, enabled: bool) -> None:
        with self.lock:
            self.layers[index].rotary = enabled
            self._apply_rotary(index)

    def apply_envelope(self, index: int) -> None:
        with self.lock:
            layer = self.layers[index]
            instrument = layer.instrument
            self.engine.set_envelope(
                self.channel(index), instrument.soundfont, instrument.bank, instrument.program,
                layer.attack, layer.release,
            )

    def _apply_rotary(self, index: int) -> None:
        layer = self.layers[index]
        instrument = layer.instrument
        self.engine.set_rotary(
            self.channel(index), instrument.soundfont, instrument.bank, instrument.program,
            layer.rotary, self.rotary_hz,
        )

    def set_rotary_fast(self, fast: bool) -> None:
        with self.lock:
            self.rotary_target_hz = ROTARY_FAST_HZ if fast else ROTARY_SLOW_HZ

    def step_rotary(self, elapsed: float) -> None:
        """Move the rotor toward its target speed, as a real one spins up or down."""
        with self.lock:
            if self.rotary_hz == self.rotary_target_hz:
                return
            speeding_up = self.rotary_target_hz > self.rotary_hz
            constant = ROTARY_SPEED_UP_SECONDS if speeding_up else ROTARY_SLOW_DOWN_SECONDS
            self.rotary_hz += (self.rotary_target_hz - self.rotary_hz) * (1 - math.exp(-elapsed / constant))
            if abs(self.rotary_target_hz - self.rotary_hz) < 0.02:
                self.rotary_hz = self.rotary_target_hz
            for index, layer in enumerate(self.layers):
                if layer.rotary:
                    self._apply_rotary(index)

    def apply_layer(self, index: int) -> None:
        with self.lock:
            layer = self.layers[index]
            channel = self.channel(index)
            selected = self.engine.select_program(
                channel, layer.instrument.soundfont, layer.instrument.bank, layer.instrument.program
            )
            if selected is not None:
                layer.instrument = Instrument(
                    selected.soundfont, selected.bank, selected.program, selected.name
                )
            self.engine.cc(channel, CC_VOLUME, layer.volume)
            self.engine.set_cutoff(channel, layer.cutoff)
            self.apply_envelope(index)
            self._apply_rotary(index)

    def apply_drum(self) -> None:
        with self.lock:
            selected = self.engine.select_program(DRUM_SYNTH_CHANNEL, self.drum.soundfont, 128, self.drum.kit)
            if selected is not None:
                self.drum.soundfont = selected.soundfont
                self.drum.kit = selected.program
                self.drum.name = selected.name
            self.engine.cc(DRUM_SYNTH_CHANNEL, CC_VOLUME, self.drum.volume)

    def load(self, layers: list[Layer], drum: DrumLayer, smooth: bool = False) -> None:
        """Replace all layers.

        With smooth, sounding notes keep their old sound on the other bank until
        their keys and the pedal are released. Otherwise they are released now.
        """
        with self.lock:
            if smooth:
                incoming = self._other_bank
                for offset in range(NUM_LAYERS):  # anything left from two presets ago
                    self._silence(incoming + offset)
                self._tail_layers = self.layers
                self._bank = incoming
            else:
                self.silence_all()
            self.layers = layers
            self.drum = drum
            for index in range(NUM_LAYERS):
                self.apply_layer(index)
                channel = self.channel(index)
                self.engine.cc(channel, CC_SUSTAIN, 127 if self._sustain and layers[index].pedal else 0)
                self.engine.cc(channel, CC_MOD, self._mod)
                self.engine.pitch_bend(channel, self._bend)
            self.apply_drum()

    # Display

    def snapshot(self) -> Snapshot:
        with self.lock:
            held = sorted({n for (c, n) in self._active if c != self.drum_channel})
            now = time.monotonic()
            pads = [0.0] * NUM_PADS
            for pad in range(NUM_PADS):
                level = 0.0
                if pad in self._pad_held:
                    level = max(0.5, self._pad_held[pad] / 127)
                elif pad in self._pad_hit:
                    hit_time, velocity = self._pad_hit[pad]
                    remaining = 1 - (now - hit_time) / PAD_FLASH_SECONDS
                    if remaining > 0:
                        level = max(0.5, velocity / 127) * remaining
                pads[pad] = level
            sounding = set()
            lowest: int | None = None
            for targets in self._active.values():
                for synth_channel, note in targets:
                    if synth_channel == DRUM_SYNTH_CHANNEL:
                        sounding.add(DRUM_SYNTH_CHANNEL)
                        continue
                    lowest = note if lowest is None else min(lowest, note)
                    if self._bank <= synth_channel < self._bank + NUM_LAYERS:
                        sounding.add(synth_channel - self._bank)
            return Snapshot(held, pads, self._sustain, self._mod, self._bend, frozenset(sounding), lowest)
