#!/usr/bin/env python3
"""Tests for wrong-device-mode detection.

The SMC-Mixer has a CC-only mapping reachable by a button combo. In it nothing
matches the Mackie layout, so the panel freezes while the MIDI monitor keeps
scrolling — which reads as "the app is broken" rather than "the mixer is in the
wrong mode". These lock in the detector that tells them apart.

All the CC-mode message shapes here were captured from the hardware.
"""
import layout
from smcmixerd import Daemon


class Stub:
    """Just enough of Daemon to exercise _check_mode in isolation."""

    def __init__(self):
        self.leds = {n: False for n in layout.all_led_notes()}
        self._recent = []
        self.mode_warning = False

    def _broadcast_state(self):
        pass


def feed(msgs):
    d = Stub()
    for m in msgs:
        Daemon._check_mode(d, bytes(m))
    return d.mode_warning


CC_FADER = [[0xB0, 0x28, v % 127] for v in range(40)]
CC_BUTTONS = [[0xB0, cc, v] for _ in range(6)
              for cc in (20, 28, 36, 44) for v in (127, 0)]
MCU_FADER = [[0xE0, 0x00, v % 128] for v in range(40)]
MCU_ENCODERS = ([[0xB0, 0x10, 0x01]] * 20) + ([[0xB0, 0x11, 0x41]] * 20)
MCU_BUTTONS = [[0x90, n, v] for _ in range(3)
               for n in (0x00, 0x0A, 0x14, 0x1C, 0x5E, 0x2E) for v in (0x7F, 0)]
MCU_MIXED = [m for _ in range(14) for m in
             ([0xE0, 0x00, 0x40], [0xB0, 0x10, 0x01], [0x90, 0x0A, 0x7F])]

CASES = [
    ("CC mode: fader as CC 40", CC_FADER, True),
    ("CC mode: buttons as CC 20/28/36/44", CC_BUTTONS, True),
    ("CC mode: mixed", CC_FADER[:20] + CC_BUTTONS[:24], True),
    ("Mackie: fader sweeps", MCU_FADER, False),
    ("Mackie: encoder ticks", MCU_ENCODERS, False),
    ("Mackie: button presses", MCU_BUTTONS, False),
    ("Mackie: realistic mixed use", MCU_MIXED, False),
    # Too little traffic to judge — must not warn on a couple of stray bytes.
    ("quiet: 6 unmapped messages", CC_FADER[:6], False),
]


def run():
    failures = 0
    for name, msgs, expected in CASES:
        got = feed(msgs)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {name:<40} warned={got}")

    # Recovery: warn in CC mode, then clear once Mackie traffic returns.
    d = Stub()
    for m in CC_FADER:
        Daemon._check_mode(d, bytes(m))
    warned = d.mode_warning
    for m in MCU_FADER:
        Daemon._check_mode(d, bytes(m))
    ok = warned and not d.mode_warning
    failures += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} recovery after switching back to Mackie")

    print("\nFAILURES:", failures)
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if run() else 0)
