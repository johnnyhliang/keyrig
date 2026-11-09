"""Keeps Python's own pauses away from played notes.

FluidSynth renders audio in native code, but every note passes through Python, so it
competes with the TUI for the interpreter. Two things made notes late while the screen
drew: the default 5 ms switch interval, and full garbage collections of about 30 ms
that nothing can interrupt, running about three times a second.
"""

from __future__ import annotations

import functools
import gc
import sys

from textual.strip import Strip

SWITCH_INTERVAL_SECONDS = 0.0002
FULL_COLLECTION_THRESHOLD = 100  # the default is 10


def _stop_caching_self(method_name: str, cache_name: str, returned_self) -> None:
    original = getattr(Strip, method_name)
    if getattr(original, "_keyrig_patched", False):
        return

    @functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if returned_self(self, result):
            cache = getattr(self, cache_name, None)
            if cache is not None:
                cache.clear()
        return result

    wrapper._keyrig_patched = True
    setattr(Strip, method_name, wrapper)


def stop_strip_reference_cycles() -> None:
    """Stop Textual's Strip from storing itself in its own caches.

    When divide() or crop_extend() has nothing to do, the result is the strip itself,
    and caching that makes a cycle. It happens for almost every line of every frame, so
    each drawn cell stayed alive until a full collection: about 24,000 objects a second.
    Those two results cost nothing to recompute.
    """
    _stop_caching_self("divide", "_divide_cache", lambda strip, result: len(result) == 1 and result[0] is strip)
    _stop_caching_self("crop_extend", "_crop_extend_cache", lambda strip, result: result is strip)


def prepare_for_tui() -> None:
    """Give notes priority over drawing. Call before the app starts."""
    sys.setswitchinterval(SWITCH_INTERVAL_SECONDS)  # the default is 5 ms
    stop_strip_reference_cycles()


def settle_garbage_collector() -> None:
    """Call once the UI is built: makes full collections shorter and rarer.

    Freezing excludes everything built at startup from later scans. Full collections
    then run about every 3 s instead of three times a second, since drawing no longer
    leaves cycles for them to find. They stay automatic, so memory is bounded even if a
    future Textual brings the cycles back. Small collections keep their default size:
    raising it made each one take up to 11 ms.
    """
    gc.collect()
    gc.freeze()
    young, middle, _ = gc.get_threshold()
    gc.set_threshold(young, middle, FULL_COLLECTION_THRESHOLD)
