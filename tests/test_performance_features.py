"""Attack/release, transpose, velocity curves, per-layer pedal, smooth changes, rotary."""

import numpy as np
import pytest

pytest.importorskip("fluidsynth")

from keyrig.layers import (  # noqa: E402
    ROTARY_FAST_HZ,
    ROTARY_SLOW_HZ,
    DrumLayer,
    Instrument,
    Layer,
    Router,
    apply_velocity_curve,
)
from keyrig.presets import PresetStore  # noqa: E402
from keyrig.synth_engine import SynthEngine, find_default_soundfont  # noqa: E402
from keyrig.ui import format_milliseconds, step_envelope  # noqa: E402

RATE = 48000


def four(*layers):
    return list(layers) + [Layer() for _ in range(4 - len(layers))]


def router_with(engine, *layers):
    router = Router(engine)
    for index, layer in enumerate(layers):
        router.layers[index] = layer
    return router


def test_transpose_applies_to_keys_but_not_drums(engine):
    router = router_with(engine, Layer(enabled=True, octave=1))
    router.transpose = 2
    router.note_on(0, 60, 100)
    router.note_on(9, 38, 100)
    assert engine.sounding == {(0, 74), (9, 38)}


def test_key_range_uses_the_physical_key_not_the_transposed_note(engine):
    router = router_with(engine, Layer(enabled=True, low=60, high=60))
    router.transpose = 5
    router.note_on(0, 60, 100)
    assert engine.sounding == {(0, 65)}


def test_transpose_change_while_held_cannot_strand_a_note(engine):
    router = router_with(engine, Layer(enabled=True))
    router.note_on(0, 60, 100)
    router.transpose = 3
    router.note_off(0, 60)
    assert engine.sounding == set()


@pytest.mark.parametrize(
    ("curve", "velocity", "check"),
    [
        ("Linear", 40, lambda v: v == 40),
        ("Light", 40, lambda v: v > 40),
        ("Lighter", 40, lambda v: v > apply_velocity_curve(40, "Light")),
        ("Heavy", 40, lambda v: v < 40),
        ("Lighter", 127, lambda v: v == 127),
        ("Heavy", 1, lambda v: v == 1),
    ],
)
def test_velocity_curves(curve, velocity, check):
    assert check(apply_velocity_curve(velocity, curve))


def test_keys_and_pads_use_separate_curves(engine):
    router = router_with(engine, Layer(enabled=True))
    router.key_curve = "Heavy"
    router.pad_curve = "Lighter"
    router.note_on(0, 60, 40)
    router.note_on(9, 38, 40)
    notes = {call[1]: call[3] for call in engine.names("noteon")}
    assert notes[0] < 40 < notes[9]


def test_pedal_can_be_excluded_per_layer(engine):
    router = router_with(engine, Layer(enabled=True, pedal=True), Layer(enabled=True, pedal=False))
    router.sustain(127)
    router.note_on(0, 60, 100)
    router.note_off(0, 60)
    assert (0, 60) in engine.sounding
    assert (1, 60) not in engine.sounding


def test_turning_a_layers_pedal_off_releases_its_sustained_notes(engine):
    router = router_with(engine, Layer(enabled=True))
    router.sustain(127)
    router.note_on(0, 60, 100)
    router.note_off(0, 60)
    router.set_layer_pedal(0, False)
    assert engine.sounding == set()


def test_toggling_a_no_pedal_layer_does_not_reengage_the_pedal(engine):
    router = router_with(engine, Layer(enabled=True, pedal=False))
    router.sustain(127)
    router.set_layer_enabled(0, True)
    assert engine.pedal[0] == 0


def test_smooth_change_lets_a_held_note_ring_then_releases_on_key_up(engine):
    router = router_with(engine, Layer(enabled=True))
    router.note_on(0, 60, 100)
    router.load(four(Layer(enabled=True, instrument=Instrument(program=19))), DrumLayer(), smooth=True)
    assert engine.sounding == {(0, 60)}, "old note still ringing"
    assert router.channel(0) == 4
    router.note_on(0, 64, 100)
    assert (4, 64) in engine.sounding, "new notes use the new sound"
    router.note_off(0, 60)
    router.note_off(0, 64)
    assert engine.sounding == set()


def test_smooth_change_tail_follows_the_pedal(engine):
    router = router_with(engine, Layer(enabled=True))
    router.sustain(127)
    router.note_on(0, 60, 100)
    router.note_off(0, 60)
    router.load(four(Layer(enabled=True)), DrumLayer(), smooth=True)
    assert (0, 60) in engine.sounding
    router.sustain(0)
    assert engine.sounding == set()


def test_second_change_clears_the_bank_it_reuses(engine):
    router = router_with(engine, Layer(enabled=True))
    router.note_on(0, 60, 100)
    router.load(four(Layer(enabled=True)), DrumLayer(), smooth=True)
    router.load(four(Layer(enabled=True)), DrumLayer(), smooth=True)
    assert router.channel(0) == 0
    assert (0, 60) not in engine.sounding
    router.note_off(0, 60)
    assert engine.sounding == set()


def test_new_bank_inherits_pedal_and_wheels(engine):
    router = router_with(engine, Layer(enabled=True))
    router.sustain(127)
    router.mod_wheel(50)
    router.pitch_bend(500)
    router.load(four(Layer(enabled=True)), DrumLayer(), smooth=True)
    assert engine.pedal[4] == 127
    assert ("cc", 4, 1, 50) in engine.calls
    assert ("bend", 4, 500) in engine.calls


def test_panic_clears_both_banks(engine):
    router = router_with(engine, Layer(enabled=True))
    router.note_on(0, 60, 100)
    router.load(four(Layer(enabled=True)), DrumLayer(), smooth=True)
    router.note_on(0, 64, 100)
    router.panic()
    assert engine.sounding == set()


def test_layer_lights_report_layer_numbers_after_a_bank_change(engine):
    router = router_with(engine, Layer(enabled=True))
    router.load(four(Layer(), Layer(enabled=True)), DrumLayer(), smooth=True)
    router.note_on(0, 60, 100)
    assert router.snapshot().sounding == frozenset({1})


def test_rotary_ramps_gradually_and_reaches_target(engine):
    router = router_with(engine, Layer(enabled=True, rotary=True))
    router.set_rotary_fast(True)
    router.step_rotary(0.1)
    assert ROTARY_SLOW_HZ < router.rotary_hz < ROTARY_FAST_HZ
    for _ in range(200):
        router.step_rotary(0.1)
    assert router.rotary_hz == ROTARY_FAST_HZ


def test_rotary_only_updates_layers_that_use_it(engine):
    router = router_with(engine, Layer(enabled=True, rotary=False), Layer(enabled=True, rotary=True))
    router.set_rotary_fast(True)
    router.step_rotary(0.1)
    assert {call[1] for call in engine.names("rotary")} == {1}


def test_new_fields_round_trip_and_old_files_get_defaults(tmp_path):
    path = tmp_path / "p.toml"
    store = PresetStore(path)
    store.load()
    store.presets[0].layers[0].attack = 500
    store.presets[0].layers[0].release = 2000
    store.presets[0].layers[0].pedal = False
    store.presets[0].layers[0].rotary = True
    store.save()
    again = PresetStore(path)
    again.load()
    assert again.presets == store.presets

    old = tmp_path / "old.toml"
    old.write_text('[[preset]]\nname="x"\n[[preset.layer]]\nvolume=90\n')
    legacy = PresetStore(old)
    legacy.load()
    layer = legacy.presets[0].layers[0]
    assert (layer.attack, layer.release, layer.pedal, layer.rotary) == (0, 0, True, False)


def test_envelope_ladder_and_labels():
    assert step_envelope(0, 1, False) == 5
    assert step_envelope(0, -1, False) == 0
    assert step_envelope(500, 1, True) == 1500
    assert format_milliseconds(0) == "auto"
    assert format_milliseconds(500) == "500"
    assert format_milliseconds(1500) == "1.5s"


# Real FluidSynth, rendered offline


@pytest.fixture
def synth():
    soundfont = find_default_soundfont()
    if soundfont is None:
        pytest.skip("no General MIDI SoundFont installed")
    instance = SynthEngine(sample_rate=RATE)
    assert instance.load_soundfont(soundfont)
    instance.set_reverb(False, 0.5, 0.5)
    instance.set_chorus(False)
    yield instance
    instance.delete()


def render(engine, seconds):
    return np.asarray(engine.get_samples(int(RATE * seconds)), dtype=float)[::2]


def loaded(engine, *layers):
    router = Router(engine)
    router.load(four(*layers), DrumLayer())
    return router


def time_to_half(engine, program, attack):
    """Seconds to half of peak level. Pads keep swelling after the attack, so 90% would mislead."""
    router = loaded(engine, Layer(enabled=True, instrument=Instrument(program=program), attack=attack))
    router.note_on(0, 60, 110)
    samples = render(engine, 5.0)
    router.panic()
    render(engine, 1.0)
    window = 480
    envelope = np.convolve(np.abs(samples), np.ones(window) / window, "same")
    return int(np.argmax(envelope >= 0.5 * envelope.max())) / RATE


@pytest.mark.parametrize("program", [16, 89])  # drawbar is about 1 ms, the warm pad 500 ms
def test_longer_attack_settings_fade_in_longer_on_any_patch(synth, program):
    short, medium, long = (time_to_half(synth, program, attack) for attack in (100, 1000, 3000))
    assert short * 1.5 < medium
    assert medium * 1.5 < long


def test_attack_time_equal_to_the_patch_changes_nothing(synth):
    assert time_to_half(synth, 89, 500) == pytest.approx(time_to_half(synth, 89, 0), abs=0.02)


def test_auto_attack_leaves_a_fast_patch_fast(synth):
    assert time_to_half(synth, 16, 0) < 0.1


def test_release_sets_how_long_a_note_rings(synth):
    def ring_time(release):
        router = loaded(synth, Layer(enabled=True, instrument=Instrument(program=16), release=release))
        router.note_on(0, 60, 110)
        render(synth, 0.2)
        router.note_off(0, 60)
        for step in range(1, 200):
            render(synth, 0.05)
            if synth.active_voices() == 0:
                return step * 0.05
        return 10.0

    assert ring_time(100) < 0.5
    assert 1.0 < ring_time(2000) < 3.5


def modulation_frequency(samples):
    window = 480
    envelope = np.convolve(np.abs(samples), np.ones(window) / window, "same")[2000:-2000][::48]
    envelope = (envelope - envelope.mean()) * np.hanning(len(envelope))
    spectrum = np.abs(np.fft.rfft(envelope))
    frequencies = np.fft.rfftfreq(len(envelope), 48 / RATE)
    useful = (frequencies > 0.3) & (frequencies < 12)
    return float(frequencies[useful][spectrum[useful].argmax()])


@pytest.mark.parametrize("hz", [ROTARY_SLOW_HZ, ROTARY_FAST_HZ])
def test_rotary_modulates_at_the_chosen_speed(synth, hz):
    router = loaded(synth, Layer(enabled=True, instrument=Instrument(program=16), rotary=True))
    router.rotary_hz = router.rotary_target_hz = hz
    router.apply_layer(0)
    router.note_on(0, 60, 110)
    render(synth, 0.2)
    assert modulation_frequency(render(synth, 5.0)) == pytest.approx(hz, abs=0.3)


def test_rotary_off_removes_the_modulation(synth):
    router = loaded(synth, Layer(enabled=True, instrument=Instrument(program=16), rotary=True))
    router.rotary_hz = ROTARY_FAST_HZ
    router.apply_layer(0)
    router.set_layer_rotary(0, False)
    router.note_on(0, 60, 110)
    render(synth, 0.2)
    assert abs(modulation_frequency(render(synth, 5.0)) - ROTARY_FAST_HZ) > 1


def test_smooth_change_keeps_old_patch_sounding_with_real_synth(synth):
    router = loaded(synth, Layer(enabled=True, instrument=Instrument(program=19)))
    router.note_on(0, 60, 110)
    render(synth, 0.1)
    router.load(four(Layer(enabled=True, instrument=Instrument(program=0))), DrumLayer(), smooth=True)
    assert np.sqrt(np.mean(render(synth, 0.5) ** 2)) > 100
    assert synth.active_voices() > 0
    router.note_off(0, 60)
    render(synth, 3.0)
    assert synth.active_voices() == 0
