#!/usr/bin/env python3
"""smcmixerd — bridges the M-VAVE SMC-Mixer onto ALSA, and serves the UI.

The bridge is the long-lived half: it owns the BLE link, the virtual MIDI port
and the authoritative device state. The UI is a detachable client over a unix
socket, so closing the window never drops the DAW's MIDI port.

  device  --BLE-->  daemon  --ALSA seq-->  REAPER / anything
  device  <--BLE--  daemon  <--ALSA seq--  (LED feedback from the host)
                       |
                       +--unix socket--> UI clients (live preview, testing)
"""
import json
import os
import socket
import subprocess
import sys
import threading

import gi
from gi.repository import GLib

import alsaseq
import blemidi
import layout

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCKET_PATH = os.path.join(RUNTIME, "smc-mixer.sock")
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "smc-mixer")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "client_name": "SMC-Mixer",
    "port_name": "SMC-Mixer MIDI",
    "address": None,             # None = autodetect a BLE-MIDI device ...
    "name_filter": "mixer",      # ... whose name contains this (SMC-PAD lives here too)
    "auto_launch_ui": True,      # open the UI when the mixer connects
    "auto_connect": True,        # ask BlueZ to reconnect a paired device
    "local_led_echo": False,     # light LEDs from button presses, no host needed
    "monitor_limit": 200,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


class Daemon:
    def __init__(self):
        self.cfg = load_config()
        self.clients = []
        self.monitor = []

        # Authoritative device state. LEDs have no readback, so what we last
        # sent is the only truth there is.
        self.faders = [0] * layout.N_CHANNELS
        self.encoders = [0] * layout.N_CHANNELS
        self.leds = {n: False for n in layout.all_led_notes()}
        self.buttons = {n: False for n in layout.all_led_notes()}

        self.seq = alsaseq.SeqPort(self.cfg["client_name"], self.cfg["port_name"])
        self.dev = blemidi.BleMidiDevice(
            address=self.cfg["address"],
            name_filter=self.cfg.get("name_filter"),
            on_midi=self._from_device,
            on_state=lambda: GLib.idle_add(self._broadcast_state))
        self._was_attached = False
        self.mode_warning = False
        self._recent = []
        self._out_queue = []
        self._flush_scheduled = False

        threading.Thread(target=self._alsa_loop, daemon=True).start()
        self._serve()

        self.dev.find_device()
        if self.cfg["auto_connect"] and self.dev.dev_path and not self.dev.connected:
            self.dev.connect_device()
        GLib.idle_add(self._try_attach)
        GLib.timeout_add_seconds(5, self._watchdog)

    # ---------------------------------------------------------------- device

    def _try_attach(self):
        if self.dev.attach():
            self._on_attached()
        return False

    def _watchdog(self):
        """Re-attach after the mixer sleeps, disconnects or drifts out of range."""
        # Reconcile against BlueZ rather than trusting our cached proxy — a
        # mixer that powered off mid-session would otherwise keep reporting
        # itself connected, and the UI would show a live panel for dead
        # hardware.
        if not self.dev.refresh_connected() and self.dev.char is not None:
            self.dev.detach()
        if self.dev.char is None:
            if self.cfg["auto_connect"] and self.dev.dev_path \
                    and not self.dev.connected:
                self.dev.connect_device()
            if self.dev.attach():
                self._on_attached()
        return True

    def _on_attached(self):
        if self._was_attached:
            return
        self._was_attached = True
        self._broadcast_state()
        if not self.cfg["auto_launch_ui"]:
            return

        def launch_if_still_needed():
            # Give an already-open window a moment to reconnect first — on a
            # bridge restart its client is simply not back yet.
            if not self.clients:
                self.launch_ui()
            return False
        GLib.timeout_add(2000, launch_if_still_needed)

    def launch_ui(self):
        """Open the UI in its own transient unit.

        Not as a plain child: it would land in this service's cgroup, and then
        restarting the bridge would either kill the window or leave systemd
        complaining about stray processes.
        """
        ui = os.path.join(HERE, "ui.py")
        env = _session_env()
        cmd = ["systemd-run", "--user", "--quiet", "--collect",
               "--unit=smc-mixer-ui", sys.executable, ui]
        try:
            if subprocess.call(cmd, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0:
                return
        except OSError:
            pass
        try:
            subprocess.Popen([sys.executable, ui], start_new_session=True,
                             env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as e:
            print(f"could not launch UI: {e}", file=sys.stderr)

    def _from_device(self, m, raw):
        """A MIDI message arrived over BLE: update state, pass it to ALSA."""
        self._apply_incoming(m)

    def _apply_incoming(self, m, echo_led=True):
        """Handle inbound control data, whether real or simulated by the UI."""
        self.seq.send(m)
        self._log_midi("in", m)
        self._check_mode(m)

        st, ch = m[0] & 0xF0, m[0] & 0x0F
        if st == blemidi.PITCH and ch < layout.N_CHANNELS:
            self.faders[ch] = (m[2] << 7) | m[1]
            self._broadcast({"t": "ev", "kind": "fader", "i": ch,
                             "v": self.faders[ch]})
        elif st == blemidi.CC and layout.CC_ENCODER_BASE <= m[1] < \
                layout.CC_ENCODER_BASE + layout.N_CHANNELS:
            i = m[1] - layout.CC_ENCODER_BASE
            d = layout.encoder_delta(m[2])
            self.encoders[i] = max(0, min(127, self.encoders[i] + d))
            self._broadcast({"t": "ev", "kind": "encoder", "i": i, "d": d,
                             "v": self.encoders[i]})
        elif st == blemidi.NOTE_ON:
            down = m[2] > 0
            self.buttons[m[1]] = down
            self._broadcast({"t": "ev", "kind": "button", "note": m[1],
                             "down": down})
            if echo_led and self.cfg["local_led_echo"] and down:
                self.set_led(m[1], not self.leds.get(m[1], False))

    def _check_mode(self, m):
        """Warn when the mixer is sending MIDI this layout cannot interpret.

        The SMC-Mixer has a second, CC-only mapping reachable by a button
        combo. In it every control sends Control Change, nothing matches the
        Mackie layout, and the panel just sits there looking broken while the
        MIDI monitor scrolls happily. Say so instead.
        """
        st, ch = m[0] & 0xF0, m[0] & 0x0F
        mapped = (
            (st == blemidi.PITCH and ch < layout.N_CHANNELS)
            # An encoder tick is a signed delta, so a value of zero is not one.
            # That distinction matters: in CC mode a button press lands on
            # CC 20, right inside the encoder range, and would otherwise look
            # legitimate.
            or (st == blemidi.CC and layout.CC_ENCODER_BASE <= m[1]
                < layout.CC_ENCODER_BASE + layout.N_CHANNELS
                and layout.encoder_delta(m[2]) != 0)
            or (st in (blemidi.NOTE_ON, blemidi.NOTE_OFF) and m[1] in self.leds)
        )
        # Judge on the ratio over a window rather than a streak — in CC mode a
        # minority of messages still collide with the Mackie layout, and a
        # streak counter would reset on each one and never trip.
        self._recent.append(mapped)
        if len(self._recent) > 40:
            del self._recent[:-40]
        if len(self._recent) < 20:
            return
        ratio = sum(self._recent) / len(self._recent)
        if ratio < 0.25 and not self.mode_warning:
            self.mode_warning = True
            print("mixer is sending unrecognised MIDI — wrong device mode?",
                  file=sys.stderr, flush=True)
            self._broadcast_state()
        elif ratio > 0.5 and self.mode_warning:
            self.mode_warning = False
            self._broadcast_state()

    # ------------------------------------------------------------------ ALSA

    def _alsa_loop(self):
        """Host -> device. Runs on a thread; ctypes frees the GIL while blocked."""
        while True:
            data = self.seq.recv()
            if data is None:
                break
            if data:
                GLib.idle_add(self._from_host, bytes(data))

    def _from_host(self, data):
        for m in _split_midi(data):
            st = m[0] & 0xF0
            if st in (blemidi.NOTE_ON, blemidi.NOTE_OFF):
                on = st == blemidi.NOTE_ON and m[2] > 0
                if m[1] in self.leds:
                    self.leds[m[1]] = on
                    self._broadcast({"t": "ev", "kind": "led", "note": m[1],
                                     "on": on})
            self._queue(m)
            self._log_midi("out", m)
        return False

    # ---------------------------------------------------------------- output

    def _queue(self, m):
        """Batch device writes — a 32-LED chase becomes one BLE packet."""
        self._out_queue.append(bytes(m))
        if not self._flush_scheduled:
            self._flush_scheduled = True
            GLib.timeout_add(12, self._flush)

    def _flush(self):
        self._flush_scheduled = False
        if self._out_queue:
            batch, self._out_queue = self._out_queue, []
            self.dev.send(batch)
        return False

    def set_led(self, note, on):
        self.leds[note] = on
        self._queue([0x90, note, 0x7F if on else 0x00])
        self._broadcast({"t": "ev", "kind": "led", "note": note, "on": on})

    def set_leds(self, notes, on):
        for n in notes:
            self.leds[n] = on
            self._queue([0x90, n, 0x7F if on else 0x00])
        self._broadcast({"t": "ev", "kind": "leds",
                         "notes": list(notes), "on": on})

    # ---------------------------------------------------------------- clients

    def _serve(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(SOCKET_PATH)
        self.srv.listen(8)
        self.srv.setblocking(False)
        alsaseq.fd_watch(self.srv.fileno(), GLib.IOCondition.IN,
                         self._accept, None)

    def _accept(self, fd, cond, _data):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return True
        conn.setblocking(False)
        client = {"sock": conn, "buf": b""}
        self.clients.append(client)
        alsaseq.fd_watch(conn.fileno(),
                         GLib.IOCondition.IN | GLib.IOCondition.HUP,
                         self._client_ready, client)
        self._send_to(client, self._state_msg())
        for entry in self.monitor[-60:]:
            self._send_to(client, entry)
        return True

    def _client_ready(self, fd, cond, client):
        if cond & GLib.IOCondition.HUP:
            self._drop(client)
            return False
        try:
            chunk = client["sock"].recv(65536)
        except OSError:
            self._drop(client)
            return False
        if not chunk:
            self._drop(client)
            return False
        client["buf"] += chunk
        while b"\n" in client["buf"]:
            line, client["buf"] = client["buf"].split(b"\n", 1)
            if line.strip():
                try:
                    self._command(json.loads(line))
                except (ValueError, KeyError, TypeError) as e:
                    print(f"bad command: {e}", file=sys.stderr)
        return True

    def _drop(self, client):
        if client in self.clients:
            self.clients.remove(client)
        try:
            client["sock"].close()
        except OSError:
            pass

    def _command(self, msg):
        c = msg.get("c")
        if c == "led":
            self.set_led(int(msg["note"]), bool(msg["on"]))
        elif c == "leds":
            notes = msg.get("notes") or layout.all_led_notes()
            self.set_leds([int(n) for n in notes], bool(msg["on"]))
        elif c == "send":                       # raw MIDI to the device
            self._queue(bytes(msg["b"]))
        elif c == "emit":                       # pretend the device sent this
            self._apply_incoming(bytes(msg["b"]), echo_led=False)
        elif c == "chase":
            self._chase()
        elif c == "config":
            self.cfg[msg["k"]] = msg["v"]
            save_config(self.cfg)
            self._broadcast_state()
        elif c == "connect":
            self.dev.connect_device()
        elif c == "refresh":
            self._broadcast_state()

    def _chase(self):
        """Walk one LED across every button, then clear. Pure visual test."""
        notes = layout.all_led_notes()
        self.set_leds(notes, False)
        state = {"i": 0}

        def step():
            if state["i"] >= len(notes):
                self.set_leds(notes, False)
                return False
            if state["i"] > 0:
                self.set_led(notes[state["i"] - 1], False)
            self.set_led(notes[state["i"]], True)
            state["i"] += 1
            return True
        GLib.timeout_add(60, step)

    # ------------------------------------------------------------- broadcast

    def _state_msg(self):
        return {
            "t": "state",
            "connected": self.dev.char is not None,
            "battery": (self.dev.battery if self.dev.battery is not None
                        else self.dev.read_battery()),
            "device": self.dev.name or "no device",
            "address": self.dev.address,
            "alsa": f"{self.seq.client}:{self.seq.port}",
            "alsa_name": f"{self.cfg['client_name']} / {self.cfg['port_name']}",
            "clients": len(self.clients),
            "mode_warning": self.mode_warning,
            "faders": self.faders,
            "encoders": self.encoders,
            "leds": {str(k): v for k, v in self.leds.items()},
            "config": self.cfg,
        }

    def _broadcast_state(self):
        # Every detach path funnels through here, so this is the one reliable
        # place to re-arm auto-launch for the next time the mixer wakes up.
        if self.dev.char is None:
            self._was_attached = False
        self._broadcast(self._state_msg())
        return False

    def _log_midi(self, direction, m):
        entry = {"t": "midi", "dir": direction, "b": list(m),
                 "desc": blemidi.describe(bytes(m))}
        self.monitor.append(entry)
        if len(self.monitor) > self.cfg["monitor_limit"]:
            del self.monitor[:-self.cfg["monitor_limit"]]
        self._broadcast(entry)

    def _broadcast(self, msg):
        if not self.clients:
            return
        for client in list(self.clients):
            self._send_to(client, msg)

    def _send_to(self, client, msg):
        try:
            client["sock"].sendall((json.dumps(msg) + "\n").encode())
        except OSError:
            self._drop(client)


def _session_env():
    """Environment for spawning the GUI.

    A user service can start before gnome-session imports the display
    variables, so pull the current set from the systemd user manager rather
    than trusting whatever we inherited at startup.
    """
    env = dict(os.environ)
    if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
        return env
    try:
        out = subprocess.check_output(["systemctl", "--user", "show-environment"],
                                      text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return env
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR",
                     "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "XDG_SESSION_TYPE"):
                env[k] = v
    return env


def _split_midi(data):
    """Split a raw byte run into individual channel-voice messages."""
    out, i, status = [], 0, None
    while i < len(data):
        if data[i] & 0x80:
            status = data[i]
            i += 1
        if status is None or status >= 0xF0:
            i += 1
            continue
        n = 1 if (status & 0xF0) in (blemidi.PROGRAM, blemidi.AFTERTOUCH) else 2
        if i + n > len(data):
            break
        out.append(bytes([status]) + bytes(data[i:i + n]))
        i += n
    return out


def main():
    d = Daemon()
    print(f"smcmixerd: ALSA {d.seq.client}:{d.seq.port}  socket {SOCKET_PATH}",
          flush=True)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    finally:
        d.seq.close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    main()
