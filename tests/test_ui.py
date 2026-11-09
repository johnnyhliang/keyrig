"""Drives the real Textual app with synthetic key presses; no audio or MIDI devices."""

import copy

import pytest

pytest.importorskip("fluidsynth")

from textual.widgets import Input, OptionList  # noqa: E402

from keyrig.config import Config  # noqa: E402
from keyrig.midi_input import MidiPort  # noqa: E402
from keyrig.presets import PresetStore  # noqa: E402
from keyrig.rig import Rig  # noqa: E402
from keyrig.synth_engine import SynthEngine, find_default_soundfont  # noqa: E402
from keyrig.ui import KeyrigApp, LearnScreen, MainScreen, PickerScreen, SavePresetScreen  # noqa: E402

import mido  # noqa: E402


@pytest.fixture
def rig(tmp_path, monkeypatch):
    if find_default_soundfont() is None:
        pytest.skip("no General MIDI SoundFont installed")
    monkeypatch.setattr("keyrig.rig.resolve_audio_device", lambda _id: [])
    config = Config(config_dir=tmp_path)
    engine = SynthEngine()
    instance = Rig(config, engine, PresetStore(config.presets_path))
    monkeypatch.setattr(instance.midi, "start", lambda: None)
    monkeypatch.setattr(instance.midi, "stop", lambda: None)
    instance.start()
    yield instance
    instance._stopping.set()
    engine.delete()


def play(rig, *args, **kwargs):
    rig._on_midi(mido.Message(*args, **kwargs), 0.0)


async def press(pilot, *keys):
    await pilot.press(*keys)
    await pilot.pause()


async def test_number_keys_toggle_layers(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        assert rig.layers[2].enabled is False
        await press(pilot, "3")
        assert rig.layers[2].enabled is True
        await press(pilot, "1")
        assert rig.layers[0].enabled is False


async def test_brackets_cycle_presets_and_wrap(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        await press(pilot, "right_square_bracket")
        assert rig.preset_name == "Piano Only"
        await press(pilot, "left_square_bracket", "left_square_bracket")
        assert rig.preset_name == "Electric Piano"


async def test_arrows_and_hjkl_adjust_selected_parameter(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        start = rig.layers[0].volume
        await press(pilot, "tab", "left")
        assert rig.layers[0].volume == start - 3
        await press(pilot, "l")
        assert rig.layers[0].volume == start
        await press(pilot, "H")
        assert rig.layers[0].volume == start - 12
        await press(pilot, "j", "h")
        assert rig.layers[1].volume == 65


async def test_low_cannot_cross_high(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        rig.set_layer_value(0, "high", 10)
        await press(pilot, "tab", "tab", "tab", *["L"] * 3)
        assert rig.layers[0].low == 10


async def test_panic_key_clears_held_notes(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        play(rig, "note_on", note=60, velocity=100)
        assert rig.router.snapshot().held == [60]
        await press(pilot, "space")
        assert rig.router.snapshot().held == []
        rig.engine.get_samples(4096)
        assert rig.engine.active_voices() == 0


async def test_instrument_picker_search_and_select(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        await press(pilot, "j", "enter")
        assert isinstance(pilot.app.screen, PickerScreen)
        await press(pilot, *"church")
        assert [item[1].name for item in pilot.app.screen.shown][0] == "Church Organ"
        await press(pilot, "enter")
        assert isinstance(pilot.app.screen, MainScreen)
        assert rig.layers[1].instrument.program == 19


async def test_gm_name_finds_differently_named_preset(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        await press(pilot, "enter", *"acoustic grand")
        assert pilot.app.screen.shown[0][1].program == 0


async def test_picker_arrows_then_escape_changes_nothing(rig):
    before = copy.deepcopy(rig.layers[0].instrument)
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        await press(pilot, "enter", "down", "down", "escape")
        assert isinstance(pilot.app.screen, MainScreen)
        assert rig.layers[0].instrument == before


async def test_letter_keys_in_picker_filter_are_typed_not_shortcuts(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        await press(pilot, "enter", "q", "1", "space")
        assert pilot.app.is_running
        assert pilot.app.screen.query_one(Input).value == "q1 "
        assert rig.layers[0].enabled is True


async def test_drum_kit_picker_lists_only_bank_128(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        await press(pilot, *"jjjj", "enter")
        assert isinstance(pilot.app.screen, PickerScreen)
        entries = [item[1] for item in pilot.app.screen.shown]
        assert entries and all(entry.bank == 128 for entry in entries)
        await press(pilot, *"808", "enter")
        assert rig.drum.kit == 25
        assert rig.engine._fs.program_info(9)[1:] == (128, 25)


async def test_midi_picker_shows_both_launchkey_ports(rig):
    rig.midi._ports = [
        MidiPort("a 20:0", "LK:LK MIDI In", "normal", 100),
        MidiPort("a 20:1", "LK:LK DAW In", "DAW/InControl", 0),
    ]
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        await press(pilot, "m")
        labels = [item[0] for item in pilot.app.screen.shown]
        assert len(labels) == 3 and "DAW/InControl" in labels[2]
        await press(pilot, "down", "down", "enter")
        assert rig.midi.preference == "LK:LK DAW In"
        assert rig.config.midi_port == "LK:LK DAW In"


async def test_save_preset_persists(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        rig.set_layer_value(0, "volume", 33)
        await press(pilot, "s")
        assert isinstance(pilot.app.screen, SavePresetScreen)
        pilot.app.screen.query_one(Input).value = "Mine"
        await press(pilot, "enter")
    reloaded = PresetStore(rig.config.presets_path)
    reloaded.load()
    assert reloaded.presets[-1].name == "Mine"
    assert reloaded.presets[-1].layers[0].volume == 33
    assert len(reloaded.presets) == 5


async def test_learn_assigns_knob_and_saves_in_preset_file(rig):
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        await press(pilot, "c")
        assert isinstance(pilot.app.screen, LearnScreen)
        play(rig, "control_change", channel=0, control=64, value=127)
        assert rig.learned is None, "a held pedal must not be learned"
        play(rig, "control_change", channel=0, control=21, value=5)
        await pilot.pause(0.15)
        assert pilot.app.screen.assigning
        assert rig.layers[0].volume != 5, "the learning move itself changes nothing"
        options = pilot.app.screen.query_one(OptionList)
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        target_row = labels.index("Layer volume: layer 2")
        await press(pilot, *["down"] * target_row, "enter")
        assert isinstance(pilot.app.screen, MainScreen)

        play(rig, "control_change", channel=0, control=21, value=42)
        assert rig.layers[1].volume == 42

    reloaded = PresetStore(rig.config.presets_path)
    reloaded.load()
    assert [(m.channel, m.cc, m.target, m.layer) for m in reloaded.mappings] == [(0, 21, "layer_volume", 2)]


async def test_learn_can_remove_mapping_and_escape_cancels(rig):
    from keyrig.presets import CCMapping

    rig.store.set_mapping(CCMapping(0, 21, "master_volume"))
    rig._rebuild_mapping_index()
    async with KeyrigApp(rig).run_test() as pilot:
        await pilot.pause()
        await press(pilot, "c", "x")
        assert rig.store.mappings == []
        await press(pilot, "escape")
        assert rig.learning is False


@pytest.mark.parametrize(
    ("target", "layer", "value", "check"),
    [
        ("master_volume", 0, 64, lambda r: r.master_volume == 64),
        ("reverb_level", 0, 127, lambda r: r.fx.reverb_level == 1.0),
        ("filter_cutoff", 1, 40, lambda r: r.layers[0].cutoff == 40),
        ("filter_cutoff", 0, 40, lambda r: all(l.cutoff == 40 for l in r.layers)),
        ("octave_shift", 1, 127, lambda r: r.layers[0].octave == 3),
        ("octave_shift", 1, 0, lambda r: r.layers[0].octave == -3),
        ("octave_shift", 1, 64, lambda r: r.layers[0].octave == 0),
    ],
)
def test_every_learn_target(rig, target, layer, value, check):
    from keyrig.presets import CCMapping

    rig.store.set_mapping(CCMapping(0, 30, target, layer))
    rig._rebuild_mapping_index()
    play(rig, "control_change", channel=0, control=30, value=value)
    assert check(rig)


def test_mapped_cc_on_other_channel_is_not_triggered(rig):
    from keyrig.presets import CCMapping

    rig.store.set_mapping(CCMapping(0, 30, "master_volume"))
    rig._rebuild_mapping_index()
    before = rig.master_volume
    play(rig, "control_change", channel=4, control=30, value=1)
    assert rig.master_volume == before


def test_mappings_survive_preset_change(rig):
    from keyrig.presets import CCMapping

    rig.store.set_mapping(CCMapping(0, 30, "master_volume"))
    rig._rebuild_mapping_index()
    rig.cycle_preset(1)
    play(rig, "control_change", channel=0, control=30, value=11)
    assert rig.master_volume == 11


def test_program_change_only_switches_when_enabled(rig):
    start = rig.preset_name
    play(rig, "program_change", program=2)
    assert rig.preset_name == start
    rig.config.program_change_presets = True
    play(rig, "program_change", program=2)
    assert rig.preset_name == "Split: Organ low / Piano high"
    play(rig, "program_change", program=99)
    assert rig.preset_name == "Split: Organ low / Piano high"


def test_disconnect_releases_all_notes(rig):
    play(rig, "note_on", note=60, velocity=100)
    rig._on_disconnect()
    assert rig.router.snapshot().held == []
    rig.engine.get_samples(4096)
    assert rig.engine.active_voices() == 0


def test_velocity_zero_note_on_is_note_off(rig):
    play(rig, "note_on", note=60, velocity=100)
    play(rig, "note_on", note=60, velocity=0)
    assert rig.router.snapshot().held == []


async def test_mouse_click_selects_and_activates(rig):
    async with KeyrigApp(rig).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        grid = pilot.app.screen.grid
        y, x0, _x1, row, col = next(h for h in grid.hits if h[3] == 2 and h[4] == 0)
        await pilot.click("#grid", offset=(x0 + 1, y))
        await pilot.pause()
        assert (grid.row, grid.col) == (row, col)
        assert rig.layers[2].enabled is False
        await pilot.click("#grid", offset=(x0 + 1, y))
        await pilot.pause()
        assert rig.layers[2].enabled is True


async def test_small_terminal_does_not_crash(rig):
    async with KeyrigApp(rig).run_test(size=(50, 20)) as pilot:
        await pilot.pause()
        play(rig, "note_on", note=60, velocity=100)
        await pilot.pause(0.1)


async def test_visualizer_view_and_freeze_keys(rig):
    from keyrig.meter import MeterFrame

    async with KeyrigApp(rig).run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        screen.meter.stop()
        first = MeterFrame("live", (0.1, 0.1), (0.1, 0.1), (0.5,) * 96)
        screen.meter.frame = first
        screen.tick()
        await press(pilot, "v")
        assert screen.view == "waterfall"
        await press(pilot, "v")
        assert screen.view == "bars"
        await press(pilot, "f")
        screen.meter.frame = MeterFrame("live", (0.9, 0.9), (0.9, 0.9), (0.9,) * 96)
        depth = len(screen.history)
        screen.tick()
        assert screen.frame is first
        assert len(screen.history) == depth
        assert "FROZEN" in screen.query_one("#viz").border_title
        await press(pilot, "f")
        screen.tick()
        assert screen.frame is not first
