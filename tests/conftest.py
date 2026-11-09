from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class Entry:
    soundfont: str
    bank: int
    program: int
    name: str


class FakeEngine:
    """Records synth calls and tracks which notes would be sounding."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.sounding: set[tuple[int, int]] = set()
        self.pedal: dict[int, int] = {}
        self.sustained: set[tuple[int, int]] = set()

    def noteon(self, channel, note, velocity):
        self.calls.append(("noteon", channel, note, velocity))
        self.sounding.add((channel, note))

    def noteoff(self, channel, note):
        self.calls.append(("noteoff", channel, note))
        if self.pedal.get(channel, 0) >= 64:
            self.sustained.add((channel, note))
        else:
            self.sounding.discard((channel, note))

    def cc(self, channel, control, value):
        self.calls.append(("cc", channel, control, value))
        if control == 64:
            self.pedal[channel] = value
            if value < 64:
                for item in [s for s in self.sustained if s[0] == channel]:
                    self.sustained.discard(item)
                    self.sounding.discard(item)

    def pitch_bend(self, channel, value):
        self.calls.append(("bend", channel, value))

    def all_notes_off(self, channel):
        # Matches FluidSynth: voices stay sustained while the pedal is down.
        self.calls.append(("all_notes_off", channel))
        for item in [s for s in self.sounding if s[0] == channel]:
            if self.pedal.get(channel, 0) >= 64:
                self.sustained.add(item)
            else:
                self.sounding.discard(item)

    def all_sounds_off(self, channel):
        self.calls.append(("all_sounds_off", channel))
        self.sounding = {s for s in self.sounding if s[0] != channel}
        self.sustained = {s for s in self.sustained if s[0] != channel}

    def set_cutoff(self, channel, value):
        self.calls.append(("cutoff", channel, value))

    def select_program(self, channel, soundfont, bank, program):
        self.calls.append(("program", channel, bank, program))
        return Entry(soundfont or "fake.sf2", bank, program, f"P{bank}:{program}")

    def set_envelope(self, channel, soundfont, bank, program, attack, release):
        self.calls.append(("envelope", channel, attack, release))

    def set_rotary(self, channel, soundfont, bank, program, on, hz):
        self.calls.append(("rotary", channel, on, hz))

    def names(self, kind):
        return [c for c in self.calls if c[0] == kind]

    # Rig-level API
    def set_gain(self, gain):
        self.calls.append(("gain", gain))

    def set_reverb(self, on, room, level):
        self.calls.append(("reverb", on, room, level))

    def set_chorus(self, on):
        self.calls.append(("chorus", on))


@pytest.fixture
def engine():
    return FakeEngine()
