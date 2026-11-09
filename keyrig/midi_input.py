"""MIDI port discovery, Launchkey port classification and hot-plug handling."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import mido
import rtmidi

log = logging.getLogger("keyrig.midi")

AUTO = "auto"

KIND_NORMAL = "normal"
KIND_DAW = "DAW/InControl"
KIND_THROUGH = "through"
KIND_VIRTUAL = "virtual"

_ALSA_ADDRESS = re.compile(r"\s+\d+:\d+$")
_DAW_PORT = re.compile(r"\b(DAW|In\s?Control)\b", re.IGNORECASE)
# Older Launchkeys (MK1/MK2) name the InControl port "<model> MIDI 2".
_LAUNCHKEY_SECOND_PORT = re.compile(r"launchkey.*\bMIDI 2$", re.IGNORECASE)
_KNOWN_KEYBOARD = re.compile(
    r"launchkey|keystation|oxygen|mpk|keylab|minilab|microkey|nanokey|komplete|a-\d+|impact|fp-\d+",
    re.IGNORECASE,
)
_HIDDEN = re.compile(r"^PipeWire-|^Midi Through:?$", re.IGNORECASE)

_IGNORED_TYPES = {"clock", "active_sensing", "start", "stop", "continue", "reset", "sysex"}


@dataclass(frozen=True)
class MidiPort:
    name: str  # full rtmidi name, includes the ALSA client:port address
    stable: str  # address removed, so it survives a replug
    kind: str
    score: int  # auto-detect rank; 0 means never auto-selected

    @property
    def label(self) -> str:
        return self.stable.split(":", 1)[-1] if ":" in self.stable else self.stable


def stable_name(name: str) -> str:
    """Strip the ALSA address, which can change when a device is replugged."""
    return _ALSA_ADDRESS.sub("", name).strip()


def classify_port(name: str) -> MidiPort:
    stable = stable_name(name)
    if _DAW_PORT.search(stable) or _LAUNCHKEY_SECOND_PORT.search(stable):
        return MidiPort(name, stable, KIND_DAW, 0)
    if "midi through" in stable.lower():
        return MidiPort(name, stable, KIND_THROUGH, 0)
    if stable.lower().startswith("rtmidi"):
        return MidiPort(name, stable, KIND_VIRTUAL, 10)
    if _KNOWN_KEYBOARD.search(stable):
        return MidiPort(name, stable, KIND_NORMAL, 100)
    return MidiPort(name, stable, KIND_NORMAL, 50)


def classify_ports(names: list[str]) -> list[MidiPort]:
    """Classify port names, best auto-detect candidate first."""
    ports = [classify_port(n) for n in names if not _HIDDEN.match(stable_name(n))]
    return sorted(ports, key=lambda p: (-p.score, p.stable))


def choose_port(ports: list[MidiPort], preference: str) -> MidiPort | None:
    """Pick the port to use. An explicit choice never falls back to another device."""
    if preference == AUTO:
        best = ports[0] if ports else None
        return best if best is not None and best.score > 0 else None
    for port in ports:
        if port.stable == preference:
            return port
    needle = preference.lower()
    for port in ports:
        if needle in port.stable.lower():
            return port
    return None


class MidiInput:
    """Keeps the best/selected input port open, surviving unplug and replug."""

    def __init__(
        self,
        on_message: Callable[[mido.Message, float], None],
        on_disconnect: Callable[[], None],
        preference: str = AUTO,
        poll_seconds: float = 0.5,
    ) -> None:
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._preference = preference
        self._poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._probe: rtmidi.MidiIn | None = None
        self._port = None
        self._current: MidiPort | None = None
        self._ports: list[MidiPort] = []
        self.error = ""

    @property
    def preference(self) -> str:
        return self._preference

    @property
    def current(self) -> MidiPort | None:
        return self._current

    @property
    def ports(self) -> list[MidiPort]:
        return list(self._ports)

    def start(self) -> None:
        self._tick()
        self._thread = threading.Thread(target=self._run, name="keyrig-midi", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._close(notify=False)

    def select(self, preference: str) -> None:
        self._preference = preference
        self._wake.set()

    def _run(self) -> None:
        while not self._stopping:
            self._wake.wait(self._poll_seconds)
            self._wake.clear()
            if self._stopping:
                return
            try:
                self._tick()
            except Exception:
                log.exception("MIDI poll failed")

    def _scan(self) -> list[MidiPort]:
        try:
            if self._probe is None:
                self._probe = rtmidi.MidiIn()
            return classify_ports(self._probe.get_ports())
        except Exception as exc:  # no sequencer yet, e.g. very early at boot
            self._probe = None
            self.error = f"MIDI system unavailable: {exc}"
            return []

    def _tick(self) -> None:
        self._ports = self._scan()
        wanted = choose_port(self._ports, self._preference)
        # Comparing full names catches a replug that landed on a new ALSA address.
        if self._current is not None and (wanted is None or wanted.name != self._current.name):
            log.info("Closing MIDI port %s", self._current.name)
            self._close(notify=True)
        if wanted is not None and self._current is None:
            self._open(wanted)

    def _open(self, port: MidiPort) -> None:
        try:
            self._port = mido.open_input(port.name, callback=self._callback)
        except Exception as exc:  # the device can vanish between scan and open
            self._port = None
            self.error = f"Could not open {port.label}: {exc}"
            log.warning(self.error)
            return
        self._current = port
        self.error = ""
        log.info("Opened MIDI port %s", port.name)

    def _close(self, notify: bool) -> None:
        port, self._port = self._port, None
        self._current = None
        if port is not None:
            try:
                port.close()
            except Exception:
                log.exception("Error closing MIDI port")
        # Runs after close and without any lock held, so a callback still in
        # flight cannot deadlock against the rtmidi thread join inside close().
        if notify:
            self._on_disconnect()

    def _callback(self, message: mido.Message) -> None:
        received = time.perf_counter()
        if message.type in _IGNORED_TYPES:
            return
        try:
            self._on_message(message, received)
        except Exception:
            log.exception("Error handling %s", message)
