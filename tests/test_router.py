from keyrig.layers import DRUM_SYNTH_CHANNEL, DrumLayer, Instrument, Layer, Router


def make_router(engine, *layers):
    router = Router(engine)
    for index, layer in enumerate(layers):
        router.layers[index] = layer
    return router


def test_fan_out_only_to_enabled_layers_in_range(engine):
    router = make_router(
        engine,
        Layer(enabled=True, low=0, high=59),
        Layer(enabled=True, low=60, high=127),
        Layer(enabled=False),
        Layer(enabled=True, low=50, high=70),
    )
    router.note_on(0, 60, 100)
    assert engine.sounding == {(1, 60), (3, 60)}
    router.note_off(0, 60)
    assert engine.sounding == set()


def test_octave_shift_and_out_of_range(engine):
    router = make_router(engine, Layer(enabled=True, octave=1), Layer(enabled=True, octave=-3))
    router.note_on(0, 120, 100)  # layer 0 would be 132: dropped
    assert engine.sounding == {(1, 84)}


def test_octave_change_while_held_does_not_strand_note(engine):
    router = make_router(engine, Layer(enabled=True))
    router.note_on(0, 60, 100)
    router.layers[0].octave = 2
    router.note_off(0, 60)
    assert engine.sounding == set()


def test_range_change_while_held_does_not_strand_note(engine):
    router = make_router(engine, Layer(enabled=True))
    router.note_on(0, 60, 100)
    router.layers[0].low = 70
    router.note_off(0, 60)
    assert engine.sounding == set()


def test_toggle_layer_silences_it_even_with_pedal_down(engine):
    router = make_router(engine, Layer(enabled=True), Layer(enabled=True))
    router.sustain(127)
    router.note_on(0, 60, 100)
    router.set_layer_enabled(0, False)
    assert (0, 60) not in engine.sounding
    assert (1, 60) in engine.sounding
    assert engine.pedal[0] == 127, "pedal state restored for the channel"
    router.note_off(0, 60)
    router.sustain(0)
    assert engine.sounding == set()


def test_preset_load_releases_notes_and_keeps_keys_displayed(engine):
    router = make_router(engine, Layer(enabled=True))
    router.note_on(0, 60, 100)
    router.load([Layer(enabled=True, instrument=Instrument(program=19)) for _ in range(4)], DrumLayer())
    assert engine.sounding == set()
    assert router.snapshot().held == [60]
    router.note_off(0, 60)
    assert engine.names("noteoff") == [], "the old voice was already released"
    assert router.snapshot().held == []


def test_panic_with_pedal_down_clears_everything(engine):
    router = make_router(engine, Layer(enabled=True))
    router.sustain(127)
    router.note_on(0, 60, 100)
    router.note_on(9, 36, 100)
    router.note_off(0, 60)
    router.panic()
    assert engine.sounding == set()
    assert engine.sustained == set()
    assert router.snapshot().held == []
    assert engine.pedal[0] == 0


def test_note_retrigger_releases_previous_voice_first(engine):
    router = make_router(engine, Layer(enabled=True))
    router.note_on(0, 60, 100)
    router.note_on(0, 60, 90)
    assert ("noteoff", 0, 60) in engine.calls
    router.note_off(0, 60)
    assert engine.sounding == set()


def test_controllers_reach_all_key_layers_but_not_drums(engine):
    router = make_router(engine, Layer(enabled=True), Layer(enabled=False))
    router.sustain(127)
    router.mod_wheel(50)
    router.pitch_bend(1000)
    for channel in range(4):
        assert ("cc", channel, 64, 127) in engine.calls
        assert ("cc", channel, 1, 50) in engine.calls
        assert ("bend", channel, 1000) in engine.calls
    assert not [c for c in engine.calls if c[1] == DRUM_SYNTH_CHANNEL]


def test_drum_channel_goes_only_to_drum_layer(engine):
    router = make_router(engine, Layer(enabled=True))
    router.note_on(9, 38, 120)
    assert engine.sounding == {(DRUM_SYNTH_CHANNEL, 38)}
    assert router.snapshot().held == [], "pads are not shown on the key strip"
    router.note_off(9, 38)
    assert engine.sounding == set()


def test_key_channel_never_reaches_drum_layer(engine):
    router = make_router(engine, Layer(enabled=True))
    router.note_on(0, 38, 120)
    assert engine.sounding == {(0, 38)}


def test_custom_drum_channel(engine):
    router = Router(engine, drum_channel=15)
    router.layers[0] = Layer(enabled=True)
    router.note_on(15, 40, 100)
    router.note_on(9, 40, 100)
    assert engine.sounding == {(DRUM_SYNTH_CHANNEL, 40), (0, 40)}


def test_pad_lights_on_hit_then_clears(engine):
    router = Router(engine)
    router.note_on(9, 36 + 5, 127)
    pads = router.snapshot().pads
    assert pads[5] == 1.0
    assert sum(1 for p in pads if p > 0) == 1
    router.note_off(9, 36 + 5)
    router._pad_hit.clear()
    assert router.snapshot().pads[5] == 0.0


def test_pad_lights_when_drum_layer_disabled_but_is_silent(engine):
    router = Router(engine)
    router.drum.enabled = False
    router.note_on(9, 36, 100)
    assert engine.sounding == set()
    assert router.snapshot().pads[0] > 0


def test_notes_outside_pad_grid_do_not_light_pads(engine):
    router = Router(engine)
    router.note_on(9, 60, 100)
    assert sum(router.snapshot().pads) == 0
    assert engine.sounding == {(DRUM_SYNTH_CHANNEL, 60)}
