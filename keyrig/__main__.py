"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .config import load_config
from .presets import PresetStore

MISSING_LIBRARY_HELP = """\
keyrig could not load the FluidSynth library.
Install it and the General MIDI SoundFont:

  sudo dnf install fluidsynth fluidsynth-devel alsa-lib-devel fluid-soundfont-gm
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="keyrig", description=__doc__)
    parser.add_argument("--headless", action="store_true", help="run without the TUI (for systemd or a Pi)")
    parser.add_argument("--list", action="store_true", help="list MIDI ports and audio devices, then exit")
    parser.add_argument("--midi-port", help="use this MIDI port for this run (name or part of it)")
    parser.add_argument("--audio-device", help="use this audio device id for this run (see --list)")
    parser.add_argument("--config-dir", type=Path, help="default: ~/.config/keyrig")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def isolate_stderr(log_path: Path) -> None:
    """Send native-library stderr (ALSA, JACK) to the log so it cannot corrupt the TUI.

    Textual draws on sys.__stderr__, so it is pointed at a private copy of the terminal.
    """
    terminal = os.dup(2)
    sys.__stderr__ = os.fdopen(terminal, "w", buffering=1, encoding="utf-8", errors="replace")
    sys.stderr = sys.__stderr__
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 2)
    os.close(log_fd)


def print_devices() -> None:
    from .midi_input import classify_ports
    from .synth_engine import list_audio_devices

    import rtmidi

    print("MIDI input ports (best first):")
    for port in classify_ports(rtmidi.MidiIn().get_ports()):
        auto = "auto-candidate" if port.score else "manual only"
        print(f"  {port.stable}  [{port.kind}, {auto}]")
    print("\nAudio devices (pass the id to --audio-device):")
    for device in list_audio_devices():
        print(f"  {device.id}  ({device.label})")


def run_headless(rig) -> int:
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    rig.start()
    log = logging.getLogger("keyrig")
    info = rig.engine.latency()
    log.info("Running headless. Preset: %s", rig.preset_name)
    log.info(
        "Audio: %s; configured latency %.1f ms",
        rig.engine.audio_device.label if rig.engine.audio_device else f"NONE ({rig.engine.audio_error})",
        info.configured_ms,
    )
    last_port = object()
    while not stop.wait(1.0):
        port = rig.midi.current
        if port != last_port:
            log.info("MIDI input: %s", port.label if port else "waiting for keyboard")
            last_port = port
    rig.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        from .rig import Rig
        from .synth_engine import SynthEngine
    except ImportError as exc:
        if "FluidSynth" in str(exc) or "fluidsynth" in str(exc):
            print(MISSING_LIBRARY_HELP, file=sys.stderr)
            return 1
        raise

    if args.list:
        print_devices()
        return 0

    config = load_config(args.config_dir)
    handlers: list[logging.Handler] = [logging.FileHandler(config.log_path)]
    if args.headless:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    engine = SynthEngine(
        sample_rate=config.sample_rate,
        period_size=config.period_size,
        periods=config.periods,
        polyphony=config.polyphony,
        cpu_cores=config.cpu_cores,
    )
    rig = Rig(config, engine, PresetStore(config.presets_path), midi_port=args.midi_port, audio_device=args.audio_device)

    if args.headless:
        return run_headless(rig)

    from .realtime import prepare_for_tui
    from .ui import KeyrigApp

    print("keyrig: loading SoundFonts…", flush=True)
    isolate_stderr(config.log_path)
    prepare_for_tui()
    rig.start()
    app = KeyrigApp(rig)
    try:
        app.run()
    finally:
        rig.stop()
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
