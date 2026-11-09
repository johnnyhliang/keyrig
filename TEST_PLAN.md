# keyrig test plan

## 1. Automated

```sh
.venv/bin/pytest
```

Covers routing, stuck-note cases, port classification, preset and mapping storage, UI keys and
learn mode, plus offline audio rendering through real FluidSynth (bank 128 kits, filter, reverb,
gain, panic with the pedal down). Nothing reaches the speakers.

## 2. Virtual port, no keyboard needed

```sh
.venv/bin/python tools/fake_keyboard.py --hold 30 &   # creates "keyrig-fake"
aconnect -l                                           # confirm it is listed
.venv/bin/keyrig --midi-port keyrig-fake
```

| # | Check | Expected |
| --- | --- | --- |
| 2.1 | Demo plays | Chord on the key strip, pads light in order, activity dot blinks |
| 2.2 | Fake keyboard exits | Status shows "waiting for keyrig-fake…", app keeps running |
| 2.3 | Start it again | Reconnects within about a second |
| 2.4 | `fake_keyboard.py --knob 21`, press `c` | Learn dialog reports CC21; assign it, and the value tracks the knob |
| 2.5 | Inspect `~/.config/keyrig/presets.toml` | Contains a `[[mapping]]` entry |

## 3. With the Launchkey (by ear)

| # | Action | Expected |
| --- | --- | --- |
| 3.1 | Start with `--midi-port` omitted | Connects to `MIDI In`, not `DAW In` |
| 3.2 | `m`, choose `DAW In`, then back to `MIDI In` | Status follows; DAW is flagged red |
| 3.3 | Play keys on "Church Organ + Grand" | Organ with a quieter piano |
| 3.4 | Load the Split preset, play either side of C4 (60) | Organ below, piano from C4 up |
| 3.5 | Hold a chord, press `1` | That layer stops, others continue |
| 3.6 | Hold a chord and the pedal, press `]` | Everything releases, no stuck notes |
| 3.7 | Pedal, mod wheel, pitch wheel | Affect all layers |
| 3.8 | Switch pads to Drum mode, hit them | Drums sound, correct pad lights; key layers silent |
| 3.9 | On row `D`, choose another kit | Pad sound changes |
| 3.10 | Press `c`, turn a knob, assign each target type | Each parameter follows the knob |
| 3.11 | Unplug the Launchkey while holding a note | Note stops, no crash, status shows waiting |
| 3.12 | Replug it | Reconnects by itself and plays |
| 3.13 | `a`, choose each output | Sound moves; a failed choice keeps the previous output |
| 3.14 | Panic (`Space`) with pedal down | Instant silence |

## 4. Audio quality

Run `pw-top` while playing large chords. `ERR` on the `keyrig` row should stay flat. Confirm that
the UI's **graph** value matches `QUANT` in `pw-top`.

Play and watch the Output panel: L/R meters and the spectrum should move with the sound. Run
`pw-link -l | grep -A1 meter`: the meter's inputs must come only from keyrig's outputs, never from
an `alsa_input` device. Mute the output and confirm `Out` shows `MUTED`.

## 5. Headless and systemd

| # | Action | Expected |
| --- | --- | --- |
| 5.1 | `keyrig --headless`, then Ctrl+C | Plays; exits cleanly and saves state |
| 5.2 | Install and enable the unit (see README) | `systemctl --user status keyrig` is active |
| 5.3 | Reboot with linger on | Plays with no login |
| 5.4 | Repeat 3.3, 3.8, 3.11 and 3.12 on the Pi 4 | Same behaviour |
