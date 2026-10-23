"""GTK 4 widget that shows a VM's display and forwards input to it."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

from rehome.ui.console.dbus_display import DisplayClient  # noqa: E402
from rehome.ui.console.keymap import QNUM, gdk_keycode_to_qnum  # noqa: E402
from rehome.ui.console import relmouse  # noqa: E402
from rehome.ui.console.relmouse import RelMouse  # noqa: E402

log = logging.getLogger(__name__)

HAS_X_FORMAT = hasattr(Gdk.MemoryFormat, "B8G8R8X8")
# GDK button -> QEMU D-Bus button value
BUTTONS = {1: 0, 2: 1, 3: 2, 8: 5, 9: 6}
BLACK = Gdk.RGBA()
BLACK.parse("black")


class DisplayView(Gtk.Widget):
    __gtype_name__ = "RehomeDisplayView"
    __gsignals__ = {
        "grab-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "release-combo": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, client: DisplayClient):
        super().__init__(focusable=True, can_focus=True, hexpand=True, vexpand=True)
        self.client = client
        self.texture: Gdk.Texture | None = None
        self.dmabuf: dict | None = None
        self._dirty = False
        self._scheduled = False
        self.scale_to_fit = True
        self.resize_guest = True
        self.grabbed = False
        self._pressed: set[int] = set()
        self._last_motion: tuple[float, float] | None = None
        self._rel = RelMouse()
        self._rel_timer = 0
        self._resize_source = 0
        self._last_ui_info = (0, 0)
        self.set_cursor(Gdk.Cursor.new_from_name("default", None))
        client.connect("scanout", self._on_frame)
        client.connect("update", self._on_frame)
        client.connect("dmabuf", self._on_dmabuf)
        client.connect("dmabuf-update", self._on_dmabuf_update)
        client.connect("cursor-define", self._on_cursor)
        client.connect("mouse-set", self._on_mouse_set)
        client.connect("mouse-mode", self._on_mouse_mode)
        client.connect("disabled", lambda *_: self._clear())

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        keys.connect("key-released", self._key_released)
        self.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", self._leave)
        self.add_controller(motion)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._button, True)
        click.connect("released", self._button, False)
        self.add_controller(click)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL |
                                           Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self.release_all_keys())
        self.add_controller(focus)

    # --- frames ------------------------------------------------------------------------

    def _on_frame(self, *_):
        self.dmabuf = None
        self._dirty = True
        if not self._scheduled:
            self._scheduled = True
            GLib.idle_add(self._rebuild, priority=GLib.PRIORITY_HIGH_IDLE + 10)

    def _rebuild(self):
        self._scheduled = False
        if not self._dirty:
            return False
        self._dirty = False
        c = self.client
        if not c.fb or not c.width:
            return False
        w, h = c.width, c.height
        if HAS_X_FORMAT:
            data, fmt = bytes(c.fb), Gdk.MemoryFormat.B8G8R8X8
        else:
            buf = bytearray(c.fb)
            buf[3::4] = b"\xff" * (w * h)
            data, fmt = bytes(buf), Gdk.MemoryFormat.B8G8R8A8
        self.texture = Gdk.MemoryTexture.new(w, h, fmt, GLib.Bytes.new(data), w * 4)
        self.queue_draw()
        return False

    def _on_dmabuf(self, _c, fd, w, h, stride, fourcc, modifier, y0_top):
        if not hasattr(Gdk, "DmabufTextureBuilder"):
            log.warning("3D display needs GTK 4.14 or newer")
            return
        self.dmabuf = {"fd": fd, "w": w, "h": h, "stride": stride, "fourcc": fourcc,
                       "modifier": modifier, "y0_top": y0_top}
        self._build_dmabuf()

    def _build_dmabuf(self):
        d = self.dmabuf
        if not d:
            return
        try:
            b = Gdk.DmabufTextureBuilder()
            b.set_display(self.get_display())
            b.set_width(d["w"])
            b.set_height(d["h"])
            b.set_fourcc(d["fourcc"])
            b.set_modifier(d["modifier"])
            b.set_n_planes(1)
            b.set_fd(0, d["fd"])
            b.set_stride(0, d["stride"])
            b.set_offset(0, 0)
            self.texture = b.build(None, None)
            self._flip = not d["y0_top"]
        except GLib.Error as e:
            log.warning("could not import the guest's 3D frame: %s", e.message)
            return
        self.queue_draw()

    def _on_dmabuf_update(self, *_):
        self._build_dmabuf()

    def _clear(self):
        self.texture = None
        self.queue_draw()

    # --- drawing --------------------------------------------------------------------------

    def _layout(self) -> tuple[float, float, float, float] | None:
        """Rectangle (x, y, w, h) in widget coordinates where the guest image is drawn."""
        t = self.texture
        if t is None:
            return None
        W, H = self.get_width(), self.get_height()
        scale = self.get_scale_factor() or 1
        tw, th = t.get_width() / scale, t.get_height() / scale   # 1:1 in device pixels
        if self.scale_to_fit or tw > W or th > H:
            f = min(W / tw, H / th) if tw and th else 1
            if not self.scale_to_fit:
                f = min(f, 1.0)
            tw, th = tw * f, th * f
        return ((W - tw) / 2, (H - th) / 2, tw, th)

    def do_snapshot(self, snapshot):
        W, H = self.get_width(), self.get_height()
        snapshot.append_color(BLACK, Graphene.Rect().init(0, 0, W, H))
        lay = self._layout()
        if lay is None:
            return
        x, y, w, h = lay
        rect = Graphene.Rect().init(x, y, w, h)
        flip = self.dmabuf is not None and getattr(self, "_flip", False)
        if flip:
            snapshot.save()
            snapshot.translate(Graphene.Point().init(0, y * 2 + h))
            snapshot.scale(1, -1)
        scale = self.get_scale_factor() or 1
        exact = abs(w * scale - self.texture.get_width()) < 0.5
        filt = Gsk.ScalingFilter.NEAREST if exact else Gsk.ScalingFilter.LINEAR
        if hasattr(snapshot, "append_scaled_texture"):
            snapshot.append_scaled_texture(self.texture, filt, rect)
        else:
            snapshot.append_texture(self.texture, rect)
        if flip:
            snapshot.restore()

    def do_measure(self, orientation, for_size):
        t = self.texture
        nat = 0
        if t is not None:
            scale = self.get_scale_factor() or 1
            nat = int((t.get_width() if orientation == Gtk.Orientation.HORIZONTAL
                       else t.get_height()) / scale)
        return (160, max(nat, 160), -1, -1)

    def do_size_allocate(self, width, height, baseline):
        if self.resize_guest:
            if self._resize_source:
                GLib.source_remove(self._resize_source)
            self._resize_source = GLib.timeout_add(400, self._send_ui_info)

    def _send_ui_info(self):
        self._resize_source = 0
        scale = self.get_scale_factor() or 1
        w, h = self.get_width() * scale, self.get_height() * scale
        if w >= 320 and h >= 200 and (w, h) != self._last_ui_info:
            self._last_ui_info = (w, h)
            # Assume ~96 dpi logical pixels for the physical size hint.
            self.client.set_ui_info(w, h, int(self.get_width() * 25.4 / 96),
                                    int(self.get_height() * 25.4 / 96))
        return False

    # --- input ------------------------------------------------------------------------

    def _key_pressed(self, ctl, keyval, keycode, state):
        ctrl_alt = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
        if (state & ctrl_alt) == ctrl_alt and keyval in (Gdk.KEY_Control_L, Gdk.KEY_Alt_L,
                                                        Gdk.KEY_Control_R, Gdk.KEY_Alt_R):
            # Ctrl+Alt releases the pointer/keyboard grab (like other VM consoles).
            self.release_all_keys()
            self.set_grab(False)
            self.emit("release-combo")
            return True
        q = gdk_keycode_to_qnum(keycode)
        if q is None:
            return False
        self._pressed.add(q)
        self.client.key_press(q)
        return True

    def _key_released(self, ctl, keyval, keycode, state):
        q = gdk_keycode_to_qnum(keycode)
        if q is None:
            return
        self._pressed.discard(q)
        self.client.key_release(q)

    def release_all_keys(self) -> None:
        for q in list(self._pressed):
            self.client.key_release(q)
        self._pressed.clear()

    def send_combo(self, names: list[str]) -> None:
        codes = [QNUM[n] for n in names]
        for c in codes:
            self.client.key_press(c)
        for c in reversed(codes):
            self.client.key_release(c)

    def _to_guest(self, x: float, y: float) -> tuple[int, int] | None:
        lay = self._layout()
        if lay is None or not self.client.width:
            return None
        lx, ly, lw, lh = lay
        gx = (x - lx) / lw * self.client.width
        gy = (y - ly) / lh * self.client.height
        return int(gx), int(gy)

    def _motion(self, ctl, x, y):
        if self.client.absolute:
            pos = self._to_guest(x, y)
            if pos:
                self.client.mouse_abs(*pos)
        elif self.grabbed:
            lay = self._layout()
            if self._last_motion is not None and lay and lay[2]:
                f = self.client.width / lay[2]
                self._rel.motion((x - self._last_motion[0]) * f, (y - self._last_motion[1]) * f)
            if lay:
                self._rel.edge(x, y, lay, (self.client.width, self.client.height))
            self._last_motion = (x, y)
            self._send_rel()

    def _leave(self, *_):
        lay = self._layout()
        if self.grabbed and not self.client.absolute and self._last_motion and lay:
            # The hidden host pointer left the view (GTK 4 cannot hold it in): the guest
            # cursor goes to the edge it left by, where the two meet again.
            self._rel.left(*self._last_motion, lay, (self.client.width, self.client.height))
            self._send_rel()
        self._last_motion = None

    def _send_rel(self) -> None:
        """Start sending the queued relative-mouse events: a burst now, then one every
        TICK_MS while any are left (see relmouse.py)."""
        if self._rel_timer or not self._rel.pending():
            return
        self._send_burst()
        self._rel_timer = GLib.timeout_add(relmouse.TICK_MS, self._rel_tick)

    def _rel_tick(self) -> bool:
        if self._rel.pending():
            self._send_burst()
            return True
        # Makes QEMU send whatever did not fit its PS/2 queue yet.
        self.client.mouse_rel(0, 0)
        self._rel_timer = 0
        return False

    def _send_burst(self) -> None:
        for item in self._rel.take():
            if item[0] == "btn":
                self.client.mouse_button(item[1], item[2])
            else:
                self.client.mouse_rel(item[1], item[2])

    def _on_mouse_mode(self, _c, absolute: bool) -> None:
        if absolute and self.grabbed:      # e.g. a USB tablet became active: no capture needed
            self.set_grab(False)

    def _button(self, gesture, n_press, x, y, pressed):
        self.grab_focus()
        if not self.client.absolute and not self.grabbed:
            if pressed:
                self.set_grab(True)
                pos = self._to_guest(x, y)
                if pos:     # the guest cursor jumps to where the host pointer is
                    self._rel.align(*pos, (self.client.width, self.client.height))
                    self._last_motion = (x, y)
                    self._send_rel()
            return
        btn = BUTTONS.get(gesture.get_current_button())
        if btn is not None:
            self._mouse_button(btn, pressed)

    def _mouse_button(self, btn: int, pressed: bool) -> None:
        if self.client.absolute:
            self.client.mouse_button(btn, pressed)
        else:       # after the moves still queued, so the click lands where the cursor goes
            self._rel.button(btn, pressed)
            self._send_rel()

    def _scroll(self, ctl, dx, dy):
        if dy == 0:
            return False
        b = 3 if dy < 0 else 4
        self._mouse_button(b, True)
        self._mouse_button(b, False)
        return True

    def set_grab(self, grab: bool) -> None:
        if grab == self.grabbed:
            return
        self.grabbed = grab
        self._last_motion = None
        self._rel.reset()
        if grab and not self.client.absolute:
            self.set_cursor(Gdk.Cursor.new_from_name("none", None))
        else:
            self.set_cursor(getattr(self, "_guest_cursor", None) or Gdk.Cursor.new_from_name("default", None))
        self.emit("grab-changed", grab)

    # --- cursor -----------------------------------------------------------------------

    def _on_cursor(self, _c, w, h, hx, hy, data):
        if w <= 0 or h <= 0 or len(data) < w * h * 4:
            return
        tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.B8G8R8A8, GLib.Bytes.new(bytes(data[:w * h * 4])), w * 4)
        self._guest_cursor = Gdk.Cursor.new_from_texture(tex, hx, hy, None)
        if self.client.absolute or not self.grabbed:
            self.set_cursor(self._guest_cursor)

    def _on_mouse_set(self, _c, x, y, visible):
        if self.client.absolute and not visible:
            self.set_cursor(Gdk.Cursor.new_from_name("none", None))
        elif visible and getattr(self, "_guest_cursor", None):
            self.set_cursor(self._guest_cursor)
