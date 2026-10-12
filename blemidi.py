#!/usr/bin/env python3
"""BLE-MIDI transport over the BlueZ D-Bus API.

Fedora's BlueZ is built without the MIDI plugin, so a BLE-MIDI device connects
but never gets an ALSA sequencer port. This module talks to the standard
BLE-MIDI GATT characteristic directly instead.

  service         03b80e5a-ede8-4b33-a751-6ce34ec4c700
  characteristic  7772e5db-3868-4112-a1a9-f2669d106bf3  (notify + write-no-rsp)

Protocol on the M-VAVE SMC-Mixer is plain Mackie Control — see layout.py.
"""
import time

import dbus
import dbus.mainloop.glib

MIDI_SERVICE_UUID = "03b80e5a-ede8-4b33-a751-6ce34ec4c700"
MIDI_CHAR_UUID = "7772e5db-3868-4112-a1a9-f2669d106bf3"

NOTE_OFF, NOTE_ON, CC, PROGRAM, AFTERTOUCH, PITCH = 0x80, 0x90, 0xB0, 0xC0, 0xD0, 0xE0

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
DEV_IFACE = "org.bluez.Device1"
CHAR_IFACE = "org.bluez.GattCharacteristic1"


# ---------------------------------------------------------------- packet codec

def decode(data):
    """Decode one BLE-MIDI packet into a list of MIDI messages.

    Layout is header(1) then [timestamp(1) status data...]..., with running
    status permitted between timestamped events. Header and timestamp bytes
    both have bit 7 set, which is how they are told apart from MIDI data.
    """
    if len(data) < 2:
        return []
    msgs = []
    i = 1  # skip header
    status = None
    while i < len(data):
        if data[i] & 0x80:  # timestamp byte
            i += 1
            if i >= len(data):
                break
        if data[i] & 0x80:  # new status byte
            status = data[i]
            i += 1
        if status is None or status >= 0xF0:
            i += 1
            continue
        n = 1 if (status & 0xF0) in (PROGRAM, AFTERTOUCH) else 2
        if i + n > len(data):
            break
        msgs.append(bytes([status]) + bytes(data[i:i + n]))
        i += n
    return msgs


def encode(messages, timestamp=None):
    """Pack MIDI messages into one BLE-MIDI packet.

    Batching matters: an LED chase across 32 notes is one write instead of 32,
    which is the difference between instant and visibly crawling over BLE.
    """
    if isinstance(messages, (bytes, bytearray)) or (
            messages and isinstance(messages[0], int)):
        messages = [messages]
    ts = int(time.monotonic() * 1000) & 0x1FFF if timestamp is None else timestamp & 0x1FFF
    out = bytearray([0x80 | ((ts >> 7) & 0x3F)])
    for m in messages:
        out.append(0x80 | (ts & 0x7F))
        out.extend(bytes(m))
    return bytes(out)


def split_for_mtu(messages, mtu):
    """Chunk messages so each encoded packet fits one ATT write."""
    payload = max(mtu - 3, 20)
    chunks, cur, size = [], [], 1
    for m in messages:
        need = 1 + len(m)
        if cur and size + need > payload:
            chunks.append(cur)
            cur, size = [], 1
        cur.append(m)
        size += need
    if cur:
        chunks.append(cur)
    return chunks


def describe(m):
    st, ch = m[0] & 0xF0, (m[0] & 0x0F) + 1
    if st == PITCH:
        return f"PitchBend ch{ch} value={(m[2] << 7) | m[1]}"
    if st == NOTE_ON:
        return f"NoteOn ch{ch} note=0x{m[1]:02X} vel={m[2]}"
    if st == NOTE_OFF:
        return f"NoteOff ch{ch} note=0x{m[1]:02X} vel={m[2]}"
    if st == CC:
        return f"CC ch{ch} cc={m[1]} value={m[2]}"
    return "raw " + m.hex(" ")


# ---------------------------------------------------------------- BlueZ device

class BleMidiDevice:
    """Tracks one BLE-MIDI device: connection, notifications, writes.

    Handles the device vanishing and coming back — BlueZ rebuilds the GATT
    object paths on reconnect, so the characteristic is re-resolved each time
    rather than cached across sessions.
    """

    def __init__(self, address=None, on_midi=None, on_state=None,
                 name_filter=None):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.address = address
        # Autodetect used to take the first BLE-MIDI device BlueZ knew about.
        # With an M-VAVE SMC-PAD paired on the same machine that was the pad,
        # and the mixer bridge happily streamed drum hits out of its port. In
        # autodetect mode only devices whose name contains this (case-
        # insensitive) are considered; an explicit address still wins.
        self.name_filter = name_filter
        self.on_midi = on_midi
        self.on_state = on_state
        self.dev_path = None
        self.char = None
        self.char_path = None
        self.mtu = 20
        self.connected = False
        self.battery = None
        self.name = None
        self._notify_sig = None

        self.bus.add_signal_receiver(
            self._props_changed, dbus_interface=PROPS_IFACE,
            signal_name="PropertiesChanged", path_keyword="path",
            arg0=DEV_IFACE)
        self.bus.add_signal_receiver(
            self._props_changed, dbus_interface=PROPS_IFACE,
            signal_name="PropertiesChanged", path_keyword="path",
            arg0="org.bluez.Battery1")
        self.bus.add_signal_receiver(
            self._ifaces_added, dbus_interface=OM_IFACE,
            signal_name="InterfacesAdded")

    # -- discovery -----------------------------------------------------------

    def _objects(self):
        om = dbus.Interface(self.bus.get_object(BLUEZ, "/"), OM_IFACE)
        return om.GetManagedObjects()

    def find_device(self):
        """Locate a BLE-MIDI device, by address if given else by service UUID."""
        for path, ifaces in self._objects().items():
            dev = ifaces.get(DEV_IFACE)
            if not dev:
                continue
            if self.address:
                if str(dev.get("Address", "")).upper() != self.address.upper():
                    continue
            elif MIDI_SERVICE_UUID not in [str(u).lower()
                                           for u in dev.get("UUIDs", [])]:
                continue
            elif self.name_filter and self.name_filter.lower() not in \
                    str(dev.get("Name", "")).lower():
                continue
            self.dev_path = path
            self.address = str(dev.get("Address", ""))
            self.name = str(dev.get("Name", "BLE MIDI"))
            self.connected = bool(dev.get("Connected", False))
            self.battery = ifaces.get("org.bluez.Battery1", {}).get("Percentage")
            if self.battery is not None:
                self.battery = int(self.battery)
            return path
        return None

    def _find_char(self):
        if not self.dev_path:
            return None
        for path, ifaces in self._objects().items():
            c = ifaces.get(CHAR_IFACE)
            if c and path.startswith(self.dev_path) and \
                    str(c.get("UUID", "")).lower() == MIDI_CHAR_UUID:
                self.mtu = int(c.get("MTU", 20) or 20)
                return path
        return None

    # -- attach / detach -----------------------------------------------------

    def attach(self):
        """Resolve the characteristic and subscribe. True if now streaming."""
        if not self.dev_path and not self.find_device():
            return False
        if self.char is not None:
            return True
        path = self._find_char()
        if not path:
            return False
        self.char_path = path
        self.char = dbus.Interface(self.bus.get_object(BLUEZ, path), CHAR_IFACE)
        try:
            self.char.StartNotify()
        except dbus.DBusException as e:
            if "InProgress" not in str(e) and "Already" not in str(e):
                self.char = None
                return False
        self._notify_sig = self.bus.add_signal_receiver(
            self._value_changed, dbus_interface=PROPS_IFACE,
            signal_name="PropertiesChanged", path=path, arg0=CHAR_IFACE)
        self.connected = True
        self._emit_state()
        return True

    def detach(self):
        if self._notify_sig:
            self._notify_sig.remove()
            self._notify_sig = None
        self.char = None
        self.char_path = None
        self._emit_state()

    def refresh_connected(self):
        """Ask BlueZ whether the link is actually up.

        A cached characteristic proxy outlives the connection, so trusting it
        can leave us reporting a healthy mixer long after it has powered off.
        """
        if not self.dev_path:
            return False
        try:
            props = dbus.Interface(self.bus.get_object(BLUEZ, self.dev_path),
                                   PROPS_IFACE)
            self.connected = bool(props.Get(DEV_IFACE, "Connected"))
        except dbus.DBusException:
            self.connected = False
        return self.connected

    def read_battery(self):
        """Re-read the battery level.

        BlueZ may not have the Battery1 interface cached when we first look,
        so the initial find() can come back empty even on a healthy link.
        """
        if not self.dev_path:
            return self.battery
        try:
            props = dbus.Interface(self.bus.get_object(BLUEZ, self.dev_path),
                                   PROPS_IFACE)
            self.battery = int(props.Get("org.bluez.Battery1", "Percentage"))
        except dbus.DBusException:
            pass
        return self.battery

    def connect_device(self):
        """Ask BlueZ to bring the link up (device must already be paired)."""
        if not self.dev_path and not self.find_device():
            return False
        try:
            dbus.Interface(self.bus.get_object(BLUEZ, self.dev_path),
                           DEV_IFACE).Connect()
            return True
        except dbus.DBusException:
            return False

    # -- signals -------------------------------------------------------------

    def _value_changed(self, iface, changed, invalidated):
        if "Value" not in changed or not self.on_midi:
            return
        data = bytes(bytearray(changed["Value"]))
        for m in decode(data):
            self.on_midi(m, data)

    def _props_changed(self, iface, changed, invalidated, path=None):
        if path != self.dev_path:
            return
        if "Connected" in changed:
            up = bool(changed["Connected"])
            if up:
                # GATT resolution lags the connect event; retry briefly.
                from gi.repository import GLib
                self._retries = 0

                def try_attach():
                    self._retries += 1
                    if self.attach():
                        return False
                    return self._retries < 15
                GLib.timeout_add(500, try_attach)
            else:
                self.connected = False
                self.detach()
        if "Percentage" in changed:
            self.battery = int(changed["Percentage"])
            self._emit_state()

    def _ifaces_added(self, path, ifaces):
        if self.dev_path is None and DEV_IFACE in ifaces:
            self.find_device()
        if CHAR_IFACE in ifaces and self.dev_path and path.startswith(self.dev_path):
            self.attach()

    def _emit_state(self):
        if self.on_state:
            self.on_state()

    # -- output --------------------------------------------------------------

    def send(self, messages):
        """Write MIDI messages to the device, batched into MTU-sized packets."""
        if self.char is None:
            return False
        if isinstance(messages, (bytes, bytearray)) or (
                messages and isinstance(messages[0], int)):
            messages = [messages]
        try:
            for chunk in split_for_mtu(messages, self.mtu):
                pkt = encode(chunk)
                self.char.WriteValue(
                    dbus.Array([dbus.Byte(b) for b in pkt], signature="y"),
                    dbus.Dictionary({"type": "command"}, signature="sv"))
            return True
        except dbus.DBusException:
            self.detach()
            return False


def listen(seconds, on_msg=None, quiet=False, address=None):
    """Standalone helper: print/collect MIDI for a while. Used by survey.py."""
    from gi.repository import GLib

    def handle(m, raw):
        if not quiet:
            print(f"  [{raw.hex(' ')}]  {describe(m)}", flush=True)
        if on_msg:
            on_msg(m)

    dev = BleMidiDevice(address=address, on_midi=handle)
    if not dev.attach():
        print("could not attach to a BLE-MIDI device", flush=True)
        return
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(seconds, loop.quit)
    print(f"Listening on {dev.name} for {seconds}s ...", flush=True)
    loop.run()
    dev.detach()


if __name__ == "__main__":
    import sys
    listen(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
