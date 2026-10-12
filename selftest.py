#!/usr/bin/env python3
"""End-to-end check of the bridge, from the ALSA side.

Acts as a stand-in DAW: subscribes to the bridge's port, drives LEDs through
it, and reports what the mixer sends back. Anything this can do, REAPER can do.
"""
import json
import os
import socket
import sys
import threading
import time

import alsaseq
import layout
from smcmixerd import SOCKET_PATH


def daemon_state():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(SOCKET_PATH)
    buf = b""
    while b"\n" not in buf:
        buf += s.recv(65536)
    s.close()
    return json.loads(buf.split(b"\n", 1)[0])


def main():
    if not os.path.exists(SOCKET_PATH):
        sys.exit("bridge is not running (systemctl --user start smc-mixer)")
    st = daemon_state()
    client, port = (int(x) for x in st["alsa"].split(":"))
    print(f"bridge: {st['device']}  connected={st['connected']}  "
          f"battery={st['battery']}  ALSA {client}:{port}")
    if not st["connected"]:
        sys.exit("mixer is not connected — turn it on and retry")

    me = alsaseq.SeqPort("smc-selftest", "selftest")
    if not (me.connect_to(client, port) and me.connect_from(client, port)):
        sys.exit("could not subscribe to the bridge port")
    print(f"subscribed as ALSA {me.client}:{me.port}, both directions\n")

    received = []

    def reader():
        while True:
            data = me.recv()
            if data is None:
                break
            if data:
                received.append(bytes(data))
                print(f"  <- from mixer: {bytes(data).hex(' ')}", flush=True)

    threading.Thread(target=reader, daemon=True).start()

    print("Writing LEDs through ALSA (watch the mixer):")
    for name, base in layout.ROWS:
        print(f"  -> {name} row on")
        for c in range(layout.N_CHANNELS):
            me.send([0x90, base + c, 0x7F])
        time.sleep(1.2)
        for c in range(layout.N_CHANNELS):
            me.send([0x90, base + c, 0x00])
        time.sleep(0.3)

    print("\nMove a fader or press a button for 15s ...")
    time.sleep(15)

    print(f"\nreceived {len(received)} messages from the mixer via ALSA")
    me.close()
    print("PASS" if received else
          "no input seen — LEDs may still be fine, but check the mixer")


if __name__ == "__main__":
    main()
