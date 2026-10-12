#!/usr/bin/env python3
"""A virtual ALSA sequencer MIDI port, via ctypes on libasound.

Fedora has no python3-rtmidi package, and pulling in a build toolchain for one
is not worth it — the sequencer API we need is small enough to bind directly.
Creates one duplex port so the device looks like ordinary MIDI hardware to
REAPER, aconnect and PipeWire.
"""
import ctypes
import ctypes.util
import threading

_lib = ctypes.CDLL(ctypes.util.find_library("asound") or "libasound.so.2")

SND_SEQ_OPEN_DUPLEX = 3
SND_SEQ_PORT_CAP_READ = 1 << 0
SND_SEQ_PORT_CAP_WRITE = 1 << 1
SND_SEQ_PORT_CAP_SUBS_READ = 1 << 5
SND_SEQ_PORT_CAP_SUBS_WRITE = 1 << 6
SND_SEQ_PORT_TYPE_MIDI_GENERIC = 1 << 1
SND_SEQ_PORT_TYPE_APPLICATION = 1 << 20
SND_SEQ_QUEUE_DIRECT = 253
SND_SEQ_ADDRESS_UNKNOWN = 253
SND_SEQ_ADDRESS_SUBSCRIBERS = 254


class _Addr(ctypes.Structure):
    _fields_ = [("client", ctypes.c_ubyte), ("port", ctypes.c_ubyte)]


class _RealTime(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_uint), ("tv_nsec", ctypes.c_uint)]


class _Time(ctypes.Union):
    _fields_ = [("tick", ctypes.c_uint), ("time", _RealTime)]


class _Data(ctypes.Union):
    _fields_ = [("raw8", ctypes.c_ubyte * 12), ("raw32", ctypes.c_uint * 3)]


class SndSeqEvent(ctypes.Structure):
    """snd_seq_event_t — 28 bytes on LP64."""
    _fields_ = [
        ("type", ctypes.c_ubyte),
        ("flags", ctypes.c_ubyte),
        ("tag", ctypes.c_byte),
        ("queue", ctypes.c_ubyte),
        ("time", _Time),
        ("source", _Addr),
        ("dest", _Addr),
        ("data", _Data),
    ]


_lib.snd_seq_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p,
                              ctypes.c_int, ctypes.c_int]
_lib.snd_seq_set_client_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
_lib.snd_seq_create_simple_port.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                            ctypes.c_uint, ctypes.c_uint]
_lib.snd_seq_client_id.argtypes = [ctypes.c_void_p]
_lib.snd_seq_event_output_direct.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(SndSeqEvent)]
_lib.snd_seq_event_input.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.POINTER(SndSeqEvent))]
_lib.snd_seq_close.argtypes = [ctypes.c_void_p]
_lib.snd_midi_event_new.argtypes = [ctypes.c_size_t,
                                    ctypes.POINTER(ctypes.c_void_p)]
_lib.snd_midi_event_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_long, ctypes.POINTER(SndSeqEvent)]
_lib.snd_midi_event_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_long, ctypes.POINTER(SndSeqEvent)]
_lib.snd_midi_event_decode.restype = ctypes.c_long
_lib.snd_midi_event_encode.restype = ctypes.c_long
_lib.snd_midi_event_no_status.argtypes = [ctypes.c_void_p, ctypes.c_int]
_lib.snd_midi_event_reset_encode.argtypes = [ctypes.c_void_p]
_lib.snd_midi_event_reset_decode.argtypes = [ctypes.c_void_p]
_lib.snd_seq_connect_to.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int]
_lib.snd_seq_connect_from.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int]
_lib.snd_strerror.argtypes = [ctypes.c_int]
_lib.snd_strerror.restype = ctypes.c_char_p


def _check(ret, what):
    if ret < 0:
        raise OSError(f"{what}: {_lib.snd_strerror(ret).decode()}")
    return ret


class SeqPort:
    """One ALSA sequencer client exposing a single duplex MIDI port."""

    def __init__(self, client_name="SMC-Mixer", port_name="SMC-Mixer MIDI"):
        self._seq = ctypes.c_void_p()
        _check(_lib.snd_seq_open(ctypes.byref(self._seq), b"default",
                                 SND_SEQ_OPEN_DUPLEX, 0), "snd_seq_open")
        _check(_lib.snd_seq_set_client_name(self._seq, client_name.encode()),
               "set_client_name")
        caps = (SND_SEQ_PORT_CAP_READ | SND_SEQ_PORT_CAP_WRITE
                | SND_SEQ_PORT_CAP_SUBS_READ | SND_SEQ_PORT_CAP_SUBS_WRITE)
        self.port = _check(
            _lib.snd_seq_create_simple_port(
                self._seq, port_name.encode(), caps,
                SND_SEQ_PORT_TYPE_MIDI_GENERIC | SND_SEQ_PORT_TYPE_APPLICATION),
            "create_simple_port")
        self.client = _check(_lib.snd_seq_client_id(self._seq), "client_id")

        self._enc = ctypes.c_void_p()
        _check(_lib.snd_midi_event_new(1024, ctypes.byref(self._enc)),
               "midi_event_new(enc)")
        self._dec = ctypes.c_void_p()
        _check(_lib.snd_midi_event_new(1024, ctypes.byref(self._dec)),
               "midi_event_new(dec)")
        # Suppress running status on the way out of the decoder, so every
        # recv() returns a self-contained message. Otherwise a continuation
        # arrives with no status byte and the caller has to carry parser state
        # across reads — which it cannot do reliably once reads are batched.
        _lib.snd_midi_event_no_status(self._dec, 1)
        _lib.snd_midi_event_no_status(self._enc, 1)
        self._wlock = threading.Lock()
        self._closed = False

    def send(self, midi_bytes):
        """Emit raw MIDI bytes to whoever is subscribed to our port."""
        buf = bytes(midi_bytes)
        ev = SndSeqEvent()
        with self._wlock:
            # We always feed one complete message, so never carry parser state
            # from the previous call into this one.
            _lib.snd_midi_event_reset_encode(self._enc)
            n = _lib.snd_midi_event_encode(self._enc, buf, len(buf),
                                           ctypes.byref(ev))
            if n <= 0 or ev.type == 0:
                return False
            ev.queue = SND_SEQ_QUEUE_DIRECT
            ev.source.port = self.port
            ev.dest.client = SND_SEQ_ADDRESS_SUBSCRIBERS
            ev.dest.port = SND_SEQ_ADDRESS_UNKNOWN
            _lib.snd_seq_event_output_direct(self._seq, ctypes.byref(ev))
        return True

    def recv(self):
        """Block until a MIDI message arrives; return raw bytes (or None).

        ctypes drops the GIL across the call, so this is safe to park a
        thread on.
        """
        evp = ctypes.POINTER(SndSeqEvent)()
        ret = _lib.snd_seq_event_input(self._seq, ctypes.byref(evp))
        if ret < 0 or self._closed:
            return None
        buf = ctypes.create_string_buffer(256)
        n = _lib.snd_midi_event_decode(self._dec, buf, 256, evp)
        return buf.raw[:n] if n > 0 else b""

    def connect_to(self, client, port):
        """Subscribe a destination port to our output."""
        return _lib.snd_seq_connect_to(self._seq, self.port, client, port) >= 0

    def connect_from(self, client, port):
        """Subscribe our input to a source port."""
        return _lib.snd_seq_connect_from(self._seq, self.port, client, port) >= 0

    def close(self):
        self._closed = True
        if self._seq:
            _lib.snd_seq_close(self._seq)
            self._seq = ctypes.c_void_p()


def fd_watch(fd, condition, callback, data=None):
    """GLib fd watch that prefers the non-deprecated GLibUnix entry point."""
    from gi.repository import GLib
    try:
        import gi
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        return GLibUnix.fd_add_full(GLib.PRIORITY_DEFAULT, fd, condition,
                                    callback, data)
    except (ImportError, ValueError):
        return GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, fd, condition,
                                     callback, data)


if __name__ == "__main__":
    import time
    p = SeqPort("SMC-Mixer-test", "test port")
    print(f"created ALSA client {p.client}:{p.port} — check `aconnect -l`")
    time.sleep(10)
    p.close()
