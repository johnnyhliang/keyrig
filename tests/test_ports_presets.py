import tomllib

from keyrig.midi_input import (
    AUTO,
    KIND_DAW,
    KIND_NORMAL,
    KIND_THROUGH,
    choose_port,
    classify_port,
    classify_ports,
    stable_name,
)
from keyrig.presets import CCMapping, PresetStore, starter_presets

MK4_NORMAL = "Launchkey MK4 61:Launchkey MK4 61 MIDI In 20:0"
MK4_DAW = "Launchkey MK4 61:Launchkey MK4 61 DAW In 20:1"
AUDIO_IF = "H224 MIDI:H224 MIDI MIDI 1 24:0"
THROUGH = "Midi Through:Midi Through Port-0 14:0"


def test_stable_name_drops_alsa_address():
    assert stable_name(MK4_NORMAL) == "Launchkey MK4 61:Launchkey MK4 61 MIDI In"


def test_launchkey_ports_classified():
    assert classify_port(MK4_NORMAL).kind == KIND_NORMAL
    assert classify_port(MK4_DAW).kind == KIND_DAW
    assert classify_port("Launchkey MK3 49:Launchkey MK3 49 LKMK3 DAW Out 28:1").kind == KIND_DAW
    assert classify_port("Launchkey 61:Launchkey 61 InControl 28:1").kind == KIND_DAW
    assert classify_port("Launchkey 61 MK2:Launchkey 61 MK2 MIDI 2 28:1").kind == KIND_DAW
    assert classify_port("Launchkey 61 MK2:Launchkey 61 MK2 MIDI 1 28:0").kind == KIND_NORMAL
    assert classify_port(THROUGH).kind == KIND_THROUGH


def test_auto_prefers_keyboard_normal_port_over_daw_and_audio_interface():
    ports = classify_ports([THROUGH, MK4_DAW, AUDIO_IF, MK4_NORMAL])
    assert choose_port(ports, AUTO).name == MK4_NORMAL


def test_auto_never_picks_daw_or_through():
    assert choose_port(classify_ports([MK4_DAW, THROUGH]), AUTO) is None


def test_manual_choice_can_use_daw_port():
    ports = classify_ports([MK4_NORMAL, MK4_DAW])
    assert choose_port(ports, stable_name(MK4_DAW)).name == MK4_DAW


def test_explicit_choice_does_not_fall_back_when_unplugged():
    assert choose_port(classify_ports([AUDIO_IF]), stable_name(MK4_NORMAL)) is None


def test_replug_at_new_address_matches_same_stable_name():
    replugged = MK4_NORMAL.replace("20:0", "32:0")
    port = choose_port(classify_ports([replugged]), stable_name(MK4_NORMAL))
    assert port.name == replugged


def test_pipewire_clients_hidden():
    assert classify_ports(["PipeWire-System:input 144:0"]) == []


def test_virtual_port_is_auto_candidate_behind_real_keyboards():
    ports = classify_ports(["RtMidiOut Client:keyrig-fake 130:0", MK4_NORMAL])
    assert choose_port(ports, AUTO).name == MK4_NORMAL
    assert choose_port(classify_ports(["RtMidiOut Client:keyrig-fake 130:0"]), AUTO) is not None


def test_starter_presets_exist_and_are_correct():
    presets = {p.name: p for p in starter_presets()}
    assert set(presets) == {"Church Organ + Grand", "Piano Only", "Split: Organ low / Piano high", "Electric Piano"}
    stack = presets["Church Organ + Grand"].layers
    assert stack[0].instrument.program == 19 and stack[1].instrument.program == 0
    assert stack[1].volume < stack[0].volume
    split = presets["Split: Organ low / Piano high"].layers
    assert split[0].high + 1 == split[1].low
    assert presets["Electric Piano"].layers[0].instrument.program == 4


def test_round_trip_presets_and_mappings(tmp_path):
    path = tmp_path / "presets.toml"
    store = PresetStore(path)
    store.load()
    store.set_mapping(CCMapping(0, 21, "layer_volume", 2))
    store.set_mapping(CCMapping(15, 22, "master_volume"))

    again = PresetStore(path)
    again.load()
    assert again.presets == store.presets
    assert again.mappings == store.mappings
    assert again.mappings[0].channel == 0

    raw = tomllib.loads(path.read_text())
    assert raw["mapping"][1]["channel"] == 16, "stored 1-based for hand editing"


def test_same_knob_remaps_rather_than_duplicates(tmp_path):
    store = PresetStore(tmp_path / "p.toml")
    store.load()
    store.set_mapping(CCMapping(0, 21, "layer_volume", 1))
    store.set_mapping(CCMapping(0, 21, "reverb_level"))
    assert [m.target for m in store.mappings] == ["reverb_level"]


def test_corrupt_file_is_preserved(tmp_path):
    path = tmp_path / "presets.toml"
    path.write_text("this is = = not toml")
    store = PresetStore(path)
    store.load()
    assert len(store.presets) == 4
    assert store.warning
    assert [p for p in tmp_path.iterdir() if ".broken-" in p.name]


def test_out_of_range_values_clamped(tmp_path):
    path = tmp_path / "presets.toml"
    path.write_text(
        '[[preset]]\nname="x"\n[[preset.layer]]\nvolume=999\noctave=-50\nlow=100\nhigh=20\n'
        '[[mapping]]\nchannel=99\ncc=1\ntarget="layer_volume"\n'
        '[[mapping]]\nchannel=1\ncc=1\ntarget="nonsense"\n'
    )
    store = PresetStore(path)
    store.load()
    layer = store.presets[0].layers[0]
    assert (layer.volume, layer.octave, layer.low, layer.high) == (127, -3, 20, 100)
    assert len(store.presets[0].layers) == 4
    assert store.mappings == []
