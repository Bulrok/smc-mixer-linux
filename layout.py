#!/usr/bin/env python3
"""Physical map of the M-VAVE SMC-Mixer, derived by surveying the hardware.

Everything here was observed from the device (see survey.py), not assumed from
the Mackie Control spec — though it turned out to match MCU exactly.

  8 faders     PitchBend on MIDI channels 1-8. The device sends 14-bit values
               but only ever sets the MSB, so there are 128 real steps.
  8 encoders   CC 16-23 on ch1, relative: 0x01-0x3F = n ticks clockwise,
               0x41-0x7F = n ticks counter-clockwise. No LED rings.
  32 buttons   NoteOn ch1 in four rows of eight, each with an LED that is lit
               by sending the same note back.
  transport    NoteOn ch1, also LED-backed.
"""

N_CHANNELS = 8

FADER_MAX = 16256          # 127 << 7 — the device's true full-scale
FADER_STEP = 128           # LSB is always zero

CC_ENCODER_BASE = 16       # ch1 CC16..CC23

# Button/LED note bases for the four channel rows.
ROWS = [
    ("REC", 0x00),
    ("SOLO", 0x08),
    ("MUTE", 0x10),
    ("SELECT", 0x18),
]

BANK = [
    ("Bank <", 0x2E),
    ("Bank >", 0x2F),
]

TRANSPORT = [
    ("Rew", 0x5B),
    ("Fwd", 0x5C),
    ("Stop", 0x5D),
    ("Play", 0x5E),
    ("Rec", 0x5F),
]

NAV = [
    ("Up", 0x60),
    ("Down", 0x61),
    ("Left", 0x62),
    ("Right", 0x63),
]


def channel_notes():
    """All 32 channel-strip note numbers."""
    return [base + c for _, base in ROWS for c in range(N_CHANNELS)]


def all_led_notes():
    """Every note that drives an LED."""
    return (channel_notes()
            + [n for _, n in BANK]
            + [n for _, n in TRANSPORT]
            + [n for _, n in NAV])


def note_name(note):
    for name, base in ROWS:
        if base <= note < base + N_CHANNELS:
            return f"{name} {note - base + 1}"
    for label, n in BANK + TRANSPORT + NAV:
        if n == note:
            return label
    return f"Note 0x{note:02X}"


def encoder_delta(value):
    """Decode a relative-encoder CC value into a signed tick count."""
    return -(value & 0x3F) if value & 0x40 else (value & 0x3F)
