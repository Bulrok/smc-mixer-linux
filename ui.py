#!/usr/bin/env python3
"""SMC-Mixer control panel — live preview, manual testing and configuration.

This is a detachable client of smcmixerd. Closing the window leaves the bridge
and its ALSA port running; the daemon is what the DAW is talking to.

Preview interactions
  left-click a button   toggle that LED on the hardware
  right-click a button  simulate a press (sends MIDI to the host, not the LED)
  drag a fader          simulate fader movement to the host
  scroll an encoder     simulate encoder ticks to the host
"""
import json
import math
import os
import socket
import subprocess
import sys

import cairo
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk  # noqa: E402

import alsaseq  # noqa: E402
import layout  # noqa: E402
from smcmixerd import SOCKET_PATH, HERE  # noqa: E402

DEBUG = bool(os.environ.get("SMC_DEBUG"))


def dbg(*a):
    if DEBUG:
        print("[dbg]", *a, file=sys.stderr, flush=True)

BG = (0.075, 0.082, 0.098)
PANEL = (0.145, 0.157, 0.180)
STROKE = (0.20, 0.223, 0.266)
TEXT = (0.765, 0.792, 0.839)
DIM = (0.475, 0.506, 0.561)
ACCENT = (0.36, 0.62, 0.95)

ROW_COLOURS = {
    "REC": (1.00, 0.30, 0.30),
    "SOLO": (1.00, 0.76, 0.30),
    "MUTE": (0.30, 0.82, 1.00),
    "SELECT": (0.43, 0.91, 0.48),
}
EXTRA_COLOUR = (0.55, 0.78, 1.00)


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def label(cr, text, cx, cy, size=10, colour=TEXT, bold=False):
    cr.select_font_face("Cantarell",
                        0, 1 if bold else 0)
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    cr.set_source_rgb(*colour)
    cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + ext.height / 2)
    cr.show_text(text)


# ------------------------------------------------------------------- client

class Client:
    """Line-delimited JSON link to smcmixerd, with auto-reconnect."""

    def __init__(self, on_msg, on_conn):
        self.on_msg = on_msg
        self.on_conn = on_conn
        self.sock = None
        self.buf = b""
        self.connect()
        GLib.timeout_add_seconds(2, self._keepalive)

    def connect(self):
        if self.sock:
            return True
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(SOCKET_PATH)
        except OSError:
            return False
        s.setblocking(False)
        self.sock = s
        self.buf = b""
        alsaseq.fd_watch(s.fileno(),
                         GLib.IOCondition.IN | GLib.IOCondition.HUP,
                         self._ready, None)
        self.on_conn(True)
        return True

    def _keepalive(self):
        if self.sock is None:
            ensure_daemon()
            self.connect()
        else:
            # A socket whose peer died can sit here looking healthy while no
            # data ever arrives again. Poke it so a broken pipe surfaces as a
            # reconnect instead of a window that has quietly gone deaf.
            self.send({"c": "ping"})
        return True

    def _ready(self, fd, cond, _d):
        if cond & GLib.IOCondition.HUP:
            self._lost()
            return False
        try:
            chunk = self.sock.recv(65536)
        except OSError:
            self._lost()
            return False
        if not chunk:
            self._lost()
            return False
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if line.strip():
                try:
                    self.on_msg(json.loads(line))
                except ValueError:
                    pass
        return True

    def _lost(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.on_conn(False)

    def send(self, msg):
        if not self.sock:
            return
        try:
            self.sock.sendall((json.dumps(msg) + "\n").encode())
        except OSError:
            self._lost()


def ensure_daemon():
    """Start the bridge if it is not already up."""
    if os.path.exists(SOCKET_PATH):
        return
    if subprocess.call(["systemctl", "--user", "start", "smc-mixer.service"],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL) == 0:
        return
    subprocess.Popen([sys.executable, os.path.join(HERE, "smcmixerd.py")],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# -------------------------------------------------------------- device view

class MixerPanel(Gtk.DrawingArea):
    """Scale drawing of the mixer that mirrors — and drives — the hardware."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.set_content_width(940)
        self.set_content_height(520)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)
        self.hits = []
        self._drag_fader = None

        click = Gtk.GestureClick.new()
        click.set_button(0)
        click.connect("pressed", self._pressed)
        self.add_controller(click)

        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)

        motion = Gtk.EventControllerMotion.new()
        motion.connect("motion", self._motion)
        self.add_controller(motion)
        self.hover = None
        self.hover_pos = None

    # -- geometry ---------------------------------------------------------

    def _geometry(self, w, h):
        pad = 16
        n = layout.N_CHANNELS
        inner_w = w - 2 * pad
        strip_w = inner_w / n

        y = pad + 14
        enc_h = 58
        btn_h = 24
        btn_gap = 5
        rows_h = len(layout.ROWS) * (btn_h + btn_gap)
        bottom_h = 62
        fader_top = y + enc_h + rows_h + 10
        fader_h = max(120, h - fader_top - bottom_h - pad - 36)

        g = {"strips": [], "extras": {}, "pad": pad,
             "fader_top": fader_top, "fader_h": fader_h}

        for c in range(n):
            sx = pad + c * strip_w
            cx = sx + strip_w / 2
            strip = {
                "index": c,
                "x": sx, "w": strip_w, "cx": cx,
                "encoder": (cx, y + enc_h / 2 - 4, min(19, strip_w / 3.2)),
                "buttons": {},
                "fader": (cx - 13, fader_top, 26, fader_h),
            }
            by = y + enc_h
            for name, base in layout.ROWS:
                strip["buttons"][base + c] = (
                    sx + 6, by, strip_w - 12, btn_h, name)
                by += btn_h + btn_gap
            g["strips"].append(strip)

        # Bottom bar: bank, transport, nav.
        by = h - bottom_h - pad + 8
        bh = 30
        x = pad
        for name, note in layout.BANK:
            g["extras"][note] = (x, by, 62, bh, name)
            x += 68
        x += 24
        for name, note in layout.TRANSPORT:
            g["extras"][note] = (x, by, 62, bh, name)
            x += 68
        x += 24
        for name, note in layout.NAV:
            g["extras"][note] = (x, by, 52, bh, name)
            x += 58
        return g

    # -- drawing ----------------------------------------------------------

    def _draw(self, area, cr, w, h, *_):
        dbg(f"_draw {w}x{h} connected={self.app.state.get('connected')} "
            f"faders={self.app.state.get('faders')}")
        st = self.app.state
        leds = st.get("leds", {})
        buttons = self.app.pressed
        g = self._geometry(w, h)
        self.hits = []

        cr.set_source_rgb(*BG)
        cr.paint()
        rounded(cr, 6, 6, w - 12, h - 12, 14)
        grad = cairo.LinearGradient(0, 6, 0, h - 6)
        grad.add_color_stop_rgb(0, 0.168, 0.180, 0.204)
        grad.add_color_stop_rgb(1, 0.125, 0.135, 0.157)
        cr.set_source(grad)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.05)
        cr.set_line_width(1)
        cr.stroke()

        for strip in g["strips"]:
            self._draw_encoder(cr, strip, st)
            for note, (x, y, bw, bh, name) in strip["buttons"].items():
                self._draw_button(cr, x, y, bw, bh, name,
                                  leds.get(str(note), False),
                                  buttons.get(note, False),
                                  ROW_COLOURS[name], note)
            self._draw_fader(cr, strip, st)

        for note, (x, y, bw, bh, name) in g["extras"].items():
            self._draw_button(cr, x, y, bw, bh, name,
                              leds.get(str(note), False),
                              buttons.get(note, False), EXTRA_COLOUR, note)

        if not st.get("connected"):
            cr.set_source_rgba(0.05, 0.05, 0.07, 0.72)
            cr.paint()
            label(cr, "mixer not connected", w / 2, h / 2 - 8, 18,
                  (0.85, 0.87, 0.9), bold=True)
            label(cr, "the ALSA port stays open — turn the mixer on to reattach",
                  w / 2, h / 2 + 16, 11, DIM)
        elif st.get("mode_warning"):
            # Unrecognised MIDI: almost always the mixer's CC-only mapping.
            # Veil the panel — whatever it is showing is stale by definition.
            cr.set_source_rgba(0.05, 0.055, 0.07, 0.66)
            cr.paint()
            bh = 52
            by = h / 2 - bh / 2
            rounded(cr, 40, by, w - 80, bh, 10)
            cr.set_source_rgba(1.0, 0.76, 0.30, 0.96)
            cr.fill()
            label(cr, "mixer is sending MIDI this layout does not recognise",
                  w / 2, by + 18, 12.5, (0.10, 0.09, 0.05), bold=True)
            label(cr, "it is probably in its CC-only mode — use the button "
                      "combo on the unit to switch back to Mackie/MCU",
                  w / 2, by + 36, 10.5, (0.22, 0.17, 0.06))

    def _draw_encoder(self, cr, strip, st):
        cx, cy, r = strip["encoder"]
        i = strip["index"]
        val = (st.get("encoders") or [0] * 8)[i]

        # Travel arc behind the dial, so position reads at a glance.
        start, sweep = -math.pi * 1.25, math.pi * 1.5
        cr.set_source_rgb(0.13, 0.145, 0.17)
        cr.set_line_width(3)
        cr.new_path()
        cr.arc(cx, cy, r + 4, start, start + sweep)
        cr.stroke()
        if val > 0:
            cr.set_source_rgb(*ACCENT)
            cr.new_path()
            cr.arc(cx, cy, r + 4, start, start + sweep * (val / 127))
            cr.stroke()

        g = cairo.LinearGradient(cx, cy - r, cx, cy + r)
        g.add_color_stop_rgb(0, 0.20, 0.22, 0.26)
        g.add_color_stop_rgb(1, 0.11, 0.12, 0.145)
        cr.set_source(g)
        cr.new_path()
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(*STROKE)
        cr.set_line_width(1)
        cr.new_path()
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()

        ang = start + sweep * (val / 127)
        cr.set_source_rgb(*[min(1.0, c * 1.15) for c in ACCENT])
        cr.set_line_width(2.4)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(cx + math.cos(ang) * r * 0.32, cy + math.sin(ang) * r * 0.32)
        cr.line_to(cx + math.cos(ang) * r * 0.82, cy + math.sin(ang) * r * 0.82)
        cr.stroke()
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        self.hits.append(((cx - r, cy - r, 2 * r, 2 * r), "encoder", i))

    def _draw_button(self, cr, x, y, w, h, name, lit, pressed, colour, note):
        if lit:
            # Halo first, so the LED reads as emitting rather than painted.
            for spread, alpha in ((6, 0.10), (3, 0.16)):
                rounded(cr, x - spread, y - spread,
                        w + 2 * spread, h + 2 * spread, 5 + spread)
                cr.set_source_rgba(*colour, alpha)
                cr.fill()
        rounded(cr, x, y, w, h, 5)
        if lit:
            g = cairo.LinearGradient(0, y, 0, y + h)
            g.add_color_stop_rgb(0, *[min(1.0, c * 1.0) for c in colour])
            g.add_color_stop_rgb(1, *[c * 0.72 for c in colour])
            cr.set_source(g)
            cr.fill_preserve()
            cr.set_source_rgb(*[min(1.0, c * 1.25) for c in colour])
            cr.set_line_width(1.2)
            cr.stroke()
        else:
            g = cairo.LinearGradient(0, y, 0, y + h)
            g.add_color_stop_rgb(0, 0.157, 0.170, 0.196)
            g.add_color_stop_rgb(1, 0.113, 0.124, 0.145)
            cr.set_source(g)
            cr.fill_preserve()
            cr.set_source_rgb(*STROKE)
            cr.set_line_width(1.0)
            cr.stroke()
        if pressed:
            rounded(cr, x - 2.5, y - 2.5, w + 5, h + 5, 7)
            cr.set_source_rgba(0.92, 0.95, 1.0, 0.9)
            cr.set_line_width(1.6)
            cr.stroke()
        if self.hover == ("button", note) and not lit:
            rounded(cr, x, y, w, h, 5)
            cr.set_source_rgba(*colour, 0.22)
            cr.fill()
        txt = (0.08, 0.09, 0.1) if lit else DIM
        label(cr, name, x + w / 2, y + h / 2, 9.5, txt, bold=lit)
        self.hits.append(((x, y, w, h), "button", note))

    def _draw_fader(self, cr, strip, st):
        x, y, w, h = strip["fader"]
        i = strip["index"]
        val = (st.get("faders") or [0] * 8)[i]
        frac = val / layout.FADER_MAX if layout.FADER_MAX else 0

        # Recessed slot.
        cr.set_source_rgb(0.075, 0.083, 0.098)
        rounded(cr, x + w / 2 - 3.5, y, 7, h, 3.5)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.05)
        cr.set_line_width(1)
        rounded(cr, x + w / 2 - 3.5, y, 7, h, 3.5)
        cr.stroke()

        cap_h = 24
        cy = y + (1 - frac) * (h - cap_h)

        # Lit portion below the cap, with a soft glow.
        if frac > 0.001:
            top = cy + cap_h / 2
            cr.set_source_rgba(*ACCENT, 0.22)
            rounded(cr, x + w / 2 - 6, top, 12, y + h - top, 6)
            cr.fill()
            cr.set_source_rgb(*ACCENT)
            rounded(cr, x + w / 2 - 3.5, top, 7, y + h - top, 3.5)
            cr.fill()

        g = cairo.LinearGradient(0, cy, 0, cy + cap_h)
        g.add_color_stop_rgb(0.0, 0.44, 0.47, 0.53)
        g.add_color_stop_rgb(0.48, 0.30, 0.33, 0.38)
        g.add_color_stop_rgb(0.52, 0.22, 0.24, 0.28)
        g.add_color_stop_rgb(1.0, 0.34, 0.37, 0.42)
        rounded(cr, x, cy, w, cap_h, 4)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_source_rgb(0.10, 0.11, 0.13)
        cr.set_line_width(1)
        cr.stroke()
        # Indicator line across the middle of the cap.
        cr.set_source_rgba(0.85, 0.89, 0.96, 0.85)
        cr.set_line_width(1.5)
        cr.move_to(x + 4, cy + cap_h / 2)
        cr.line_to(x + w - 4, cy + cap_h / 2)
        cr.stroke()

        label(cr, str(i + 1), strip["cx"], y + h + 12, 11, TEXT, bold=True)
        label(cr, f"{round(frac * 100)}%", strip["cx"], y + h + 26, 9, DIM)
        self.hits.append(((x - 6, y, w + 12, h), "fader", i))

    # -- interaction ------------------------------------------------------

    def _hit(self, px, py, kinds=None):
        for (x, y, w, h), kind, payload in reversed(self.hits):
            if kinds and kind not in kinds:
                continue
            if x <= px <= x + w and y <= py <= y + h:
                return kind, payload, (x, y, w, h)
        return None, None, None

    def _motion(self, _c, x, y):
        self.hover_pos = (x, y)
        kind, payload, _ = self._hit(x, y, ("button",))
        hover = (kind, payload) if kind else None
        if hover != self.hover:
            self.hover = hover
            self.queue_draw()

    def _pressed(self, gesture, n_press, x, y):
        button = gesture.get_current_button()
        dbg(f"_pressed button={button} at {x:.0f},{y:.0f} hits={len(self.hits)} "
            f"-> {self._hit(x, y, ('button',))[:2]}")
        kind, payload, _ = self._hit(x, y, ("button",))
        if kind != "button":
            return
        if button == 3:                      # right-click: simulate a press
            self.app.simulate_press(payload)
        else:                                # left-click: toggle the real LED
            lit = self.app.state.get("leds", {}).get(str(payload), False)
            self.app.client.send({"c": "led", "note": payload, "on": not lit})

    def _drag_begin(self, gesture, x, y):
        kind, payload, rect = self._hit(x, y, ("fader",))
        if kind == "fader":
            self._drag_fader = (payload, rect)
            self._set_fader_from_y(payload, rect, y)

    def _drag_update(self, gesture, dx, dy):
        if not self._drag_fader:
            return
        ok, sx, sy = gesture.get_start_point()
        if not ok:
            return
        i, rect = self._drag_fader
        self._set_fader_from_y(i, rect, sy + dy)

    def _drag_end(self, *_a):
        self._drag_fader = None

    def _set_fader_from_y(self, i, rect, y):
        _x, ry, _w, rh = rect
        frac = 1 - max(0.0, min(1.0, (y - ry) / rh))
        steps = round(frac * (layout.FADER_MAX / layout.FADER_STEP))
        value = int(steps * layout.FADER_STEP)
        self.app.client.send({"c": "emit",
                              "b": [0xE0 | i, value & 0x7F, (value >> 7) & 0x7F]})

    def _scroll(self, _c, _dx, dy):
        """Scrolling over an encoder emits relative ticks, as the knob would."""
        if self.hover_pos is None:
            return False
        kind, payload, _ = self._hit(*self.hover_pos, ("encoder",))
        if kind != "encoder":
            return False
        tick = 0x01 if dy < 0 else 0x41       # 0x01 = CW, 0x41 = CCW
        self.app.client.send({"c": "emit",
                              "b": [0xB0, layout.CC_ENCODER_BASE + payload, tick]})
        return True


# ------------------------------------------------------------------ window

# Same palette the panel is painted with, so the chrome and the device art read
# as one surface rather than a dark drawing pasted into a light app.
CSS = """
@define-color bg      #131519;
@define-color panel   #1b1e23;
@define-color raised  #23272e;
@define-color hover   #2c313a;
@define-color stroke  #333944;
@define-color text    #c3cad6;
@define-color dim     #79818f;
@define-color accent  #5c9ef2;

window, .background { background-color: @bg; color: @text; }

headerbar {
  background: linear-gradient(to bottom, #21252b, #191c21);
  border-bottom: 1px solid @stroke;
  box-shadow: none;
  min-height: 44px;
  padding: 0 8px;
  color: @text;
}
headerbar label { color: @text; }

button {
  background-image: none;
  background-color: @raised;
  color: @text;
  border: 1px solid @stroke;
  border-radius: 7px;
  padding: 5px 13px;
  box-shadow: none;
  text-shadow: none;
  transition: background-color 120ms ease, border-color 120ms ease;
}
button:hover  { background-color: @hover; border-color: #414855; }
button:active,
button:checked {
  background-color: @accent;
  border-color: @accent;
  color: #0e1116;
  font-weight: bold;
}
button:disabled { background-color: @panel; color: @dim; }

stackswitcher { padding: 2px; }
stackswitcher button {
  border: 1px solid transparent;
  background-color: transparent;
  border-radius: 7px;
  padding: 4px 14px;
  color: @dim;
  font-weight: normal;
}
stackswitcher button:hover   { background-color: @raised; color: @text; }
stackswitcher button:checked {
  background-color: @raised;
  border-color: @stroke;
  color: @text;
  font-weight: bold;
}

scrolledwindow, textview, textview text {
  background-color: @panel;
  color: @text;
  border-radius: 8px;
}
textview { padding: 8px; }
textview text selection { background-color: @accent; color: #0e1116; }

switch {
  background-color: @raised;
  border: 1px solid @stroke;
  border-radius: 999px;
  min-width: 44px;
  min-height: 24px;
}
switch:checked { background-color: @accent; border-color: @accent; }
switch > slider {
  background-color: #d3d9e3;
  border: none;
  border-radius: 999px;
  min-width: 18px;
  min-height: 18px;
  margin: 2px;
}

checkbutton { color: @text; }
check {
  background-color: @raised;
  border: 1px solid @stroke;
  border-radius: 5px;
}
check:checked { background-color: @accent; border-color: @accent; }

separator { background-color: @stroke; min-width: 1px; min-height: 1px; }
tooltip {
  background-color: #0f1216;
  color: @text;
  border: 1px solid @stroke;
  border-radius: 7px;
}
scrollbar { background-color: transparent; border: none; }
scrollbar slider {
  background-color: #39404b;
  border-radius: 999px;
  min-width: 8px;
  min-height: 8px;
}
scrollbar slider:hover { background-color: #4a5260; }

.mono      { font-family: monospace; font-size: 11px; color: @dim; }
.statusbad { color: #ff8080; font-weight: bold; }
.statusok  { color: #6ee87a; font-weight: bold; }
.hint      { font-size: 11px; color: @dim; }
.section   { font-weight: bold; color: @text; }
.battery   { color: @dim; font-size: 11px; }
"""


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="SMC-Mixer")
        self.app = app
        self.set_default_size(980, 760)

        header = Gtk.HeaderBar()
        self.status = Gtk.Label(label="connecting…")
        header.pack_start(self.status)
        self.battery = Gtk.Label(label="")
        header.pack_end(self.battery)
        self.set_titlebar(header)

        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=stack)
        header.set_title_widget(switcher)

        stack.add_titled(self._device_page(), "device", "Device")
        stack.add_titled(self._monitor_page(), "monitor", "MIDI Monitor")
        stack.add_titled(self._settings_page(), "settings", "Settings")
        self.set_child(stack)

    # -- pages ------------------------------------------------------------

    def _device_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=8, margin_bottom=10,
                      margin_start=10, margin_end=10)
        self.panel = MixerPanel(self.app)
        box.append(self.panel)

        hint = Gtk.Label(xalign=0, label=(
            "left-click a button to toggle its LED   ·   "
            "right-click to simulate a press   ·   "
            "drag a fader or scroll an encoder to send MIDI to the host"))
        hint.add_css_class("hint")
        box.append(hint)

        tests = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for text, fn in (
            ("All LEDs on", lambda: self.app.leds_all(True)),
            ("All off", lambda: self.app.leds_all(False)),
            ("Chase", lambda: self.app.client.send({"c": "chase"})),
            ("Fader sweep", self.app.fader_sweep),
        ):
            b = Gtk.Button(label=text)
            b.connect("clicked", lambda _b, f=fn: f())
            tests.append(b)

        tests.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        for name, base in layout.ROWS:
            b = Gtk.Button(label=name)
            b.set_tooltip_text(f"Light the whole {name} row")
            b.connect("clicked",
                      lambda _b, bs=base: self.app.light_row(bs))
            tests.append(b)
        box.append(tests)
        return box

    def _monitor_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=10, margin_bottom=10,
                      margin_start=10, margin_end=10)
        self.mon_view = Gtk.TextView(editable=False, monospace=True,
                                     cursor_visible=False)
        self.mon_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.mon_view.set_left_margin(10)
        self.mon_view.set_top_margin(8)
        self.mon_buf = self.mon_view.get_buffer()
        # Direction is far easier to read as colour than as an arrow glyph.
        self.tag_in = self.mon_buf.create_tag("in", foreground="#5c9ef2")
        self.tag_out = self.mon_buf.create_tag("out", foreground="#ffc24d")
        self.tag_raw = self.mon_buf.create_tag("raw", foreground="#79818f")
        self.tag_txt = self.mon_buf.create_tag("txt", foreground="#c3cad6")
        self.tag_name = self.mon_buf.create_tag("name", foreground="#6ee87a")
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_child(self.mon_view)
        box.append(sw)

        row = Gtk.Box(spacing=8)
        self.mon_pause = Gtk.CheckButton(label="Pause")
        row.append(self.mon_pause)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda _b: self.mon_buf.set_text(""))
        row.append(clear)
        legend = Gtk.Label(xalign=0, label="   in = from mixer   ·   "
                                          "out = from host")
        legend.add_css_class("hint")
        row.append(legend)
        box.append(row)
        return box

    def _settings_page(self):
        grid = Gtk.Grid(row_spacing=12, column_spacing=16,
                        margin_top=18, margin_bottom=18,
                        margin_start=18, margin_end=18)
        r = 0

        def add_switch(text, key, subtitle):
            nonlocal r
            lbl = Gtk.Label(label=text, xalign=0)
            sub = Gtk.Label(label=subtitle, xalign=0)
            sub.add_css_class("hint")
            wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            wrap.append(lbl)
            wrap.append(sub)
            sw = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
            sw.connect("state-set", lambda _s, val, k=key:
                       self.app.set_config(k, val))
            grid.attach(wrap, 0, r, 1, 1)
            grid.attach(sw, 1, r, 1, 1)
            r += 1
            return sw

        self.sw_launch = add_switch(
            "Open this window when the mixer connects", "auto_launch_ui",
            "The bridge keeps running when you close the window either way.")
        self.sw_connect = add_switch(
            "Reconnect the mixer automatically", "auto_connect",
            "Asks BlueZ to re-establish the link when the mixer wakes up.")
        self.sw_echo = add_switch(
            "Light LEDs locally on button press", "local_led_echo",
            "Lets buttons toggle their own lights with no DAW attached.")

        info = Gtk.Label(xalign=0, selectable=True)
        info.add_css_class("mono")
        self.info_label = info
        grid.attach(info, 0, r, 2, 1)
        r += 1

        restart = Gtk.Button(label="Restart bridge")
        restart.set_halign(Gtk.Align.START)
        restart.connect("clicked", lambda _b: subprocess.Popen(
            ["systemctl", "--user", "restart", "smc-mixer.service"]))
        grid.attach(restart, 0, r, 1, 1)
        return grid

    # -- updates ----------------------------------------------------------

    def set_bridge(self, up):
        """Show whether we are talking to the daemon at all."""
        self.bridge_up = up
        if not up:
            self.status.set_text("bridge offline — reconnecting…")
            self.status.remove_css_class("statusok")
            self.status.add_css_class("statusbad")

    def apply_state(self, st):
        if not getattr(self, "bridge_up", True):
            return
        ok = st.get("connected")
        self.status.set_text(
            f"{st.get('device', '?')} — {'connected' if ok else 'disconnected'}"
            f"   ·   ALSA {st.get('alsa', '?')}")
        self.status.remove_css_class("statusok")
        self.status.remove_css_class("statusbad")
        self.status.add_css_class("statusok" if ok else "statusbad")
        bat = st.get("battery")
        self.battery.set_text(f"battery {bat}%" if bat is not None else "")

        cfg = st.get("config", {})
        for sw, key in ((self.sw_launch, "auto_launch_ui"),
                        (self.sw_connect, "auto_connect"),
                        (self.sw_echo, "local_led_echo")):
            want = bool(cfg.get(key))
            if sw.get_active() != want:
                sw.set_active(want)
        self.info_label.set_text(
            f"ALSA port    {st.get('alsa_name', '')}  ({st.get('alsa', '')})\n"
            f"Bluetooth    {st.get('address', '')}\n"
            f"Config file  {CONFIG_HINT}")
        self.panel.queue_draw()

    def log_midi(self, entry):
        if self.mon_pause.get_active():
            return
        inbound = entry["dir"] == "in"
        raw = " ".join(f"{b:02X}" for b in entry["b"])
        buf = self.mon_buf

        def put(text, tag):
            buf.insert_with_tags(buf.get_end_iter(), text, tag)

        put(f"{'IN ' if inbound else 'OUT'} ", self.tag_in if inbound
            else self.tag_out)
        put(f"{raw:<11} ", self.tag_raw)
        put(f"{entry['desc']:<34}", self.tag_txt)
        if (entry["b"][0] & 0xF0) in (0x90, 0x80):
            put(layout.note_name(entry["b"][1]), self.tag_name)
        put("\n", self.tag_txt)
        if self.mon_buf.get_line_count() > 500:
            start = self.mon_buf.get_start_iter()
            cut = self.mon_buf.get_iter_at_line(200)[1]
            self.mon_buf.delete(start, cut)
        self.mon_view.scroll_to_iter(self.mon_buf.get_end_iter(), 0, False, 0, 0)


CONFIG_HINT = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "smc-mixer", "config.json")


# --------------------------------------------------------------------- app

class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.local.SmcMixer")
        self.state = {"leds": {}, "faders": [0] * 8, "encoders": [0] * 8}
        self.pressed = {}
        self.win = None

    def do_activate(self):
        if self.win:
            self.win.present()
            return
        # Dark regardless of the desktop's preference — the device art is
        # painted dark, and a light theme around it looks like a bug.
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-application-prefer-dark-theme", True)

        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(CSS)          # GTK 4.12+
        except AttributeError:
            provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        ensure_daemon()
        self.win = Window(self)
        self.client = Client(self._on_msg, self._on_conn)
        self.win.present()

    # -- daemon messages --------------------------------------------------

    def _on_conn(self, up):
        if not self.win:
            return
        self.win.set_bridge(up)
        if not up:
            self.state["connected"] = False
            self.win.panel.queue_draw()

    def _on_msg(self, msg):
        t = msg.get("t")
        if t == "state":
            self.state = msg
            self.win.apply_state(msg)
        elif t == "midi":
            self.win.log_midi(msg)
        elif t == "ev":
            self._event(msg)

    def _event(self, msg):
        dbg("_event", msg)
        kind = msg.get("kind")
        if kind == "fader":
            self.state.setdefault("faders", [0] * 8)[msg["i"]] = msg["v"]
        elif kind == "encoder":
            self.state.setdefault("encoders", [0] * 8)[msg["i"]] = msg["v"]
        elif kind == "button":
            self.pressed[msg["note"]] = msg["down"]
        elif kind == "led":
            self.state.setdefault("leds", {})[str(msg["note"])] = msg["on"]
        elif kind == "leds":
            for n in msg["notes"]:
                self.state.setdefault("leds", {})[str(n)] = msg["on"]
        self.win.panel.queue_draw()

    # -- test helpers -----------------------------------------------------

    def leds_all(self, on):
        self.client.send({"c": "leds", "notes": layout.all_led_notes(),
                          "on": on})

    def light_row(self, base):
        notes = [base + c for c in range(layout.N_CHANNELS)]
        lit = all(self.state.get("leds", {}).get(str(n)) for n in notes)
        self.client.send({"c": "leds", "notes": notes, "on": not lit})

    def simulate_press(self, note):
        """Send a press/release to the host without touching the LED."""
        self.client.send({"c": "emit", "b": [0x90, note, 0x7F]})
        GLib.timeout_add(90, lambda: (
            self.client.send({"c": "emit", "b": [0x90, note, 0x00]}), False)[1])

    def fader_sweep(self):
        """Run every fader up and back down, as MIDI to the host."""
        state = {"step": 0}
        steps = 48

        def tick():
            p = state["step"] / steps
            frac = p * 2 if p < 0.5 else (1 - p) * 2
            value = int(round(frac * 127)) << 7
            for i in range(layout.N_CHANNELS):
                self.client.send({"c": "emit",
                                  "b": [0xE0 | i, value & 0x7F,
                                        (value >> 7) & 0x7F]})
            state["step"] += 1
            return state["step"] <= steps

        GLib.timeout_add(40, tick)

    def set_config(self, key, value):
        self.client.send({"c": "config", "k": key, "v": bool(value)})
        return False


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
