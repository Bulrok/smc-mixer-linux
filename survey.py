#!/usr/bin/env python3
"""Record every distinct control the SMC-Mixer emits, to map the hardware."""
import json
import sys
import blemidi

faders = {}
notes = {}
ccs = {}
order = []


def on_msg(m):
    st, ch = m[0] & 0xF0, m[0] & 0x0F
    if st == blemidi.PITCH:
        val = (m[2] << 7) | m[1]
        if ch not in faders:
            order.append(f"fader ch{ch + 1}")
        f = faders.setdefault(ch, {"min": val, "max": val, "count": 0})
        f["min"] = min(f["min"], val)
        f["max"] = max(f["max"], val)
        f["count"] += 1
    elif st == blemidi.NOTE_ON:
        key = (ch, m[1])
        if m[2] and key not in notes:
            order.append(f"note ch{ch + 1} 0x{m[1]:02X}")
        n = notes.setdefault(key, {"presses": 0})
        if m[2]:
            n["presses"] += 1
    elif st == blemidi.CC:
        key = (ch, m[1])
        if key not in ccs:
            order.append(f"cc ch{ch + 1} #{m[1]}")
        c = ccs.setdefault(key, {"min": m[2], "max": m[2], "count": 0})
        c["min"] = min(c["min"], m[2])
        c["max"] = max(c["max"], m[2])
        c["count"] += 1


secs = int(sys.argv[1]) if len(sys.argv) > 1 else 120
blemidi.listen(secs, on_msg=on_msg, quiet=True)

out = {
    "faders": {f"ch{k + 1}": v for k, v in sorted(faders.items())},
    "notes": {f"ch{k[0] + 1}/0x{k[1]:02X}": v for k, v in sorted(notes.items())},
    "ccs": {f"ch{k[0] + 1}/cc{k[1]}": v for k, v in sorted(ccs.items())},
    "first_seen_order": order,
}
print("\n===== SURVEY =====")
print(json.dumps(out, indent=2))
with open("survey.json", "w") as fh:
    json.dump(out, fh, indent=2)
