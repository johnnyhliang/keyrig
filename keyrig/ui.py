"""Keyboard-driven Textual interface. All state lives in Rig; this only draws and edits it."""

from __future__ import annotations

import colorsys
import math
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click, MouseScrollDown, MouseScrollUp, Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .gm import GM_DRUM_LABELS, is_black_key, note_name
from .layers import DRUM_SYNTH_CHANNEL, NUM_LAYERS, NUM_PADS, Snapshot
from .meter import (
    SPECTRUM_HIGH_HZ,
    STATE_LIVE,
    STATE_UNAVAILABLE,
    AudioMeter,
    MeterFrame,
    column_for_frequency,
)
from .midi_input import AUTO, KIND_DAW
from .presets import (
    T_FILTER_CUTOFF,
    T_LAYER_VOLUME,
    T_MASTER_VOLUME,
    T_OCTAVE_SHIFT,
    T_REVERB_LEVEL,
    T_ROTARY_SPEED,
    target_label,
)
from .rig import Rig
from .synth_engine import NODE_NAME, list_audio_devices

REFRESH_HZ = 20
COMPACT_BELOW_ROWS = 30
ACTIVITY_SECONDS = 0.15

ROW_DRUM = NUM_LAYERS
ROW_FX = NUM_LAYERS + 1
ROW_PLAY = NUM_LAYERS + 2
ROW_COUNT = NUM_LAYERS + 3
LAYER_COLUMNS = (
    "on", "instrument", "volume", "octave", "low", "high", "cutoff", "attack", "release", "pedal", "rotary",
)
DRUM_COLUMNS = ("on", "instrument", "volume")
FX_COLUMNS = ("master", "reverb", "room", "level", "chorus")
PLAY_COLUMNS = ("transpose", "key_curve", "pad_curve", "rotary_speed")
ENVELOPE_STEPS_MS = (0, 5, 10, 20, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000)

# parameter -> (normal step, coarse step)
STEPS: dict[str, tuple[Any, Any]] = {
    "volume": (3, 12), "cutoff": (3, 12), "low": (1, 12), "high": (1, 12), "octave": (1, 1),
    "master": (3, 12), "room": (0.05, 0.2), "level": (0.05, 0.2),
}

COLOR_ACCENT = "#ffb000"
COLOR_OK = "#50fa7b"
COLOR_DIM = "#666677"
COLOR_BAD = "#ff5555"
COLOR_HELD = "#ff7a18"
PAD_OFF = (42, 42, 58)
PAD_ON = (0, 229, 255)


def columns_for(row: int) -> tuple[str, ...]:
    if row == ROW_FX:
        return FX_COLUMNS
    if row == ROW_PLAY:
        return PLAY_COLUMNS
    return DRUM_COLUMNS if row == ROW_DRUM else LAYER_COLUMNS


def format_milliseconds(value: int) -> str:
    if value <= 0:
        return "auto"
    return str(value) if value < 1000 else f"{value / 1000:g}s"


def step_envelope(value: int, direction: int, coarse: bool) -> int:
    position = min(range(len(ENVELOPE_STEPS_MS)), key=lambda i: abs(ENVELOPE_STEPS_MS[i] - value))
    moved = position + direction * (3 if coarse else 1)
    return ENVELOPE_STEPS_MS[max(0, min(len(ENVELOPE_STEPS_MS) - 1, moved))]


class LayerGrid(Static):
    def __init__(self, rig: Rig, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rig = rig
        self.row = 0
        self.col = 1
        self.hits: list[tuple[int, int, int, int, int]] = []  # y, x0, x1, row, col

    @property
    def field(self) -> str:
        return columns_for(self.row)[self.col]

    def move_row(self, delta: int) -> None:
        self.row = max(0, min(ROW_COUNT - 1, self.row + delta))
        self.col = min(self.col, len(columns_for(self.row)) - 1)

    def move_col(self, delta: int) -> None:
        self.col = (self.col + delta) % len(columns_for(self.row))

    def _cell(self, line: Text, y: int, row: int, col: int, text: str, width: int, style: str = "") -> None:
        selected = (row, col) == (self.row, self.col)
        x0 = line.cell_len
        line.append(text[:width].ljust(width), "reverse bold" if selected else style)
        self.hits.append((y, x0, x0 + width, row, col))
        line.append(" ")

    def draw(self, sounding: frozenset[int] = frozenset()) -> Text:
        rig = self.rig
        self.hits = []
        width = self.content_size.width or 78
        name_width = max(14, width - 51)
        lines = [
            Text(f"  #  On {'Instrument'.ljust(name_width)}  Vol  Oct  Low  High  Cut Atk  Rel Ped Rot", style=COLOR_DIM)
        ]

        for index, layer in enumerate(rig.layers):
            y = len(lines)
            cursor = ">" if self.row == index else " "
            base = "" if layer.enabled else COLOR_DIM
            line = Text(f"{cursor} ", style=base)
            line.append(str(index + 1), f"bold {COLOR_HELD}" if index in sounding else base)
            line.append("  ", base)
            self._cell(line, y, index, 0, "●" if layer.enabled else "○", 2, COLOR_OK if layer.enabled else COLOR_DIM)
            self._cell(line, y, index, 1, layer.instrument.name or "(none)", name_width, base)
            self._cell(line, y, index, 2, f"{layer.volume:>3}", 4, base)
            self._cell(line, y, index, 3, f"{layer.octave:>+3}", 4, base)
            self._cell(line, y, index, 4, f"{note_name(layer.low):>4}", 4, base)
            self._cell(line, y, index, 5, f"{note_name(layer.high):>4}", 5, base)
            self._cell(line, y, index, 6, f"{layer.cutoff:>3}", 3, base)
            self._cell(line, y, index, 7, f"{format_milliseconds(layer.attack):>4}", 4, base)
            self._cell(line, y, index, 8, f"{format_milliseconds(layer.release):>4}", 4, base)
            self._cell(line, y, index, 9, " ●" if layer.pedal else " ○", 3, COLOR_OK if layer.pedal else COLOR_DIM)
            self._cell(line, y, index, 10, " ●" if layer.rotary else " ○", 3, COLOR_ACCENT if layer.rotary else COLOR_DIM)
            lines.append(line)

        y = len(lines)
        drum = rig.drum
        base = "" if drum.enabled else COLOR_DIM
        line = Text(f"{'>' if self.row == ROW_DRUM else ' '} ", style=base)
        line.append("D", f"bold {COLOR_HELD}" if DRUM_SYNTH_CHANNEL in sounding else base)
        line.append("  ", base)
        self._cell(line, y, ROW_DRUM, 0, "●" if drum.enabled else "○", 2, COLOR_OK if drum.enabled else COLOR_DIM)
        self._cell(line, y, ROW_DRUM, 1, f"{drum.name or '(no kit)'}", name_width, base)
        self._cell(line, y, ROW_DRUM, 2, f"{drum.volume:>3}", 4, base)
        line.append(f"drums: ch{rig.config.drum_channel}", COLOR_DIM)
        lines.append(line)

        y = len(lines)
        fx = rig.fx
        line = Text(f"{'>' if self.row == ROW_FX else ' '} ")
        for col, (label, value, cell) in enumerate((
            ("Master", f"{rig.master_volume:>3}", 3),
            ("Reverb", "on" if fx.reverb_on else "off", 3),
            ("Room", f"{fx.reverb_room:.2f}", 4),
            ("Level", f"{fx.reverb_level:.2f}", 4),
            ("Chorus", "on" if fx.chorus_on else "off", 3),
        )):
            line.append(f"{label} ", COLOR_DIM)
            self._cell(line, y, ROW_FX, col, value, cell)
            line.append(" ")
        lines.append(line)

        y = len(lines)
        router = rig.router
        line = Text(f"{'>' if self.row == ROW_PLAY else ' '} ")
        for col, (label, value, cell) in enumerate((
            ("Transpose", f"{router.transpose:+d}", 3),
            ("Keys", router.key_curve, 7),
            ("Pads", router.pad_curve, 7),
            ("Rotary", "fast" if rig.rotary_fast else "slow", 4),
        )):
            line.append(f"{label} ", COLOR_DIM)
            self._cell(line, y, ROW_PLAY, col, value, cell)
            line.append(" ")
        lines.append(line)
        return Text("\n").join(lines)

    def _hit(self, event: Click | MouseScrollUp | MouseScrollDown) -> tuple[int, int] | None:
        offset = event.get_content_offset(self)
        if offset is None:
            return None
        for y, x0, x1, row, col in self.hits:
            if y == offset.y and x0 <= offset.x < x1:
                return row, col
        return None

    def on_click(self, event: Click) -> None:
        hit = self._hit(event)
        if hit is None:
            return
        if hit == (self.row, self.col):
            self.screen.action_activate()
        else:
            self.row, self.col = hit

    def _scroll(self, event: MouseScrollUp | MouseScrollDown, delta: int) -> None:
        hit = self._hit(event)
        if hit is not None:
            self.row, self.col = hit
            self.screen.action_adjust(delta, False)
        event.stop()

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        self._scroll(event, 1)

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        self._scroll(event, -1)


def key_window(width: int) -> tuple[int, int]:
    """Lowest and highest note that fit: 88 keys, else the 61 keys of a Launchkey 61."""
    if width >= 88:
        return 21, 108
    if width >= 61:
        return 36, 96
    low = max(0, 66 - width // 2)
    return low, low + max(1, width) - 1


def draw_keys(width: int, held: list[int]) -> Text:
    low, high = key_window(width)
    active = set(held)
    labels = [" "] * (high - low + 1)
    for note in range(low, high + 1):
        if note % 12 == 0:
            for offset, char in enumerate(note_name(note)):
                if note - low + offset < len(labels):
                    labels[note - low + offset] = char
    keys = Text()
    white_shade = 0
    for note in range(low, high + 1):
        if note in active:
            color = COLOR_HELD
        elif is_black_key(note):
            color = "#262630"
        else:
            white_shade += 1
            color = "#e6e6e6" if white_shade % 2 else "#c4c4c4"
        keys.append(" ", style=f"on {color}")
    return Text("\n").join([Text("".join(labels), style=COLOR_DIM), keys, keys.copy()])


HORIZONTAL_BLOCKS = " ▏▎▍▌▋▊▉█"
VERTICAL_BLOCKS = " ▁▂▃▄▅▆▇█"
METER_FLOOR_DB = -48.0
METER_SIDE_COLUMNS = 14  # label, dB readout and clip light
METER_EMPTY = "#333344"
VIEW_BARS = "bars"
VIEW_WATERFALL = "waterfall"
WATERFALL_HISTORY = 96


def meter_fraction(level: float) -> float:
    """Linear level to bar position: 0 at the -48 dB floor, 1 at full scale."""
    if level <= 0:
        return 0.0
    return max(0.0, min(1.0, 1 - 20 * math.log10(level) / METER_FLOOR_DB))


def _meter_color(position: float) -> str:
    if position < 0.75:  # below -12 dB
        return COLOR_OK
    return "#ffd166" if position < 0.9375 else COLOR_BAD  # red above -3 dB


def draw_meter(label: str, level: float, hold: float, width: int, clipped: bool) -> Text:
    bar_width = max(4, width - METER_SIDE_COLUMNS - (len(label) - 1))
    eighths = round(meter_fraction(level) * bar_width * 8)
    hold_position = meter_fraction(hold)
    hold_cell = min(bar_width - 1, int(hold_position * bar_width)) if hold_position > 0 else -1
    line = Text(f"{label} ", COLOR_DIM)
    for cell in range(bar_width):
        fill = max(0, min(8, eighths - cell * 8))
        color = _meter_color((cell + 0.5) / bar_width)
        if fill:
            line.append(HORIZONTAL_BLOCKS[fill], color)
        elif cell == hold_cell:
            line.append("│", color)
        else:
            line.append("·", METER_EMPTY)
    decibels = f"{20 * math.log10(level):6.1f}" if level > 1e-5 else "  -inf"
    line.append(f" {decibels} ", "default")
    line.append("CLIP", f"bold white on {COLOR_BAD}" if clipped else METER_EMPTY)
    return line


@lru_cache(maxsize=8)
def _column_colors(width: int) -> tuple[str, ...]:
    """A rainbow across the frequency axis: red bass through to violet treble."""
    colors = []
    for column in range(width):
        red, green, blue = colorsys.hsv_to_rgb(0.78 * column / max(1, width - 1), 0.8, 1.0)
        colors.append(f"rgb({round(red * 255)},{round(green * 255)},{round(blue * 255)})")
    return tuple(colors)


def _resample(values: tuple[float, ...], index: int, width: int) -> float:
    return values[index * len(values) // width] if values else 0.0


def draw_spectrum(bands: tuple[float, ...], caps: tuple[float, ...], width: int, rows: int) -> list[Text]:
    """One bar per terminal column on a true log-frequency axis, with falling peak caps."""
    colors = _column_colors(width)
    eighths = [round(_resample(bands, index, width) * rows * 8) for index in range(width)]
    cap_rows = [
        min(rows - 1, int(_resample(caps, index, width) * rows)) if _resample(caps, index, width) > 0.02 else -1
        for index in range(width)
    ]
    lines = []
    for row in range(rows):
        height_above_floor = rows - 1 - row
        line = Text()
        for index in range(width):
            fill = max(0, min(8, eighths[index] - height_above_floor * 8))
            if fill:
                line.append(VERTICAL_BLOCKS[fill], colors[index])
            elif cap_rows[index] == height_above_floor:
                line.append("▔", "bold white")
            else:
                line.append(" ")
        lines.append(line)
    return lines


@lru_cache(maxsize=1)
def _heat_colors() -> tuple[str, ...]:
    stops = ((12, 12, 24), (70, 20, 130), (200, 40, 110), (255, 130, 30), (255, 230, 120), (255, 255, 255))
    colors = []
    for step in range(64):
        position = step / 63 * (len(stops) - 1)
        low = min(int(position), len(stops) - 2)
        fraction = position - low
        red, green, blue = (round(a + (b - a) * fraction) for a, b in zip(stops[low], stops[low + 1]))
        colors.append(f"rgb({red},{green},{blue})")
    return tuple(colors)


def draw_waterfall(history: Sequence[tuple[float, ...]], width: int, rows: int) -> list[Text]:
    """Spectrum over time, newest at the bottom. Each character cell shows two time steps."""
    colors = _heat_colors()

    def color(age: int, column: int) -> str:
        if age >= len(history):
            return colors[0]
        return colors[round(_resample(history[-1 - age], column, width) * 63)]

    lines = []
    for row in range(rows):
        lower_age = (rows - 1 - row) * 2
        line = Text()
        for column in range(width):
            line.append("▀", f"{color(lower_age + 1, column)} on {color(lower_age, column)}")
        lines.append(line)
    return lines


def _place_labels(width: int, labels: Iterable[tuple[int, str]]) -> str:
    """Write labels at columns without letting a later label overwrite an earlier one."""
    cells = [" "] * width
    for column, label in labels:
        span = range(column, column + len(label) + 1)
        if column >= 0 and span.stop <= width + 1 and all(c >= width or cells[c] == " " for c in span):
            for offset, char in enumerate(label):
                cells[column + offset] = char
    return "".join(cells)


def note_frequency(note: float) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def draw_hz_axis(width: int) -> Text:
    marks = ((50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1k"), (2000, "2k"), (5000, "5k"), (10000, "10k"))
    labels = [(column_for_frequency(hz, width), f"╵{label}") for hz, label in marks]
    return Text(_place_labels(width, labels), COLOR_DIM)


def draw_note_axis(width: int, lowest_note: int | None) -> Text:
    """Where each overtone of the lowest sounding note lands, or octave markers when idle."""
    if lowest_note is None:
        labels = [(column_for_frequency(note_frequency(n), width), note_name(n)) for n in range(24, 120, 12)]
        return Text(_place_labels(width, labels), COLOR_DIM)
    root = note_frequency(lowest_note)
    labels = [
        (column_for_frequency(root * n, width), str(n) if n < 10 else "•")
        for n in range(1, int(SPECTRUM_HIGH_HZ / root) + 1)
    ]
    return Text(_place_labels(width, labels), COLOR_HELD)


def describe_frequency(hz: float) -> str:
    """For example '261.6 Hz C4 +1¢'."""
    exact = 69 + 12 * math.log2(hz / 440)
    note = round(exact)
    return f"{hz:.1f} Hz {note_name(note)} {round((exact - note) * 100):+d}¢"


def draw_visualizer(
    frame: MeterFrame,
    width: int,
    height: int,
    view: str = VIEW_BARS,
    history: Sequence[tuple[float, ...]] = (),
    lowest_note: int | None = None,
) -> Text:
    """Meters, a spectrum or waterfall, and frequency axes, fitted to the rows available."""
    if height <= 0 or width <= 0:
        return Text()
    if frame.state == STATE_UNAVAILABLE:
        return Text("The meter needs a PipeWire output (press a to choose one).", COLOR_DIM)
    if height >= 8:
        lines = [
            draw_meter("L", frame.peak[0], frame.hold[0], width, frame.clipped),
            draw_meter("R", frame.peak[1], frame.hold[1], width, frame.clipped),
        ]
    else:
        lines = [draw_meter("LR", max(frame.peak), max(frame.hold), width, frame.clipped)]
    axes = 2 if height >= 5 else 1 if height >= 3 else 0
    graph_rows = height - len(lines) - axes
    if graph_rows > 0:
        lines += (
            draw_waterfall(history, width, graph_rows)
            if view == VIEW_WATERFALL
            else draw_spectrum(frame.bands, frame.caps, width, graph_rows)
        )
    if axes == 2:
        lines.append(draw_note_axis(width, lowest_note))
    if axes:
        lines.append(draw_hz_axis(width))
    return Text("\n").join(lines)


def draw_pads(brightness: list[float], base_note: int) -> Text:
    rows = []
    for row in range(3, -1, -1):  # pad 1 is at the bottom left, like the hardware
        line = Text()
        for col in range(4):
            pad = row * 4 + col
            level = min(1.0, brightness[pad])
            red, green, blue = (round(a + (b - a) * level) for a, b in zip(PAD_OFF, PAD_ON))
            note = base_note + pad
            label = GM_DRUM_LABELS.get(note, note_name(note))
            line.append(f"{label:^6}", style=f"{'black' if level > 0.4 else '#9090a8'} on rgb({red},{green},{blue})")
            if col < 3:
                line.append(" ")
        rows.append(line)
    return Text("\n").join(rows)


def _matches(search: str, needle: str) -> bool:
    return all(term in search for term in needle.lower().split())


class PickerScreen(ModalScreen):
    """Searchable list. Dismisses with the chosen value, or None on Esc."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("pageup", "move(-10)", show=False),
        Binding("pagedown", "move(10)", show=False),
    ]

    def __init__(self, title: str, items: list[tuple[str, Any, str]], current: Any = None) -> None:
        super().__init__()
        self.title_text = title
        self.items = items
        self.current = current
        self.shown: list[tuple[str, Any, str]] = items

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, markup=False, classes="dialog-title")
            yield Input(placeholder="Type to filter…", id="filter")
            yield OptionList(id="options")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#filter", Input).focus()

    def _fill(self, needle: str) -> None:
        self.shown = [item for item in self.items if _matches(item[2], needle)]
        options = self.query_one("#options", OptionList)
        options.clear_options()
        options.add_options([Option(Text(item[0])) for item in self.shown])
        if self.shown:
            index = next((i for i, item in enumerate(self.shown) if item[1] == self.current), 0)
            options.highlighted = index

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        highlighted = self.query_one("#options", OptionList).highlighted
        if highlighted is not None and highlighted < len(self.shown):
            self.dismiss(self.shown[highlighted][1])

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.shown[event.option_index][1])

    def action_move(self, delta: int) -> None:
        options = self.query_one("#options", OptionList)
        if self.shown:
            current = options.highlighted or 0
            options.highlighted = max(0, min(len(self.shown) - 1, current + delta))


class SavePresetScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", show=False)]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.initial = name

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-small"):
            yield Static("Save preset as (Enter saves, Esc cancels)", markup=False, classes="dialog-title")
            yield Input(value=self.initial, id="name")

    @on(Input.Submitted, "#name")
    def _submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if name:
            self.dismiss(name)


def learn_targets() -> list[tuple[str, tuple[str, int]]]:
    targets = [
        (target_label(target, 0), (target, 0)) for target in (T_MASTER_VOLUME, T_REVERB_LEVEL, T_ROTARY_SPEED)
    ]
    for target in (T_LAYER_VOLUME, T_FILTER_CUTOFF, T_OCTAVE_SHIFT):
        for layer in (0, 1, 2, 3, 4):
            targets.append((target_label(target, layer), (target, layer)))
    return targets


class LearnScreen(ModalScreen):
    """Listens for a knob, then asks which parameter that knob should control."""

    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("delete,x", "remove", show=False),
    ]

    def __init__(self, rig: Rig) -> None:
        super().__init__()
        self.rig = rig
        self.assigning = False
        self.targets = learn_targets()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("", markup=False, id="prompt", classes="dialog-title")
            yield OptionList(id="options")

    def on_mount(self) -> None:
        self.rig.begin_learn()
        self._show_listening()
        self.query_one("#options", OptionList).focus()
        self.set_interval(0.05, self._poll)

    def _show_listening(self) -> None:
        self.query_one("#prompt", Static).update(
            "LEARN: move a knob or slider now. Esc closes. x removes the highlighted mapping."
        )
        options = self.query_one("#options", OptionList)
        options.clear_options()
        options.add_options(
            [Option(Text(f"{m.source_label:<12} -> {m.target_label}")) for m in self.rig.store.mappings]
            or [Option(Text("(no mappings yet)"), disabled=True)]
        )
        if self.rig.store.mappings:
            options.highlighted = 0

    def _poll(self) -> None:
        if self.assigning or self.rig.learned is None:
            return
        self.assigning = True
        channel, control = self.rig.learned
        self.query_one("#prompt", Static).update(f"CC{control} on channel {channel + 1}: choose its parameter, then Enter")
        options = self.query_one("#options", OptionList)
        options.clear_options()
        options.add_options([Option(Text(label)) for label, _ in self.targets])
        options.highlighted = 0

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if not self.assigning:
            return
        target, layer = self.targets[event.option_index][1]
        mapping = self.rig.assign_learned(target, layer)
        self.dismiss(mapping)

    def action_remove(self) -> None:
        highlighted = self.query_one("#options", OptionList).highlighted
        mappings = self.rig.store.mappings
        if not self.assigning and highlighted is not None and highlighted < len(mappings):
            self.rig.remove_mapping(mappings[highlighted])
            self._show_listening()

    def action_cancel(self) -> None:
        self.rig.cancel_learn()
        self.dismiss(None)


HELP_TEXT = (
    "↑↓/jk row  Tab col  ←→/hl adjust (shift: coarse)  Enter pick  1-4 layer  d drums  "
    "[ ] preset  P list  s save  m MIDI  a audio  c learn  r rotary  v view  f freeze  Space panic  q quit"
)


class MainScreen(Screen):
    BINDINGS = [
        Binding("up,k", "row(-1)", show=False),
        Binding("down,j", "row(1)", show=False),
        Binding("tab", "col(1)", show=False, priority=True),
        Binding("shift+tab", "col(-1)", show=False, priority=True),
        Binding("left,h", "adjust(-1, False)", show=False),
        Binding("right,l", "adjust(1, False)", show=False),
        Binding("shift+left,H", "adjust(-1, True)", show=False),
        Binding("shift+right,L", "adjust(1, True)", show=False),
        Binding("enter", "activate", show=False),
        Binding("1", "toggle(0)", show=False),
        Binding("2", "toggle(1)", show=False),
        Binding("3", "toggle(2)", show=False),
        Binding("4", "toggle(3)", show=False),
        Binding("d", "toggle_drums", show=False),
        Binding("left_square_bracket", "preset(-1)", show=False),
        Binding("right_square_bracket", "preset(1)", show=False),
        Binding("space", "panic", show=False),
        Binding("P", "pick_preset", show=False),
        Binding("s", "save", show=False),
        Binding("m", "pick_midi", show=False),
        Binding("a", "pick_audio", show=False),
        Binding("c", "learn", show=False),
        Binding("r", "rotary_speed", show=False),
        Binding("v", "cycle_view", show=False),
        Binding("f", "toggle_freeze", show=False),
        Binding("q", "app.quit", show=False),
    ]

    def __init__(self, rig: Rig) -> None:
        super().__init__()
        self.rig = rig
        self._drawn: dict[str, Text] = {}
        self.meter = AudioMeter(NODE_NAME, self._audio_generation)
        self.view = VIEW_BARS
        self.frozen = False
        self.frame = MeterFrame()
        self.history: deque[tuple[float, ...]] = deque(maxlen=WATERFALL_HISTORY)
        self.lowest_note: int | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield LayerGrid(self.rig, id="grid")
        yield Static(id="keys")
        with Horizontal(id="bottom"):
            yield Static(id="pads")
            yield Static(id="activity")
        yield Static(id="viz")
        yield Static(HELP_TEXT, markup=False, id="help")

    def on_mount(self) -> None:
        self.query_one("#keys").border_title = "Keys"
        self.query_one("#pads").border_title = "Pads"
        self.query_one("#activity").border_title = "Activity"
        self.query_one("#viz").border_title = "Output"
        self.meter.start()
        self.tick()
        self.set_interval(1 / REFRESH_HZ, self.tick)

    def on_unmount(self) -> None:
        self.meter.stop()

    def on_resize(self, event: Resize) -> None:
        # Short terminals give up the help text and a border to fit the visualizer.
        self.set_class(event.size.height < COMPACT_BELOW_ROWS, "compact")

    def _audio_generation(self) -> int | None:
        engine = self.rig.engine
        device = engine.audio_device
        return engine.audio_generation if device is not None and device.driver == "pipewire" else None

    def _set(self, widget_id: str, content: Text) -> None:
        if self._drawn.get(widget_id) != content:
            self._drawn[widget_id] = content
            self.query_one(f"#{widget_id}", Static).update(content)

    def tick(self) -> None:
        rig = self.rig
        snapshot = rig.router.snapshot()
        self._set("status", self._draw_status(snapshot))
        self._set("grid", self.query_one("#grid", LayerGrid).draw(snapshot.sounding))
        self._set("keys", draw_keys(self.query_one("#keys").content_size.width or 78, snapshot.held))
        self._set("pads", draw_pads(snapshot.pads, rig.router.pad_base))
        self._set("activity", self._draw_activity(snapshot.held))
        viz = self.query_one("#viz")
        size = viz.content_size
        self.meter.band_count = max(1, size.width)
        if not self.frozen:
            self.frame = self.meter.frame
            if self.frame.state == STATE_LIVE:
                self.history.append(self.frame.bands)
            self.lowest_note = snapshot.lowest_sounded
        frame = self.frame
        parts = ["Output" if frame.state == STATE_LIVE else f"Output: {frame.state}", self.view]
        if frame.dominant_hz is not None:
            parts.append(f"loudest {describe_frequency(frame.dominant_hz)}")
        if self.frozen:
            parts.append("FROZEN")
        title = "  ".join(parts)
        if viz.border_title != title:
            viz.border_title = title
        self._set("viz", draw_visualizer(frame, size.width, size.height, self.view, self.history, self.lowest_note))

    def _draw_output_status(self, line: Text) -> None:
        line.append("\nOut ", COLOR_DIM)
        status = self.meter.output
        if self.rig.engine.audio_device is None:
            line.append("no output open", COLOR_BAD)
        elif status is None:
            line.append("ALSA direct, no status available", COLOR_DIM)
        elif not status.connected:
            line.append("NOT CONNECTED", f"bold {COLOR_BAD}")
        elif status.muted:
            line.append("MUTED", f"bold {COLOR_BAD}")
        else:
            line.append("connected", COLOR_OK)
            if status.volume is not None:
                line.append(f"  vol {status.volume * 100:.0f}%", COLOR_BAD if status.volume == 0 else "default")

    def _draw_status(self, snapshot: Snapshot) -> Text:
        rig = self.rig
        width = self.query_one("#status").content_size.width or 78
        line = Text()
        count = len(rig.store.presets)
        line.append("keyrig ", f"bold {COLOR_ACCENT}")
        line.append(f"Preset {rig.preset_index + 1}/{count} ", COLOR_DIM)
        line.append(rig.preset_name, "bold")
        line.append("  ")
        line.append(" SUS ", "bold black on " + COLOR_OK if snapshot.sustain else METER_EMPTY)
        line.append(f" MOD {snapshot.mod:<3}", COLOR_ACCENT if snapshot.mod else METER_EMPTY)
        line.append(f" BEND {snapshot.bend:+d}", COLOR_ACCENT if snapshot.bend else METER_EMPTY)
        devices = Text("MIDI ", COLOR_DIM)
        port = rig.midi.current
        if port is not None:
            devices.append(port.label, COLOR_OK)
            devices.append(f" ({port.kind})", COLOR_BAD if port.kind == KIND_DAW else COLOR_DIM)
        else:
            wanted = "keyboard" if rig.midi.preference == AUTO else rig.midi.preference
            devices.append(f"waiting for {wanted}…", COLOR_BAD)
        devices.append("  Audio ", COLOR_DIM)
        device = rig.engine.audio_device
        if device is not None:
            devices.append(device.label.replace("PipeWire: ", "PW: ").replace("ALSA direct: ", "ALSA: "))
        else:
            devices.append(rig.engine.audio_error or "not running", COLOR_BAD)
        latency = Text("Latency ", COLOR_DIM)
        info = rig.engine.latency()
        buffers = f"{info.period_size}" if info.driver == "pipewire" else f"{info.period_size}x{info.periods}"
        latency.append(f"{info.configured_ms:.1f} ms cfg ({buffers}@{info.sample_rate})", "default")
        if rig.graph_quantum is not None:
            graph = rig.graph_quantum
            latency.append(f"  graph {graph.quantum}@{graph.rate} = {graph.quantum / graph.rate * 1000:.1f} ms", "default")
        latency.append(f"  MIDI->synth {rig.dispatch.last_ms:.2f} ms (peak {rig.dispatch.peak_ms:.2f})", "default")
        # One line each, so a long device name can never push the latency off screen.
        for part in (line, devices, latency):
            part.truncate(width, overflow="ellipsis")
        return Text("\n").join([line, devices, latency])

    def _draw_activity(self, held: list[int]) -> Text:
        rig = self.rig
        active = time.monotonic() - rig.last_event_time < ACTIVITY_SECONDS
        line = Text()
        line.append("● " if active else "○ ", COLOR_OK if active else COLOR_DIM)
        line.append(rig.last_event or "no MIDI yet")
        line.append(f"\nHeld: {' '.join(note_name(n) for n in held) or '-'}"[:60])
        self._draw_output_status(line)
        line.append(f"\nVoices {rig.engine.active_voices()}/{rig.config.polyphony}  CPU {rig.engine.cpu_load():.0f}%", COLOR_DIM)
        xruns = rig.graph_quantum.errors if rig.graph_quantum is not None else None
        if xruns is not None:
            line.append("  Dropouts ", COLOR_DIM)
            line.append(str(xruns), COLOR_BAD if xruns else COLOR_DIM)
        warnings = [rig.store.warning, rig.midi.error, *rig.config.warnings]
        if not rig.engine.has_soundfont:
            warnings.append("No SoundFont loaded: install fluid-soundfont-gm")
        if rig.engine.log_messages:
            warnings.append(rig.engine.log_messages[-1])
        message = next((w for w in warnings if w), "")
        activity = self.query_one("#activity")
        subtitle = Text(message[:60], COLOR_BAD) if message else None
        if activity.border_subtitle != subtitle:
            activity.border_subtitle = subtitle
        return line

    @property
    def grid(self) -> LayerGrid:
        return self.query_one("#grid", LayerGrid)

    def action_row(self, delta: int) -> None:
        self.grid.move_row(delta)

    def action_col(self, delta: int) -> None:
        self.grid.move_col(delta)

    def action_toggle(self, index: int) -> None:
        self.rig.toggle_layer(index)

    def action_toggle_drums(self) -> None:
        self.rig.toggle_drum()

    def action_preset(self, delta: int) -> None:
        self.rig.cycle_preset(delta)

    def action_cycle_view(self) -> None:
        self.view = VIEW_WATERFALL if self.view == VIEW_BARS else VIEW_BARS

    def action_toggle_freeze(self) -> None:
        self.frozen = not self.frozen

    def action_panic(self) -> None:
        self.rig.panic()

    def action_adjust(self, direction: int, coarse: bool) -> None:
        rig, grid = self.rig, self.grid
        field = grid.field
        step = STEPS[field][1 if coarse else 0] * direction if field in STEPS else 0
        if grid.row < NUM_LAYERS:
            layer = rig.layers[grid.row]
            if field == "on":
                rig.toggle_layer(grid.row)
            elif field == "instrument":
                rig.step_instrument(grid.row, direction)
            elif field in ("attack", "release"):
                rig.set_layer_value(grid.row, field, step_envelope(getattr(layer, field), direction, coarse))
            elif field == "pedal":
                rig.toggle_layer_pedal(grid.row)
            elif field == "rotary":
                rig.toggle_layer_rotary(grid.row)
            else:
                rig.set_layer_value(grid.row, field, getattr(layer, field) + step)
        elif grid.row == ROW_DRUM:
            if field == "on":
                rig.toggle_drum()
            elif field == "instrument":
                rig.step_drum_kit(direction)
            else:
                rig.set_drum_volume(rig.drum.volume + step)
        elif field == "master":
            rig.set_master(rig.master_volume + step)
        elif field == "reverb":
            rig.set_reverb(on=not rig.fx.reverb_on)
        elif field == "room":
            rig.set_reverb(room=rig.fx.reverb_room + step)
        elif field == "level":
            rig.set_reverb(level=rig.fx.reverb_level + step)
        elif field == "chorus":
            rig.set_chorus(not rig.fx.chorus_on)
        elif field == "transpose":
            rig.set_transpose(rig.router.transpose + direction * (12 if coarse else 1))
        elif field == "key_curve":
            rig.cycle_velocity_curve("key", direction)
        elif field == "pad_curve":
            rig.cycle_velocity_curve("pad", direction)
        elif field == "rotary_speed":
            rig.toggle_rotary_speed()

    def action_activate(self) -> None:
        grid = self.grid
        if grid.field == "instrument":
            self._pick_instrument(grid.row)
        elif grid.field in ("on", "reverb", "chorus", "pedal", "rotary", "rotary_speed"):
            self.action_adjust(1, False)

    def action_rotary_speed(self) -> None:
        self.rig.toggle_rotary_speed()

    def _push_picker(self, title: str, items: list, current: Any, chosen: Callable[[Any], None]) -> None:
        def handle(value: Any) -> None:
            if value is not None:
                chosen(value)

        self.app.push_screen(PickerScreen(title, items, current), handle)

    def _pick_instrument(self, row: int) -> None:
        rig = self.rig
        if row == ROW_DRUM:
            kits = rig.engine.drum_kits()
            items = [(f"{e.name}  [{e.program:03}] {e.soundfont}", e, e.search_text) for e in kits]
            current = next((e for e in kits if e.program == rig.drum.kit and e.soundfont == rig.drum.soundfont), None)
            self._push_picker("Drum kit", items, current, rig.set_drum_kit)
            return
        layer = rig.layers[row]
        entries = rig.engine.melodic_entries()
        items = [(f"{e.bank:03}:{e.program:03}  {e.name}  {e.soundfont}", e, e.search_text) for e in entries]
        current = next(
            (e for e in entries if (e.soundfont, e.bank, e.program)
             == (layer.instrument.soundfont, layer.instrument.bank, layer.instrument.program)),
            None,
        )
        self._push_picker(f"Instrument for layer {row + 1}", items, current, lambda e: rig.set_instrument(row, e))

    def action_pick_preset(self) -> None:
        rig = self.rig
        items = [(p.name, i, p.name.lower()) for i, p in enumerate(rig.store.presets)]
        self._push_picker("Load preset", items, rig.preset_index, rig.load_preset)

    def action_save(self) -> None:
        def handle(name: str | None) -> None:
            if name:
                self.rig.save_preset(name)
                self.notify(f"Saved preset: {name}", timeout=2)

        self.app.push_screen(SavePresetScreen(self.rig.preset_name), handle)

    def action_pick_midi(self) -> None:
        rig = self.rig
        items: list[tuple[str, str, str]] = [("Auto-detect (prefers the normal keyboard port)", AUTO, "auto")]
        for port in rig.midi.ports:
            label = f"{port.label}  [{port.kind}]"
            items.append((label, port.stable, label.lower()))
        self._push_picker("MIDI input port", items, rig.midi.preference, rig.select_midi_port)

    def action_pick_audio(self) -> None:
        rig = self.rig

        def chosen(device) -> None:
            if not rig.select_audio_device(device):
                self.notify(f"Could not open {device.label}", severity="error")

        devices = list_audio_devices()
        items = [(d.label, d, d.label.lower()) for d in devices]
        self._push_picker("Audio output", items, rig.engine.audio_device, chosen)

    def action_learn(self) -> None:
        def handle(mapping) -> None:
            if mapping is not None:
                self.notify(f"{mapping.source_label} -> {mapping.target_label}", timeout=2)

        self.app.push_screen(LearnScreen(self.rig), handle)


class KeyrigApp(App):
    TITLE = "keyrig"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; background: #14141c; }
    #status { height: 3; padding: 0 1; }
    #grid { height: 8; padding: 0 1; }
    #keys { height: 5; border: round #444455; padding: 0 1; }
    #bottom { height: 6; }
    #pads { width: 31; height: 6; border: round #444455; padding: 0 0 0 1; }
    #activity { width: 1fr; height: 6; border: round #444455; padding: 0 1; }
    #viz { height: 1fr; max-height: 24; border: round #444455; padding: 0 1; }
    #help { height: auto; padding: 0 1; color: #8888a0; }
    MainScreen.compact #keys { border: none; height: 3; }
    MainScreen.compact #help { display: none; }
    PickerScreen, SavePresetScreen, LearnScreen { align: center middle; }
    .dialog { width: 70%; height: 70%; border: round #ffb000; background: #1d1d28; padding: 0 1; }
    .dialog-small { height: 6; }
    .dialog-title { color: #ffb000; height: auto; }
    #options { height: 1fr; }
    """

    def __init__(self, rig: Rig) -> None:
        super().__init__()
        self.rig = rig

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self.rig))
