#!/usr/bin/env python3
"""A virtual MIDI keyboard for testing keyrig with no hardware attached.

Creates a virtual output port, then plays a scripted performance into it:

    python tools/fake_keyboard.py            # run the whole demo once
    python tools/fake_keyboard.py --hold 30  # wait 30 s first (time to pick the port)
    python tools/fake_keyboard.py --knob 21  # only sweep CC21, for learn mode

Then find the port with `aconnect -l`, and select it in keyrig (press m), or run
`keyrig --midi-port keyrig-fake`.
"""

from __future__ import annotations

import argparse
import time

import mido

PORT_NAME = "keyrig-fake"
DRUM_CHANNEL = 9


def sweep(port, control: int, channel: int = 0, seconds: float = 1.0) -> None:
    steps = 32
    for step in range(steps + 1):
        port.send(mido.Message("control_change", channel=channel, control=control, value=step * 127 // steps))
        time.sleep(seconds / steps)


def demo(port) -> None:
    print("chord (C major), with pedal and pitch bend")
    for note in (60, 64, 67):
        port.send(mido.Message("note_on", note=note, velocity=100))
    time.sleep(0.5)
    port.send(mido.Message("control_change", control=64, value=127))
    for note in (60, 64, 67):
        port.send(mido.Message("note_off", note=note))
    port.send(mido.Message("pitchwheel", pitch=4000))
    time.sleep(0.3)
    port.send(mido.Message("pitchwheel", pitch=0))
    port.send(mido.Message("control_change", control=1, value=90))
    time.sleep(0.3)
    port.send(mido.Message("control_change", control=1, value=0))
    port.send(mido.Message("control_change", control=64, value=0))
    time.sleep(0.5)

    print("all 16 pads on channel 10")
    for note in range(36, 52):
        port.send(mido.Message("note_on", channel=DRUM_CHANNEL, note=note, velocity=110))
        time.sleep(0.12)
        port.send(mido.Message("note_off", channel=DRUM_CHANNEL, note=note))

    print("knob sweep on CC21")
    sweep(port, 21)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hold", type=float, default=0, help="seconds to wait before playing")
    parser.add_argument("--after", type=float, default=1, help="seconds to keep the port open afterwards")
    parser.add_argument("--knob", type=int, help="only sweep this CC, repeatedly")
    args = parser.parse_args()

    with mido.open_output(PORT_NAME, virtual=True) as port:
        print(f"Virtual port '{PORT_NAME}' is open. See it with: aconnect -l")
        time.sleep(args.hold)
        try:
            if args.knob is not None:
                while True:
                    sweep(port, args.knob)
            demo(port)
            time.sleep(args.after)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
