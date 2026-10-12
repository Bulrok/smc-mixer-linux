#!/usr/bin/env python3
"""Unit tests for the BLE-MIDI packet codec.

Every packet here is real traffic captured from the SMC-Mixer, so these lock in
the multi-message and running-status cases that are easy to get wrong.
"""
import blemidi

CASES = [
    # (raw packet, expected messages)
    ("80 80 90 5e 7f", [(0x90, 0x5E, 0x7F)]),
    ("80 80 90 5e 00", [(0x90, 0x5E, 0x00)]),
    ("80 80 e0 00 4a", [(0xE0, 0x00, 0x4A)]),
    ("80 80 e2 00 50", [(0xE2, 0x00, 0x50)]),
    # Two pitch bends sharing one timestamp, second via running status.
    ("80 80 e0 00 2e 00 30", [(0xE0, 0x00, 0x2E), (0xE0, 0x00, 0x30)]),
    # Three in one packet.
    ("80 80 e0 00 3e 00 41 00 43",
     [(0xE0, 0x00, 0x3E), (0xE0, 0x00, 0x41), (0xE0, 0x00, 0x43)]),
    # Encoder: relative CC.
    ("80 80 b0 10 01", [(0xB0, 0x10, 0x01)]),
    ("80 80 b0 10 41", [(0xB0, 0x10, 0x41)]),
    # A fresh timestamp before each of two different statuses.
    ("80 a5 90 14 7f a6 90 14 00", [(0x90, 0x14, 0x7F), (0x90, 0x14, 0x00)]),
    # Running status with an intervening timestamp byte whose low bits
    # happen to look like a status byte (0xb0 here) — it must be treated as a
    # timestamp, not as a Control Change.
    ("80 a5 90 14 7f b0 14 00", [(0x90, 0x14, 0x7F), (0x90, 0x14, 0x00)]),
]


def run():
    failures = 0
    for hexs, expected in CASES:
        raw = bytes.fromhex(hexs.replace(" ", ""))
        got = [tuple(m) for m in blemidi.decode(raw)]
        ok = got == [tuple(e) for e in expected]
        if not ok:
            failures += 1
        print(f"{'ok  ' if ok else 'FAIL'} {hexs}")
        if not ok:
            print(f"       expected {[tuple(e) for e in expected]}")
            print(f"       got      {got}")

    # Round trip: encode -> decode must be lossless.
    msgs = [bytes([0x90, 0x0A, 0x7F]), bytes([0xE3, 0x00, 0x40]),
            bytes([0xB0, 0x11, 0x41])]
    back = [tuple(m) for m in blemidi.decode(blemidi.encode(msgs))]
    ok = back == [tuple(m) for m in msgs]
    failures += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} encode/decode round trip")

    print("\nFAILURES:", failures)
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if run() else 0)
