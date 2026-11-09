import gc
import sys

import pytest
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip

from keyrig import realtime

WIDTH = 20


@pytest.fixture(autouse=True)
def patched():
    realtime.stop_strip_reference_cycles()


def make_strip() -> Strip:
    return Strip([Segment("x", Style(color=f"rgb({i},0,0)")) for i in range(WIDTH)], WIDTH)


def cycles_left_by(operation) -> int:
    gc.collect()
    gc.disable()
    try:
        strip = make_strip()
        result = operation(strip)
        del result, strip
        return gc.collect()
    finally:
        gc.enable()


@pytest.mark.parametrize(
    "operation",
    [
        lambda strip: strip.divide([WIDTH]),
        lambda strip: strip.crop_extend(0, WIDTH, None),
        lambda strip: strip.divide([5, WIDTH]),
        lambda strip: strip.crop_extend(2, 10, None),
    ],
)
def test_strip_operations_leave_no_cycles(operation):
    assert cycles_left_by(operation) == 0


def test_divide_results_are_unchanged():
    strip = make_strip()
    assert strip.divide([WIDTH]) == [strip]
    left, right = strip.divide([5, WIDTH])
    assert (left.cell_length, right.cell_length) == (5, WIDTH - 5)


def test_crop_extend_results_are_unchanged():
    strip = make_strip()
    assert strip.crop_extend(0, WIDTH, None) is strip
    assert strip.crop_extend(2, 10, None).cell_length == 8
    assert strip.crop_extend(0, WIDTH + 4, None).cell_length == WIDTH + 4


def test_useful_results_stay_cached():
    strip = make_strip()
    first = strip.divide([5, WIDTH])
    assert strip.divide([5, WIDTH]) is first


def test_patching_twice_wraps_once():
    wrapped = Strip.divide
    realtime.stop_strip_reference_cycles()
    assert Strip.divide is wrapped


def test_prepare_for_tui_shortens_switch_interval():
    original = sys.getswitchinterval()
    try:
        realtime.prepare_for_tui()
        assert sys.getswitchinterval() == pytest.approx(realtime.SWITCH_INTERVAL_SECONDS)
    finally:
        sys.setswitchinterval(original)


def test_settle_makes_full_collections_rarer_and_keeps_small_ones_default(monkeypatch):
    original = gc.get_threshold()
    monkeypatch.setattr(realtime.gc, "collect", lambda: 0)
    monkeypatch.setattr(realtime.gc, "freeze", lambda: None)
    try:
        realtime.settle_garbage_collector()
        assert gc.get_threshold() == (original[0], original[1], realtime.FULL_COLLECTION_THRESHOLD)
    finally:
        gc.set_threshold(*original)
