"""Renders audio offline through the real FluidSynth; nothing reaches the speakers."""

import pytest

np = pytest.importorskip("numpy")
fluidsynth_module = pytest.importorskip("fluidsynth")

from keyrig.layers import DRUM_SYNTH_CHANNEL, DrumLayer, Instrument, Layer, Router  # noqa: E402
from keyrig.synth_engine import AudioDevice, SynthEngine, find_default_soundfont  # noqa: E402

RATE = 48000
ORGAN_RELEASE_SECONDS = 3.0  # FluidR3's church organ takes about 2.5 s to decay


@pytest.fixture
def synth():
    soundfont = find_default_soundfont()
    if soundfont is None:
        pytest.skip("no General MIDI SoundFont installed")
    engine = SynthEngine(sample_rate=RATE)
    assert engine.load_soundfont(soundfont)
    engine.set_reverb(False, 0.5, 0.5)
    engine.set_chorus(False)
    yield engine
    engine.delete()


def render(engine, seconds):
    return np.asarray(engine.get_samples(int(RATE * seconds)), dtype=float)


def rms(samples):
    return float(np.sqrt(np.mean(samples**2)))


def high_ratio(samples, hz=2000):
    mono = samples[::2]
    spectrum = np.abs(np.fft.rfft(mono))
    freqs = np.fft.rfftfreq(len(mono), 1 / RATE)
    return float(spectrum[freqs > hz].sum() / spectrum.sum())


def router_with(engine, *layers, drum=None):
    router = Router(engine)
    padded = list(layers) + [Layer() for _ in range(4 - len(layers))]
    router.load(padded, drum or DrumLayer())
    return router


def test_catalog_has_gm_programs_and_drum_kits(synth):
    assert len(synth.melodic_entries()) >= 128
    kits = synth.drum_kits()
    assert {0, 8, 16, 25, 40}.issubset({k.program for k in kits})


def test_note_makes_sound_and_release_stops_voices(synth):
    router = router_with(synth, Layer(enabled=True))
    router.note_on(0, 60, 110)
    assert rms(render(synth, 0.2)) > 50
    assert synth.active_voices() > 0
    router.note_off(0, 60)
    render(synth, 2.5)
    assert synth.active_voices() == 0


def test_stacked_layers_use_separate_channels(synth):
    router = router_with(
        synth,
        Layer(enabled=True, instrument=Instrument(program=19)),
        Layer(enabled=True, instrument=Instrument(program=0)),
    )
    assert synth._fs.program_info(0)[2] == 19
    assert synth._fs.program_info(1)[2] == 0
    router.note_on(0, 60, 110)
    render(synth, 0.05)
    assert synth.active_voices() >= 2


def test_drum_channel_uses_bank_128_and_selected_kit(synth):
    kit = next(k for k in synth.drum_kits() if k.program == 25)
    router = router_with(synth, Layer(enabled=True), drum=DrumLayer(kit=25))
    assert synth._fs.program_info(DRUM_SYNTH_CHANNEL)[1:] == (128, 25)
    assert router.drum.name == kit.name
    router.note_on(9, 38, 120)
    assert rms(render(synth, 0.2)) > 50


def test_filter_cutoff_removes_high_frequencies(synth):
    router = router_with(synth, Layer(enabled=True, instrument=Instrument(program=81)))
    router.note_on(0, 60, 110)
    render(synth, 0.05)
    open_ratio = high_ratio(render(synth, 0.3))
    router.note_off(0, 60)
    render(synth, 1.5)

    router.layers[0].cutoff = 20
    router.apply_layer(0)
    router.note_on(0, 60, 110)
    render(synth, 0.05)
    closed_ratio = high_ratio(render(synth, 0.3))
    assert closed_ratio < open_ratio / 3


def test_filter_cutoff_changes_a_sounding_note(synth):
    router = router_with(synth, Layer(enabled=True, instrument=Instrument(program=81)))
    router.note_on(0, 60, 110)
    render(synth, 0.05)
    before = high_ratio(render(synth, 0.2))
    synth.set_cutoff(0, 15)
    render(synth, 0.05)
    assert high_ratio(render(synth, 0.2)) < before / 3


def test_reverb_toggle_changes_tail(synth):
    def tail(on):
        synth.set_reverb(on, 0.9, 1.0)
        router = router_with(synth, Layer(enabled=True))
        router.note_on(0, 60, 120)
        render(synth, 0.3)
        router.note_off(0, 60)
        router.panic()
        render(synth, 0.02)
        return rms(render(synth, 0.3))

    with_reverb = tail(True)
    render(synth, 4)
    without = tail(False)
    assert with_reverb > 10 * max(without, 0.1)


def test_master_gain_scales_output(synth):
    router = router_with(synth, Layer(enabled=True))
    synth.set_gain(0.1)
    router.note_on(0, 60, 110)
    quiet = rms(render(synth, 0.2))
    router.panic()
    render(synth, 0.5)
    synth.set_gain(0.8)
    router.note_on(0, 60, 110)
    assert rms(render(synth, 0.2)) > quiet * 4


def test_panic_cuts_sound_even_with_pedal_down(synth):
    router = router_with(synth, Layer(enabled=True, instrument=Instrument(program=19)))
    router.sustain(127)
    router.note_on(0, 60, 110)
    router.note_off(0, 60)
    render(synth, 0.2)
    assert synth.active_voices() > 0
    router.panic()
    render(synth, 0.1)
    assert synth.active_voices() == 0


def test_toggle_layer_with_pedal_down_really_stops_organ(synth):
    router = router_with(
        synth,
        Layer(enabled=True, instrument=Instrument(program=19)),
    )
    router.sustain(127)
    router.note_on(0, 60, 110)
    render(synth, 0.1)
    router.set_layer_enabled(0, False)
    render(synth, ORGAN_RELEASE_SECONDS)
    assert synth.active_voices() == 0


def test_preset_change_stops_held_organ(synth):
    router = router_with(synth, Layer(enabled=True, instrument=Instrument(program=19)))
    router.note_on(0, 60, 110)
    render(synth, 0.1)
    router.load([Layer() for _ in range(4)], DrumLayer())
    render(synth, ORGAN_RELEASE_SECONDS)
    assert synth.active_voices() == 0


def test_missing_soundfont_program_falls_back(synth):
    selected = synth.select_program(0, "not-loaded.sf2", 77, 200)
    assert selected is not None
    assert selected.bank == 0


def test_soundfont_load_failure_is_reported_not_raised(synth):
    assert synth.load_soundfont("/nonexistent/file.sf2") is False
    assert "not found" in synth.log_messages[-1]


def test_unavailable_audio_device_is_reported_not_fatal(synth):
    missing = AudioDevice("alsa:nope", "Missing", "alsa", "plughw:CARD=KeyrigNoSuchCard,DEV=0")
    assert synth.start_audio([missing]) is False
    assert synth.audio_device is None
    assert "Missing" in synth.audio_error


def test_visualizer_resolves_overtones_of_a_real_patch(synth):
    from keyrig.meter import FFT_SIZE, analyse, column_for_frequency

    router = router_with(synth, Layer(enabled=True, instrument=Instrument(program=81)))  # sawtooth lead
    note = 48  # C3
    root = 440.0 * 2 ** ((note - 69) / 12)
    router.note_on(0, note, 110)
    render(synth, 0.3)
    stereo = render(synth, FFT_SIZE / RATE).reshape(-1, 2).astype(np.float32) / 32768
    count = 120
    _, bands, dominant = analyse(stereo[-2048:], stereo[-FFT_SIZE:], count)
    assert dominant is not None
    assert min(abs(1200 * np.log2(dominant / (root * n))) for n in range(1, 13)) < 20

    def at(hz):
        return bands[column_for_frequency(hz, count)]

    for harmonic in (1, 2, 3, 4):
        assert at(root * harmonic) > at(root * (harmonic + 0.5)) + 15 / 80
