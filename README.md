# keyrig

A terminal instrument rig for a USB MIDI keyboard. It needs no DAW: FluidSynth runs inside the
process, with up to 4 key layers, a drum layer, presets, and knob learn. Runs as a TUI on Fedora
(PipeWire), or headless under systemd, for example on a Raspberry Pi 4.

## Install (Fedora)

```sh
sudo dnf install fluidsynth fluidsynth-devel alsa-lib-devel fluid-soundfont-gm

cd ~/Documents/soundchain
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
ln -s "$PWD/.venv/bin/keyrig" ~/.local/bin/keyrig   # makes `keyrig` a normal command
```

## Run

```sh
keyrig              # the TUI
keyrig --list       # show MIDI ports and audio devices
keyrig --headless   # no UI; uses the ports chosen earlier in the TUI
```

`--midi-port NAME` and `--audio-device ID` override the saved choice for one run.

If only one instance should be playing, stop the service before opening the TUI
(`systemctl --user stop keyrig`). Two running instances both respond to the keyboard.

## Keys

| Key | Action |
| --- | --- |
| `↑` `↓` or `k` `j` | Select row (layers 1-4, drums, global effects) |
| `Tab` / `Shift+Tab` | Select column |
| `←` `→` or `h` `l` | Adjust the selected parameter |
| `Shift+←/→` or `H` `L` | Adjust in coarse steps |
| `Enter` | Open the instrument or kit picker; toggle on/off cells |
| `1`-`4` | Toggle layer |
| `d` | Toggle drum layer |
| `[` `]` | Previous / next preset |
| `P` | Preset list |
| `s` | Save preset |
| `m` / `a` | Choose MIDI input / audio output |
| `c` | Learn a knob |
| `r` | Rotary speed: slow or fast |
| `v` | Visualizer: bars or waterfall |
| `f` | Freeze the visualizer |
| `Space` | Panic (all notes off) |
| `q` | Quit |

Mouse: click a cell to select it, click it again to activate it, and scroll over a cell to adjust.

## How the sound is routed

- Notes from every channel except the drum channel fan out to each enabled layer whose
  low/high range contains the key. Layer N plays on FluidSynth channel N-1.
- Notes from channel 10 (`drum_channel`) go only to the drum layer, which uses bank 128 on
  FluidSynth channel 10. Select the kit on the `D` row.
- Sustain (CC64), mod wheel (CC1) and pitch bend go to all key layers.
- Toggling a layer, changing preset, or losing the keyboard all release sounding notes, including
  notes held by the sustain pedal.

## Shaping the sound

Per layer, saved in presets:

| Column | Meaning |
| --- | --- |
| `Atk` | Fade-in time. `auto` keeps the patch's own; times are true milliseconds or seconds |
| `Rel` | Fade-out time after release, also true time |
| `Ped` | Whether this layer responds to the sustain pedal |
| `Rot` | Rotary effect on this layer |

On the bottom row, not saved per preset:

- **Transpose**: ±12 semitones for key layers. Drums are unaffected, and key ranges still refer to
  the physical keys.
- **Keys / Pads**: separate velocity curves. `Light` and `Lighter` make soft playing louder;
  `Heavy` needs a firmer touch.
- **Rotary**: slow or fast, with a gradual change of speed like a real rotor. Press `r`, or assign
  a control in learn mode. The effect uses FluidSynth's LFO for tremolo, a small pitch wobble and
  filter movement; it approximates a Leslie but has no true stereo rotation.

**Smooth preset changes:** notes you are holding keep their old sound until you let go, while new
notes use the new preset. Set `smooth_preset_changes = false` in `config.toml` to release them
immediately. Panic always stops everything.

## Visualizer and indicators

The **Output** panel at the bottom grows with the terminal height:

- **L / R meters**: live level in dB, a marker holding the recent peak, and a `CLIP` light that
  stays lit for 2 seconds. If it lights, lower master volume or `max_gain`.
- **Spectrum**: 27.5 Hz to 20 kHz on a true logarithmic axis, one band per terminal column, with
  about 6 Hz of resolution and no emphasis on any frequency. Bars are real dB down to -80.
  White caps mark recent peaks.
- **Overtone markers**: while you hold a key, the row under the spectrum shows `1 2 3...` where
  each harmonic of the lowest sounding note belongs. When idle, it shows octave positions.
- **Title**: the loudest frequency right now, with its note and tuning offset in cents. With
  several notes playing, this is the loudest partial, not necessarily the note you played.
- Press `v` to switch between bars and a scrolling waterfall, and `f` to freeze the display.

Other indicators:

| Where | Shows |
| --- | --- |
| Top line | `SUS` pedal, `MOD` wheel, `BEND` amount |
| Layer number turns orange | That layer is playing a held key |
| `Out` in Activity | `connected` and the system volume for that output, or `MUTED` / `NOT CONNECTED` |
| `Voices` | Notes sounding against the polyphony limit |
| `Dropouts` | Audio xruns counted by PipeWire; if rising, raise `period_size` |

The meters read keyrig's own audio before the system volume, so check `Out` for mute and volume.
They need a PipeWire output and stay blank on ALSA direct. On terminals under 30 rows, the help
text is hidden to make room. The meter runs only in the TUI, not in headless mode.

## Launchkey

- The Launchkey exposes two ports. Auto-detect uses `MIDI In` and never picks `DAW In`
  (or `InControl` on older models). Press `m` to choose a port by hand.
- The pad grid displays 16 consecutive notes from `pad_base_note` (36), bottom-left first.
  Change `pad_base_note` in `config.toml` if your pads send a different range.
- If a knob does nothing in learn mode, it is probably in a mode that talks only to the DAW port.
  Change the knob mode on the keyboard (a Custom mode is the usual choice), or select the DAW port.

## Learn mode

Press `c`, move a knob, then choose a parameter and press Enter: layer volume, master volume,
reverb level, filter cutoff, or octave shift. Layer parameters can target one layer or all four.

Mappings are stored in `presets.toml` at the top level of the file, so a knob keeps its job when
you switch presets. In the learn dialog, `x` removes the highlighted mapping. A mapped CC replaces
that CC's normal function. The sustain pedal is ignored while learning.

## Files

All in `~/.config/keyrig/`:

| File | Purpose |
| --- | --- |
| `config.toml` | Your settings, with comments. keyrig never rewrites it. Restart to apply. |
| `presets.toml` | Presets and knob mappings. Created with the four starter presets. |
| `state.toml` | Last-used ports, master volume, preset. |
| `keyrig.log` | Log, including native ALSA/FluidSynth messages. |

Add SoundFonts with `extra_soundfonts = ["~/sf2/piano.sf2"]`; their presets appear in the
instrument picker. Set `program_change_presets = true` to make incoming program change N load
preset N+1.

## Latency tuning

The UI shows three numbers:

- **cfg**: what keyrig asked for, from `period_size`, `periods` and `sample_rate`.
- **graph**: the quantum PipeWire is really running at, read from `pw-top`. This is the
  number to trust. It only appears when using PipeWire.
- **MIDI->synth**: measured time from receiving a note to FluidSynth accepting it.

These do not include the audio interface's own converter and USB delay.

Set these in `config.toml`, then restart:

| `period_size` | Latency at 48 kHz (PipeWire) | Use |
| --- | --- | --- |
| 64 | 1.3 ms | Fast machine, nothing else playing |
| 128 (default) | 2.7 ms | Good starting point |
| 256 | 5.3 ms | Older machines, or a Pi 4 on PipeWire |
| 512 | 10.7 ms | If you still hear crackles |

- **PipeWire** uses `period_size` only. Run `pw-top`: a rising `ERR` value means xruns, so raise
  `period_size`. A small quantum applies to the whole audio graph while keyrig runs.
- **ALSA direct** latency is `period_size × periods`. Keep `periods` at 2 and raise it to 3 or 4 if
  it crackles. ALSA direct generally needs the device to be free of PipeWire.
- Keep `sample_rate` equal to the device rate (48000 here) to avoid resampling.
- Lowering `polyphony` reduces CPU load when large chords cause dropouts.

## Auto-start with systemd

```sh
mkdir -p ~/.config/systemd/user
cp packaging/keyrig.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now keyrig.service
loginctl enable-linger "$USER"     # start at boot, without needing a login
journalctl --user -u keyrig -f     # watch the log
```

Pick your ports once in the TUI first; headless mode reuses them. If the project is not in
`~/Documents/soundchain`, edit `ExecStart` in the unit.

## Raspberry Pi 4 notes

Not yet tested on a Pi. Expected setup on Raspberry Pi OS:

```sh
sudo apt install libfluidsynth3 fluid-soundfont-gm libasound2-dev build-essential python3-venv
```

keyrig needs Python 3.12 or newer (`uv python install 3.12` if your OS ships an older one). Start
with `polyphony = 64`, `period_size = 256`. Without PipeWire, keyrig falls back to ALSA.

## Testing without a keyboard

See [TEST_PLAN.md](TEST_PLAN.md). In short:

```sh
.venv/bin/python tools/fake_keyboard.py --hold 30 &
aconnect -l                      # look for keyrig-fake
.venv/bin/keyrig --midi-port keyrig-fake
```

## Code layout

| Module | Responsibility |
| --- | --- |
| `keyrig/midi_input.py` | Port discovery, Launchkey port classification, hot-plug |
| `keyrig/synth_engine.py` | FluidSynth, SoundFonts, audio devices, latency |
| `keyrig/layers.py` | Layer model and note routing |
| `keyrig/presets.py` | Presets, mappings, starter presets, TOML storage |
| `keyrig/rig.py` | Connects the above; used by both the TUI and headless mode |
| `keyrig/ui.py` | Textual interface |
