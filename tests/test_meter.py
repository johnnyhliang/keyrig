import numpy as np
import pytest

from keyrig.meter import (
    FFT_SIZE,
    FRAMES,
    RATE,
    STATE_LIVE,
    STATE_UNAVAILABLE,
    Ballistics,
    MeterFrame,
    analyse,
    column_for_frequency,
    column_frequency,
    parse_output_status,
    parse_volume,
    tap_command,
    tap_environment,
)

pytest.importorskip("fluidsynth")

from keyrig.layers import Layer, Router  # noqa: E402
from keyrig.ui import (  # noqa: E402
    describe_frequency,
    draw_hz_axis,
    draw_meter,
    draw_note_axis,
    draw_spectrum,
    draw_visualizer,
    draw_waterfall,
    meter_fraction,
)

COLUMNS = 96


def sine(hz, left=0.5, right=0.5):
    t = np.arange(FRAMES) / RATE
    wave = np.sin(2 * np.pi * hz * t)
    return np.column_stack((wave * left, wave * right)).astype(np.float32)


def test_tap_can_never_autoconnect_to_an_input():
    command = " ".join(tap_command("x-meter"))
    assert "--target 0" in command
    assert "node.autoconnect=false" in command


def test_tap_does_not_inherit_keyrigs_routing(monkeypatch):
    monkeypatch.setenv("PIPEWIRE_NODE", "some-sink")
    monkeypatch.setenv("PIPEWIRE_PROPS", "{ node.name=keyrig }")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    env = tap_environment()
    assert "PIPEWIRE_NODE" not in env and "PIPEWIRE_PROPS" not in env
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"


def spectrum(*partials, count=COLUMNS):
    """Analyse a sum of (hz, amplitude) sine waves."""
    t = np.arange(FFT_SIZE) / RATE
    wave = sum(amplitude * np.sin(2 * np.pi * hz * t) for hz, amplitude in partials)
    history = np.column_stack((wave, wave)).astype(np.float32)
    return analyse(history[-FRAMES:], history, count)


def level_at(bands, hz):
    return bands[column_for_frequency(hz, len(bands))]


def test_peaks_are_per_channel():
    samples = sine(440, 0.8, 0.1)
    peaks, _, _ = analyse(samples, np.zeros((FFT_SIZE, 2), dtype=np.float32), COLUMNS)
    assert peaks[0] == pytest.approx(0.8, abs=0.01)
    assert peaks[1] == pytest.approx(0.1, abs=0.01)


@pytest.mark.parametrize("hz", [55.0, 110.0, 261.63, 440.0, 1000.0, 4186.0, 12000.0])
def test_tone_appears_in_the_correct_column(hz):
    _, bands, _ = spectrum((hz, 0.5))
    assert abs(int(np.argmax(bands)) - column_for_frequency(hz, COLUMNS)) <= 1


@pytest.mark.parametrize("hz", [55.0, 440.0, 1000.0, 4186.0])
def test_loudest_frequency_is_within_two_cents(hz):
    _, _, dominant = spectrum((hz, 0.5))
    assert abs(1200 * np.log2(dominant / hz)) < 2


def test_response_is_flat_with_no_frequency_emphasis():
    low = max(spectrum((80.0, 0.25))[1])
    middle = max(spectrum((1000.0, 0.25))[1])
    high = max(spectrum((10000.0, 0.25))[1])
    one_db = 1 / 80
    assert low == pytest.approx(middle, abs=one_db)
    assert high == pytest.approx(middle, abs=one_db)


def test_level_is_true_decibels():
    expected = 1 - (20 * np.log10(0.25)) / -80
    assert max(spectrum((1000.0, 0.25))[1]) == pytest.approx(expected, abs=1 / 80)


def test_overtones_of_a_low_note_are_separated():
    root = 65.41  # C2
    _, bands, _ = spectrum((root, 0.5), (root * 2, 0.25), (root * 3, 0.12))
    for between in (root * 1.5, root * 2.5):
        assert level_at(bands, between) < min(level_at(bands, root * 2), level_at(bands, root * 3)) - 20 / 80


def test_quiet_overtone_is_visible_beside_a_loud_note():
    _, bands, _ = spectrum((220.0, 0.8), (880.0, 0.0008))  # 60 dB quieter
    assert level_at(bands, 880.0) > level_at(bands, 600.0) + 10 / 80


def test_silence_reads_zero():
    silence = np.zeros((FFT_SIZE, 2), dtype=np.float32)
    peaks, bands, dominant = analyse(silence[-FRAMES:], silence, COLUMNS)
    assert peaks == (0.0, 0.0)
    assert max(bands) == 0.0
    assert dominant is None


def test_column_mapping_round_trips():
    for column in (0, 37, 95):
        assert column_for_frequency(column_frequency(column, 96), 96) == column


def test_harmonic_markers_are_at_true_columns():
    note = 48  # C3, 130.81 Hz
    row = draw_note_axis(96, note).plain
    for harmonic in (1, 2, 3, 5):
        assert row[column_for_frequency(130.81 * harmonic, 96)] == str(harmonic)


def test_note_axis_shows_octaves_when_idle():
    assert "C4" in draw_note_axis(96, None).plain


def test_hz_axis_label_position():
    row = draw_hz_axis(96).plain
    assert row[column_for_frequency(1000, 96)] == "╵"
    assert "1k" in row and len(row) == 96


def test_describe_frequency():
    assert describe_frequency(440.0) == "440.0 Hz A4 +0¢"
    assert describe_frequency(261.63).startswith("261.6 Hz C4")


def test_waterfall_newest_row_at_bottom():
    lines = draw_waterfall([(0.0,) * 10, (1.0,) * 10], 10, 2)
    assert len(lines) == 2
    assert all(line.plain == "▀" * 10 for line in lines)
    assert lines[-1].spans[0].style.endswith("on rgb(255,255,255)")
    assert lines[0].spans[0].style == "rgb(12,12,24) on rgb(12,12,24)"


def test_caps_fall_after_holding():
    ballistics = Ballistics()
    ballistics.update((0, 0), [0.8], 0.0)
    assert ballistics.update((0, 0), [0.0], 0.3).caps[0] == pytest.approx(0.8)
    assert ballistics.update((0, 0), [0.0], 1.5).caps[0] < 0.8


def test_ballistics_rise_instantly_and_fall_slowly():
    ballistics = Ballistics()
    assert ballistics.update((0.5, 0.5), [0.9], 0.0).peak[0] == 0.5
    frame = ballistics.update((0.0, 0.0), [0.0], 0.05)
    assert 0.2 < frame.peak[0] < 0.5
    assert 0.0 < frame.bands[0] < 0.9


def test_peak_hold_stays_then_falls():
    ballistics = Ballistics()
    ballistics.update((0.5, 0.0), [], 0.0)
    assert ballistics.update((0.0, 0.0), [], 1.0).hold[0] == 0.5
    assert ballistics.update((0.0, 0.0), [], 3.0).hold[0] < 0.5


def test_clip_light_latches_then_clears():
    ballistics = Ballistics()
    assert ballistics.update((1.0, 0.0), [], 0.0).clipped
    assert ballistics.update((0.1, 0.0), [], 1.0).clipped
    assert not ballistics.update((0.1, 0.0), [], 3.0).clipped


def dump(linked_to_sink=True, media_class="Audio/Sink"):
    objects = [
        {"id": 1, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "keyrig-5"}}},
        {"id": 2, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "out", "media.class": media_class}}},
    ]
    if linked_to_sink:
        objects.append({"id": 3, "type": "PipeWire:Interface:Link", "info": {"output-node-id": 1, "input-node-id": 2}})
    return objects


def test_output_status_connected():
    assert parse_output_status(dump(), "keyrig-5") == (True, 2)


def test_output_status_not_linked():
    assert parse_output_status(dump(False), "keyrig-5") == (False, None)


def test_link_to_the_meter_does_not_count_as_connected():
    assert parse_output_status(dump(media_class="Stream/Input/Audio"), "keyrig-5") == (False, None)


def test_output_status_for_missing_node():
    assert parse_output_status(dump(), "keyrig-999") == (False, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Volume: 0.40\n", (0.40, False)), ("Volume: 0.40 [MUTED]\n", (0.40, True)), ("", (None, False))],
)
def test_parse_volume(text, expected):
    assert parse_volume(text) == expected


def test_meter_scale():
    assert meter_fraction(1.0) == 1.0
    assert meter_fraction(0.0) == 0.0
    assert meter_fraction(10 ** (-24 / 20)) == pytest.approx(0.5)


@pytest.mark.parametrize("width", [20, 40, 76, 150])
def test_meter_fills_exact_width(width):
    assert draw_meter("L", 0.5, 0.7, width, True).cell_len == width


def test_spectrum_rows_and_full_height():
    lines = draw_spectrum((1.0,) * 10, (), 20, 3)
    assert len(lines) == 3
    assert all(line.plain == "█" * 20 for line in lines)


def test_spectrum_with_no_data_is_blank():
    assert draw_spectrum((), (), 20, 2)[0].plain.strip() == ""


def test_peak_cap_is_drawn_above_a_lower_bar():
    lines = draw_spectrum((0.0,), (0.9,), 3, 3)
    assert lines[0].plain == "▔▔▔"


@pytest.mark.parametrize("view", ["bars", "waterfall"])
@pytest.mark.parametrize("height", [1, 2, 3, 4, 5, 7, 8, 22])
def test_visualizer_uses_available_rows(height, view):
    frame = MeterFrame(STATE_LIVE, (0.5, 0.4), (0.6, 0.5), (0.5,) * 20)
    lines = draw_visualizer(frame, 76, height, view, [(0.5,) * 76], 60).plain.split("\n")
    assert len(lines) == height
    assert all(len(line) == 76 for line in lines)


def test_visualizer_zero_height_and_unavailable():
    assert draw_visualizer(MeterFrame(), 76, 0).plain == ""
    assert "PipeWire" in draw_visualizer(MeterFrame(STATE_UNAVAILABLE), 76, 5).plain


def test_snapshot_indicators(engine):
    router = Router(engine)
    router.layers[1] = Layer(enabled=True)
    router.sustain(127)
    router.mod_wheel(40)
    router.pitch_bend(-1200)
    router.note_on(0, 60, 100)
    router.note_on(9, 36, 100)
    snapshot = router.snapshot()
    assert (snapshot.sustain, snapshot.mod, snapshot.bend) == (True, 40, -1200)
    assert snapshot.sounding == frozenset({1, 9})
    assert snapshot.lowest_sounded == 60, "drum notes are not melodic roots"
    router.panic()
    snapshot = router.snapshot()
    assert (snapshot.sustain, snapshot.mod, snapshot.bend, snapshot.sounding) == (False, 0, 0, frozenset())
