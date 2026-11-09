"""In-process FluidSynth: SoundFonts, audio output, effects and latency reporting."""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import fluidsynth

from .gm import GM_PROGRAMS

log = logging.getLogger("keyrig.synth")

AUTO = "auto"
DRUM_BANK = 128
GEN_FILTERFC = 8  # SoundFont generator number for the initial filter cutoff
FILTER_RANGE_CENTS = 9600  # cutoff 0 lowers the filter by eight octaves
GEN_MOD_LFO_TO_PITCH = 5
GEN_MOD_LFO_TO_FILTER = 10
GEN_MOD_LFO_TO_VOLUME = 13
GEN_MOD_LFO_FREQ = 22
GEN_VOL_ATTACK = 34
GEN_VOL_RELEASE = 38
ROTARY_PITCH_CENTS = 6.0
ROTARY_FILTER_CENTS = 150.0
ROTARY_VOLUME_CENTIBELS = 30.0
PROBE_KEYS = (60, 36, 84)  # some patches, such as drum kits, have no sample on every key
# Unique per process, so the level meter can never attach to another keyrig instance.
NODE_NAME = f"keyrig-{os.getpid()}"

SOUNDFONT_SEARCH_PATHS = [
    "/usr/share/soundfonts/FluidR3_GM.sf2",
    "/usr/share/soundfonts/default.sf2",
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    "/usr/share/sounds/sf2/default-GM.sf2",
    "/usr/share/sounds/sf3/default-GM.sf3",
]

_CFUNC_PARAM = 1
_fluid_set_gen = fluidsynth.cfunc(
    "fluid_synth_set_gen", ctypes.c_int,
    ("synth", ctypes.c_void_p, _CFUNC_PARAM), ("chan", ctypes.c_int, _CFUNC_PARAM),
    ("param", ctypes.c_int, _CFUNC_PARAM), ("value", ctypes.c_float, _CFUNC_PARAM),
)
_fluid_reverb_on = fluidsynth.cfunc(
    "fluid_synth_reverb_on", ctypes.c_int,
    ("synth", ctypes.c_void_p, _CFUNC_PARAM), ("group", ctypes.c_int, _CFUNC_PARAM),
    ("on", ctypes.c_int, _CFUNC_PARAM),
)
_fluid_chorus_on = fluidsynth.cfunc(
    "fluid_synth_chorus_on", ctypes.c_int,
    ("synth", ctypes.c_void_p, _CFUNC_PARAM), ("group", ctypes.c_int, _CFUNC_PARAM),
    ("on", ctypes.c_int, _CFUNC_PARAM),
)
_fluid_cpu_load = fluidsynth.cfunc(
    "fluid_synth_get_cpu_load", ctypes.c_double, ("synth", ctypes.c_void_p, _CFUNC_PARAM),
)
_fluid_get_voicelist = fluidsynth.cfunc(
    "fluid_synth_get_voicelist", None,
    ("synth", ctypes.c_void_p, _CFUNC_PARAM), ("buf", ctypes.POINTER(ctypes.c_void_p), _CFUNC_PARAM),
    ("size", ctypes.c_int, _CFUNC_PARAM), ("id", ctypes.c_int, _CFUNC_PARAM),
)
_fluid_voice_gen_get = fluidsynth.cfunc(
    "fluid_voice_gen_get", ctypes.c_float,
    ("voice", ctypes.c_void_p, _CFUNC_PARAM), ("gen", ctypes.c_int, _CFUNC_PARAM),
)
_LOG_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
_fluid_set_log = fluidsynth.cfunc(
    "fluid_set_log_function", _LOG_CALLBACK,
    ("level", ctypes.c_int, _CFUNC_PARAM), ("fun", _LOG_CALLBACK, _CFUNC_PARAM),
    ("data", ctypes.c_void_p, _CFUNC_PARAM),
)

_LEVELS = {0: logging.CRITICAL, 1: logging.ERROR, 2: logging.WARNING, 3: logging.INFO, 4: logging.DEBUG}


@dataclass(frozen=True)
class AudioDevice:
    id: str
    label: str
    driver: str
    device: str = ""


@dataclass(frozen=True)
class CatalogEntry:
    soundfont: str
    bank: int
    program: int
    name: str

    @property
    def search_text(self) -> str:
        gm = GM_PROGRAMS[self.program] if self.bank != DRUM_BANK else "drum kit"
        return f"{self.name} {gm} {self.soundfont} {self.bank}:{self.program}".lower()


@dataclass(frozen=True)
class LatencyInfo:
    driver: str
    period_size: int
    periods: int
    sample_rate: int
    configured_ms: float


@dataclass(frozen=True)
class PatchInfo:
    """A patch's own values, needed because FluidSynth only accepts offsets from them."""

    attack: float  # timecents
    release: float  # timecents
    mod_lfo_freq: float  # absolute cents


@dataclass
class _LoadedFont:
    sfid: int
    path: str
    basename: str


def find_default_soundfont() -> str | None:
    override = os.environ.get("KEYRIG_SOUNDFONT")
    candidates = [override] if override else SOUNDFONT_SEARCH_PATHS
    return next((p for p in candidates if Path(p).exists()), None)


def _run(command: list[str], timeout: float = 3.0) -> str | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def list_pipewire_sinks() -> list[AudioDevice] | None:
    """Sinks known to PipeWire, or None when PipeWire is not running."""
    output = _run(["pw-dump"])
    if output is None:
        return None
    try:
        objects = json.loads(output)
    except json.JSONDecodeError:
        return None
    sinks = []
    for obj in objects:
        info = obj.get("info")
        props = info.get("props", {}) if isinstance(info, dict) else {}
        if props.get("media.class") == "Audio/Sink" and props.get("node.name"):
            name = props["node.name"]
            label = props.get("node.description") or props.get("node.nick") or name
            sinks.append(AudioDevice(f"pipewire:{name}", f"PipeWire: {label}", "pipewire", name))
    return sorted(sinks, key=lambda s: s.label)


def list_alsa_devices() -> list[AudioDevice]:
    output = _run(["aplay", "-l"]) or ""
    devices = []
    pattern = re.compile(r"^card \d+: (\S+) \[(.*?)\], device (\d+): (.*?) \[(.*?)\]")
    for line in output.splitlines():
        match = pattern.match(line)
        if match:
            card, card_label, number, device_name, device_label = match.groups()
            device_label = device_label or device_name.replace(" (*)", "")
            pcm = f"plughw:CARD={card},DEV={number}"
            devices.append(AudioDevice(f"alsa:{pcm}", f"ALSA direct: {card_label} ({device_label})", "alsa", pcm))
    return devices


def list_audio_devices() -> list[AudioDevice]:
    sinks = list_pipewire_sinks()
    devices = []
    if sinks is not None:
        devices.append(AudioDevice("pipewire:", "PipeWire: system default output", "pipewire"))
        devices.extend(sinks)
    devices.append(AudioDevice("alsa:default", "ALSA: default device", "alsa", "default"))
    devices.extend(list_alsa_devices())
    return devices


def resolve_audio_device(device_id: str) -> list[AudioDevice]:
    """Devices to try for a saved id, most preferred first."""
    devices = list_audio_devices()
    by_id = {d.id: d for d in devices}
    fallback = [d for d in devices if d.id in ("pipewire:", "alsa:default")]
    if device_id != AUTO and device_id in by_id:
        return [by_id[device_id]] + [d for d in fallback if d.id != device_id]
    return fallback


@dataclass(frozen=True)
class GraphInfo:
    quantum: int
    rate: int
    errors: int  # xruns PipeWire has counted against keyrig's stream


def probe_pipewire_quantum() -> GraphInfo | None:
    """Return the state of the PipeWire graph keyrig is running in."""
    output = _run(["pw-top", "-b", "-n", "2"], timeout=4.0)
    if output is None:
        return None
    driver: tuple[int, int] | None = None
    found: GraphInfo | None = None
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] == "S":
            continue
        if not fields[-1] == NODE_NAME:
            # Driver rows have no "+" tree marker before the node name.
            if "+" not in fields and fields[2].isdigit() and fields[3].isdigit() and int(fields[2]) > 0:
                driver = (int(fields[2]), int(fields[3]))
            continue
        if driver is not None:  # later samples overwrite the zeroed first pw-top sample
            errors = int(fields[8]) if len(fields) > 8 and fields[8].isdigit() else 0
            found = GraphInfo(driver[0], driver[1], errors)
    return found


class SynthEngine:
    def __init__(
        self,
        sample_rate: int = 48000,
        period_size: int = 128,
        periods: int = 2,
        polyphony: int = 128,
        cpu_cores: int = 1,
        gain: float = 0.7,
    ) -> None:
        self.log_messages: deque[str] = deque(maxlen=50)
        self._log_callback = _LOG_CALLBACK(self._on_fluid_log)
        for level in _LEVELS:
            _fluid_set_log(level, self._log_callback, None)

        self.sample_rate = sample_rate
        self.period_size = period_size
        self.periods = periods
        self._fs = fluidsynth.Synth(
            gain=gain,
            samplerate=float(sample_rate),
            channels=16,
            **{
                "audio.period-size": period_size,
                "audio.periods": periods,
                "synth.polyphony": polyphony,
                "synth.cpu-cores": cpu_cores,
            },
        )
        self._driver = None
        self.audio_generation = 0  # counts stream (re)creations
        self.audio_device: AudioDevice | None = None
        self.audio_error = ""
        self._fonts: list[_LoadedFont] = []
        self.catalog: list[CatalogEntry] = []
        self._preset_names: dict[tuple[str, int, int], str] = {}
        self._probe: fluidsynth.Synth | None = None  # silent synth for reading patch values
        self._probe_fonts: dict[str, int] = {}
        self._patch_info: dict[tuple[str, int, int], PatchInfo | None] = {}

    def _on_fluid_log(self, level: int, message: bytes | None, _data) -> None:
        text = (message or b"").decode("utf-8", "replace")
        log.log(_LEVELS.get(level, logging.INFO), "%s", text)
        if level <= 2:
            self.log_messages.append(text)

    # SoundFonts

    def load_soundfont(self, path: str) -> bool:
        expanded = str(Path(path).expanduser())
        if not Path(expanded).is_file():
            self.log_messages.append(f"SoundFont not found: {expanded}")
            return False
        sfid = self._fs.sfload(expanded)
        if sfid < 0:
            self.log_messages.append(f"Could not load SoundFont: {expanded}")
            return False
        basename = Path(expanded).name
        self._fonts.append(_LoadedFont(sfid, expanded, basename))
        for bank in range(DRUM_BANK + 1):
            for program in range(128):
                name = self._fs.sfpreset_name(sfid, bank, program)
                if name is not None:
                    self.catalog.append(CatalogEntry(basename, bank, program, name))
                    self._preset_names[(basename, bank, program)] = name
        return True

    @property
    def has_soundfont(self) -> bool:
        return bool(self._fonts)

    def melodic_entries(self) -> list[CatalogEntry]:
        return [e for e in self.catalog if e.bank != DRUM_BANK]

    def drum_kits(self) -> list[CatalogEntry]:
        return [e for e in self.catalog if e.bank == DRUM_BANK]

    def select_program(self, channel: int, soundfont: str, bank: int, program: int) -> CatalogEntry | None:
        """Select a preset, falling back gracefully. Returns what was really selected."""
        if not self._fonts:
            return None
        font = next((f for f in self._fonts if f.basename == soundfont), self._fonts[0])
        fallbacks = ((bank, program), (0 if bank != DRUM_BANK else DRUM_BANK, program),
                     (bank, 0), (0 if bank != DRUM_BANK else DRUM_BANK, 0))
        for try_bank, try_program in fallbacks:
            name = self._preset_names.get((font.basename, try_bank, try_program))
            if name is not None:
                self._fs.program_select(channel, font.sfid, try_bank, try_program)
                return CatalogEntry(font.basename, try_bank, try_program, name)
        return None

    # Realtime events

    def noteon(self, channel: int, note: int, velocity: int) -> None:
        self._fs.noteon(channel, note, velocity)

    def noteoff(self, channel: int, note: int) -> None:
        self._fs.noteoff(channel, note)

    def cc(self, channel: int, control: int, value: int) -> None:
        self._fs.cc(channel, control, value)

    def pitch_bend(self, channel: int, value: int) -> None:
        self._fs.pitch_bend(channel, value)

    def all_notes_off(self, channel: int) -> None:
        self._fs.all_notes_off(channel)

    def all_sounds_off(self, channel: int) -> None:
        self._fs.all_sounds_off(channel)

    def set_cutoff(self, channel: int, value: int) -> None:
        """Apply cutoff 0-127 as a realtime offset; 127 leaves the patch's filter alone."""
        _fluid_set_gen(self._fs.synth, channel, GEN_FILTERFC, -(127 - value) / 127 * FILTER_RANGE_CENTS)

    def patch_info(self, soundfont: str, bank: int, program: int) -> PatchInfo | None:
        """Read a patch's values by playing a note on a private synth with no audio output."""
        if not self._fonts:
            return None
        font = next((f for f in self._fonts if f.basename == soundfont), self._fonts[0])
        key = (font.basename, bank, program)
        if key not in self._patch_info:
            self._patch_info[key] = self._read_patch(font, bank, program)
        return self._patch_info[key]

    def _read_patch(self, font: _LoadedFont, bank: int, program: int) -> PatchInfo | None:
        if self._probe is None:
            self._probe = fluidsynth.Synth(gain=0.0, samplerate=float(self.sample_rate), channels=16)
        if font.basename not in self._probe_fonts:
            self._probe_fonts[font.basename] = self._probe.sfload(font.path)
        probe = self._probe
        probe.program_select(0, self._probe_fonts[font.basename], bank, program)
        for note in PROBE_KEYS:
            probe.noteon(0, note, 100)
            voices = (ctypes.c_void_p * 32)()
            _fluid_get_voicelist(probe.synth, voices, 32, -1)
            voice = next((v for v in voices if v), None)
            info = None
            if voice is not None:
                info = PatchInfo(
                    float(_fluid_voice_gen_get(voice, GEN_VOL_ATTACK)),
                    float(_fluid_voice_gen_get(voice, GEN_VOL_RELEASE)),
                    float(_fluid_voice_gen_get(voice, GEN_MOD_LFO_FREQ)),
                )
            probe.all_sounds_off(0)
            probe.get_samples(64)  # lets the synth free the voice
            if info is not None:
                return info
        return None

    def set_envelope(
        self, channel: int, soundfont: str, bank: int, program: int, attack_ms: int, release_ms: int
    ) -> None:
        """Give new notes these true times. A time of 0 keeps the patch's own."""
        info = self.patch_info(soundfont, bank, program)

        def offset(milliseconds: int, patch_value: float | None) -> float:
            if milliseconds <= 0 or patch_value is None:
                return 0.0
            return 1200 * math.log2(milliseconds / 1000) - patch_value

        _fluid_set_gen(self._fs.synth, channel, GEN_VOL_ATTACK, offset(attack_ms, info.attack if info else None))
        _fluid_set_gen(self._fs.synth, channel, GEN_VOL_RELEASE, offset(release_ms, info.release if info else None))

    def set_rotary(self, channel: int, soundfont: str, bank: int, program: int, on: bool, hz: float) -> None:
        """Drive FluidSynth's per-voice LFO: sample-accurate, with no timing done in Python."""
        synth = self._fs.synth
        info = self.patch_info(soundfont, bank, program)
        base = info.mod_lfo_freq if info else 0.0
        target = 1200 * math.log2(hz / 8.176) - base
        _fluid_set_gen(synth, channel, GEN_MOD_LFO_FREQ, target if on else 0.0)
        _fluid_set_gen(synth, channel, GEN_MOD_LFO_TO_PITCH, ROTARY_PITCH_CENTS if on else 0.0)
        _fluid_set_gen(synth, channel, GEN_MOD_LFO_TO_FILTER, ROTARY_FILTER_CENTS if on else 0.0)
        _fluid_set_gen(synth, channel, GEN_MOD_LFO_TO_VOLUME, ROTARY_VOLUME_CENTIBELS if on else 0.0)

    # Global controls

    def set_gain(self, gain: float) -> None:
        self._fs.setting("synth.gain", float(gain))

    def set_reverb(self, on: bool, room: float, level: float) -> None:
        self._fs.set_reverb_roomsize(float(room))
        self._fs.set_reverb_level(float(level))
        _fluid_reverb_on(self._fs.synth, -1, int(on))

    def set_chorus(self, on: bool) -> None:
        _fluid_chorus_on(self._fs.synth, -1, int(on))

    def active_voices(self) -> int:
        return self._fs.get_active_voice_count()

    def cpu_load(self) -> float:
        return float(_fluid_cpu_load(self._fs.synth))

    def get_samples(self, frames: int):
        """Offline render, for tests. Only valid while no audio driver is running."""
        return self._fs.get_samples(frames)

    # Audio output

    def start_audio(self, candidates: list[AudioDevice]) -> bool:
        """Open the first working device. The synth and its state keep running."""
        self.stop_audio()
        errors = []
        for device in candidates:
            if self._open_driver(device):
                self.audio_device = device
                self.audio_error = ""
                log.info("Audio output: %s", device.label)
                return True
            errors.append(device.label)
        self.audio_error = "Could not open: " + ", ".join(errors) if errors else "No audio devices found"
        return False

    def _open_driver(self, device: AudioDevice) -> bool:
        self._fs.setting("audio.driver", device.driver)
        if device.driver == "pipewire":
            if device.device:
                os.environ["PIPEWIRE_NODE"] = device.device
            else:
                os.environ.pop("PIPEWIRE_NODE", None)
            os.environ["PIPEWIRE_PROPS"] = (
                f"{{ node.name={NODE_NAME} node.description={NODE_NAME} media.name={NODE_NAME} }}"
            )
        elif device.driver == "alsa":
            self._fs.setting("audio.alsa.device", device.device or "default")
        driver = fluidsynth.new_fluid_audio_driver(self._fs.settings, self._fs.synth)
        if not driver:
            return False
        self._driver = driver
        self.audio_generation += 1
        return True

    def stop_audio(self) -> None:
        if self._driver:
            fluidsynth.delete_fluid_audio_driver(self._driver)
        self._driver = None
        self.audio_device = None

    def latency(self) -> LatencyInfo:
        driver = self.audio_device.driver if self.audio_device else "none"
        # PipeWire buffers one period per graph cycle; ALSA queues all periods.
        count = 1 if driver == "pipewire" else self.periods
        ms = self.period_size * count / self.sample_rate * 1000
        return LatencyInfo(driver, self.period_size, self.periods, self.sample_rate, ms)

    def delete(self) -> None:
        self.stop_audio()
        self._fs.delete()
        if self._probe is not None:
            self._probe.delete()
