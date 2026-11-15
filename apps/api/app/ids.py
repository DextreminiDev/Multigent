"""UUIDv7 generation (RFC 9562). Time-sortable ids without a DB extension."""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7() -> uuid.UUID:
    """Return a UUIDv7. Monotonic within a process: 48-bit ms timestamp, then a
    12-bit counter (rand_a) that increments for ids minted in the same millisecond."""
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            ms = _last_ms
            _counter += 1
            if _counter > 0xFFF:  # counter overflow: borrow the next millisecond
                ms += 1
                _counter = 0
        else:
            _counter = int.from_bytes(os.urandom(2), "big") & 0x3FF  # leave headroom
        _last_ms = ms
        counter = _counter

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= counter << 64
    value |= 0b10 << 62  # variant
    value |= rand_b
    return uuid.UUID(int=value)
