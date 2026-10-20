"""Client for QEMU's D-Bus display (``-display dbus,p2p=yes``).

Peer-to-peer setup:
 1. Over QMP: ``getfd`` (one end of a socketpair, via SCM_RIGHTS) and
    ``add_client protocol=@dbus-display`` — QEMU becomes the D-Bus server on it.
 2. We connect as a D-Bus client and talk to ``/org/qemu/Display1/VM`` and
    ``/org/qemu/Display1/Console_N``.
 3. ``Console.RegisterListener(fd)`` with another socketpair. QEMU is the
    authenticating server on that one too; we export
    ``/org/qemu/Display1/Listener`` and receive Scanout/Update/... calls.

The framebuffer is kept as 32-bit BGRX (``self.fb``). Nothing here depends
on GTK, so it can be tested headless. Every callback runs in the GLib main
context that created the client.
"""

from __future__ import annotations

import logging
import mmap
import os
import socket

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib, GObject  # noqa: E402

from rehome.vm.qmp import QMP  # noqa: E402

log = logging.getLogger(__name__)

PIXMAN_X8R8G8B8 = 0x20020888
PIXMAN_A8R8G8B8 = 0x20028888
PIXMAN_R5G6B5 = 0x10020565
PIXMAN_X1R5G5B5 = 0x10020555
PIXMAN_B8G8R8X8 = 0x20030888
PIXMAN_R8G8B8 = 0x18020888

LISTENER_XML = """<node>
<interface name="org.qemu.Display1.Listener">
 <method name="Scanout"><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="ay" direction="in"/></method>
 <method name="Update"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="ay" direction="in"/></method>
 <method name="ScanoutDMABUF"><arg type="h" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="t" direction="in"/><arg type="b" direction="in"/></method>
 <method name="UpdateDMABUF"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="Disable"/>
 <method name="MouseSet"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
 <method name="CursorDefine"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="ay" direction="in"/></method>
 <property name="Interfaces" type="as" access="read"/>
</interface>
<interface name="org.qemu.Display1.Listener.Unix.Map">
 <method name="ScanoutMap"><arg type="h" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="u" direction="in"/></method>
 <method name="UpdateMap"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
</interface>
</node>"""

CLIPBOARD_XML = """<node><interface name="org.qemu.Display1.Clipboard">
 <method name="Register"/>
 <method name="Unregister"/>
 <method name="Grab"><arg type="u" name="selection"/><arg type="u" name="serial"/><arg type="as" name="mimes"/></method>
 <method name="Release"><arg type="u" name="selection"/></method>
 <method name="Request"><arg type="u" name="selection" direction="in"/><arg type="as" name="mimes" direction="in"/><arg type="s" name="reply_mime" direction="out"/><arg type="ay" name="data" direction="out"/></method>
 <property name="Interfaces" type="as" access="read"/>
</interface></node>"""

TEXT_MIMES = ["text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING"]


def _to_bgrx(data: bytes, w: int, h: int, stride: int, fmt: int) -> bytearray | None:
    """Convert a guest surface to tightly packed 32-bit BGRX."""
    if fmt in (PIXMAN_X8R8G8B8, PIXMAN_A8R8G8B8):
        if stride == w * 4:
            return bytearray(data[:w * 4 * h])
        out = bytearray(w * 4 * h)
        for y in range(h):
            out[y * w * 4:(y + 1) * w * 4] = data[y * stride:y * stride + w * 4]
        return out
    if fmt in (PIXMAN_R5G6B5, PIXMAN_X1R5G5B5):
        import cairo
        cfmt = cairo.FORMAT_RGB16_565
        if fmt == PIXMAN_X1R5G5B5:
            return _x1r5g5b5_to_bgrx(data, w, h, stride)
        src_stride = cairo.ImageSurface.format_stride_for_width(cfmt, w)
        buf = bytearray(src_stride * h)
        for y in range(h):
            buf[y * src_stride:y * src_stride + w * 2] = data[y * stride:y * stride + w * 2]
        src = cairo.ImageSurface.create_for_data(buf, cfmt, w, h, src_stride)
        dst = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
        cr = cairo.Context(dst)
        cr.set_source_surface(src, 0, 0)
        cr.paint()
        dst.flush()
        dstride = dst.get_stride()
        raw = dst.get_data()
        if dstride == w * 4:
            return bytearray(raw)
        return bytearray(b"".join(bytes(raw[y * dstride:y * dstride + w * 4]) for y in range(h)))
    if fmt == PIXMAN_R8G8B8:
        out = bytearray(w * 4 * h)
        for y in range(h):
            row = data[y * stride:y * stride + w * 3]
            o = y * w * 4
            out[o:o + w * 4:4] = row[0::3]      # B
            out[o + 1:o + w * 4:4] = row[1::3]  # G
            out[o + 2:o + w * 4:4] = row[2::3]  # R
        return out
    return None


def _x1r5g5b5_to_bgrx(data: bytes, w: int, h: int, stride: int) -> bytearray:
    import array
    out = bytearray(w * 4 * h)
    for y in range(h):
        px = array.array("H", data[y * stride:y * stride + w * 2])
        o = y * w * 4
        for x, p in enumerate(px):
            out[o + x * 4] = (p & 0x1F) << 3
            out[o + x * 4 + 1] = ((p >> 5) & 0x1F) << 3
            out[o + x * 4 + 2] = ((p >> 10) & 0x1F) << 3
    return out


class DisplayClient(GObject.Object):
    __gsignals__ = {
        "scanout": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        "update": (GObject.SignalFlags.RUN_FIRST, None, (int, int, int, int)),
        "dmabuf": (GObject.SignalFlags.RUN_FIRST, None, (int, int, int, int, int, GObject.TYPE_UINT64, bool)),
        "dmabuf-update": (GObject.SignalFlags.RUN_FIRST, None, (int, int, int, int)),
        "disabled": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "cursor-define": (GObject.SignalFlags.RUN_FIRST, None, (int, int, int, int, object)),
        "mouse-set": (GObject.SignalFlags.RUN_FIRST, None, (int, int, bool)),
        "mouse-mode": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "guest-clipboard": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "closed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.conn: Gio.DBusConnection | None = None
        self.lconn: Gio.DBusConnection | None = None
        self.console = self.keyboard = self.mouse = self.clipboard = None
        self.width = self.height = 0
        self.fb = bytearray()
        self.map: mmap.mmap | None = None
        self.map_stride = 0
        self.dmabuf_fd = -1
        self.absolute = True
        self.label = ""
        self.console_ids: list[int] = []
        self._reg_ids: list[int] = []
        self._clip_serial = 0
        self._clip_registered = False
        self.host_clipboard_reader = None   # callable(callback(str|None)) set by the UI
        self._pending_listener = None

    # --- connection ----------------------------------------------------------------

    def connect_qmp(self, qmp_path: str, console_id: int = 0) -> None:
        """Attach to a running VM through its QMP socket (blocking, quick)."""
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with QMP(qmp_path, timeout=10).connect(retries=3) as q:
            q.send_fd(theirs.fileno(), "rehome-dbus")
            q.execute("add_client", {"protocol": "@dbus-display", "fdname": "rehome-dbus"})
        theirs.close()
        self._connect_stream(ours, console_id)

    def _connect_stream(self, sock: socket.socket, console_id: int) -> None:
        gsock = Gio.Socket.new_from_fd(sock.detach())
        stream = gsock.connection_factory_create_connection()
        self.conn = Gio.DBusConnection.new_sync(stream, None,
                                                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT, None, None)
        self.conn.connect("closed", lambda *_: self.emit("closed"))
        vm = self._proxy("/org/qemu/Display1/VM", "org.qemu.Display1.VM")
        ids = vm.get_cached_property("ConsoleIDs")
        self.console_ids = list(ids.unpack()) if ids else [0]
        path = f"/org/qemu/Display1/Console_{console_id}"
        self.console = self._proxy(path, "org.qemu.Display1.Console")
        lab = self.console.get_cached_property("Label")
        self.label = lab.unpack() if lab else ""
        w, h = self.console.get_cached_property("Width"), self.console.get_cached_property("Height")
        self.width, self.height = (w.unpack() if w else 0), (h.unpack() if h else 0)
        self.keyboard = self._proxy(path, "org.qemu.Display1.Keyboard")
        self.mouse = self._proxy(path, "org.qemu.Display1.Mouse")
        ab = self.mouse.get_cached_property("IsAbsolute")
        self.absolute = bool(ab.unpack()) if ab else True
        self.mouse.connect("g-properties-changed", self._mouse_props)
        # QEMU (8.x) does not send a new Scanout when the guest changes video
        # mode; only the console's Width/Height properties change.
        self.console.connect("g-properties-changed", self._console_props)
        # Every blocking call must happen before RegisterListener: QEMU handles
        # it by authenticating the listener connection synchronously, which only
        # completes once our main loop runs. A *_sync call made after it would
        # deadlock both sides until the D-Bus timeout (25 s).
        self._setup_clipboard()
        self._register_listener()

    def _proxy(self, path: str, iface: str) -> Gio.DBusProxy:
        return Gio.DBusProxy.new_sync(self.conn, Gio.DBusProxyFlags.NONE, None, None, path, iface, None)

    def _register_listener(self) -> None:
        info = Gio.DBusNodeInfo.new_for_xml(LISTENER_XML)
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        lstream = Gio.Socket.new_from_fd(a.detach()).connection_factory_create_connection()

        def on_conn(_src, res):
            try:
                self.lconn = Gio.DBusConnection.new_finish(res)
            except GLib.Error as e:
                log.warning("display listener connection failed: %s", e.message)
                return
            for iface in info.interfaces:
                self._reg_ids.append(self.lconn.register_object(
                    "/org/qemu/Display1/Listener", iface, self._on_listener_call,
                    self._on_listener_prop, None))
            self.lconn.start_message_processing()

        # QEMU authenticates as the server on this socket; we are the client.
        Gio.DBusConnection.new(lstream, None,
                               Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
                               Gio.DBusConnectionFlags.DELAY_MESSAGE_PROCESSING,
                               None, None, on_conn)
        fdl = Gio.UnixFDList.new()
        idx = fdl.append(b.fileno())
        b.close()
        self.console.call_with_unix_fd_list("RegisterListener", GLib.Variant("(h)", (idx,)),
                                            Gio.DBusCallFlags.NONE, -1, fdl, None, None)

    def close(self) -> None:
        for conn in (self.lconn, self.conn):
            if conn is not None and not conn.is_closed():
                try:
                    conn.close_sync(None)
                except GLib.Error:
                    pass
        self.lconn = self.conn = None
        if self.map is not None:
            self.map.close()
            self.map = None
        if self.dmabuf_fd >= 0:
            os.close(self.dmabuf_fd)
            self.dmabuf_fd = -1

    # --- listener (QEMU -> us) ---------------------------------------------------------

    def _on_listener_prop(self, _conn, _sender, _path, _iface, prop):
        if prop == "Interfaces":
            return GLib.Variant("as", ["org.qemu.Display1.Listener.Unix.Map"])
        return None

    def _on_listener_call(self, _conn, _sender, _path, _iface, method, params, inv):
        try:
            getattr(self, f"_m_{method}")(params, inv)
        except Exception:  # never leave QEMU waiting on a reply
            log.exception("display listener %s failed", method)
        inv.return_value(None)

    def _m_Scanout(self, p, _inv):
        w, h, stride, fmt = (p.get_child_value(i).unpack() for i in range(4))
        data = p.get_child_value(4).get_data_as_bytes().get_data()
        fb = _to_bgrx(data, w, h, stride, fmt)
        if fb is None:
            log.warning("unsupported pixel format 0x%x", fmt)
            return
        self._drop_map()
        self.width, self.height, self.fb = w, h, fb
        self.emit("scanout", w, h)

    def _m_Update(self, p, _inv):
        x, y, w, h = (p.get_child_value(i).unpack() for i in range(4))
        stride, fmt = p.get_child_value(4).unpack(), p.get_child_value(5).unpack()
        data = p.get_child_value(6).get_data_as_bytes().get_data()
        if self.map is not None or not self.fb or w <= 0 or h <= 0:
            return
        if x + w > self.width or y + h > self.height:
            # Updates for a larger new surface can arrive before the size change.
            self._resize(max(self.width, x + w), max(self.height, y + h))
        rect = _to_bgrx(data, w, h, stride, fmt)   # packed rows of w * 4 bytes
        if rect is None:
            return
        src_row = w * 4
        x0, y0 = max(0, x), max(0, y)
        cw, ch = min(x + w, self.width) - x0, min(y + h, self.height) - y0
        if cw <= 0 or ch <= 0:
            return
        sx, sy = x0 - x, y0 - y
        for row in range(ch):
            dst = ((y0 + row) * self.width + x0) * 4
            src = (sy + row) * src_row + sx * 4
            self.fb[dst:dst + cw * 4] = rect[src:src + cw * 4]
        self.emit("update", x0, y0, cw, ch)

    def _resize(self, w: int, h: int) -> None:
        """Change the plain framebuffer's size, keeping the overlapping pixels."""
        if (w, h) == (self.width, self.height) or w <= 0 or h <= 0:
            return
        fb = bytearray(w * h * 4)
        cw, ch = min(w, self.width), min(h, self.height)
        for row in range(ch):
            fb[row * w * 4:(row * w + cw) * 4] = self.fb[row * self.width * 4:(row * self.width + cw) * 4]
        self.width, self.height, self.fb = w, h, fb
        self.emit("scanout", w, h)

    def _console_props(self, _proxy, changed, _invalidated):
        w, h = changed.lookup_value("Width", None), changed.lookup_value("Height", None)
        if (w is None and h is None) or self.map is not None or self.dmabuf_fd >= 0 or not self.fb:
            return
        self._resize(w.unpack() if w is not None else self.width,
                     h.unpack() if h is not None else self.height)

    def _m_ScanoutMap(self, p, inv):
        handle = p.get_child_value(0).get_handle()
        offset, w, h, stride, fmt = (p.get_child_value(i).unpack() for i in range(1, 6))
        fdl = inv.get_message().get_unix_fd_list()
        if fdl is None or fmt not in (PIXMAN_X8R8G8B8, PIXMAN_A8R8G8B8):
            return
        fd = fdl.get(handle)
        try:
            page = mmap.ALLOCATIONGRANULARITY
            base = offset - offset % page
            self._drop_map()
            self.map = mmap.mmap(fd, stride * h + (offset - base), mmap.MAP_SHARED,
                                 mmap.PROT_READ, offset=base)
            self._map_skip = offset - base
        finally:
            os.close(fd)
        self.width, self.height, self.map_stride = w, h, stride
        self._refresh_from_map()
        self.emit("scanout", w, h)

    def _m_UpdateMap(self, p, _inv):
        x, y, w, h = (p.get_child_value(i).unpack() for i in range(4))
        self._refresh_from_map()
        self.emit("update", x, y, w, h)

    def _refresh_from_map(self) -> None:
        if self.map is None:
            return
        W, H, S = self.width, self.height, self.map_stride
        skip = getattr(self, "_map_skip", 0)
        if S == W * 4:
            self.fb = bytearray(self.map[skip:skip + W * 4 * H])
        else:
            self.fb = bytearray(b"".join(self.map[skip + y * S:skip + y * S + W * 4] for y in range(H)))

    def _drop_map(self) -> None:
        if self.map is not None:
            self.map.close()
            self.map = None

    def _m_ScanoutDMABUF(self, p, inv):
        handle = p.get_child_value(0).get_handle()
        w, h, stride, fourcc = (p.get_child_value(i).unpack() for i in range(1, 5))
        modifier, y0_top = p.get_child_value(5).unpack(), p.get_child_value(6).unpack()
        fdl = inv.get_message().get_unix_fd_list()
        if fdl is None:
            return
        if self.dmabuf_fd >= 0:
            os.close(self.dmabuf_fd)
        self.dmabuf_fd = fdl.get(handle)
        self.width, self.height = w, h
        self.emit("dmabuf", self.dmabuf_fd, w, h, stride, fourcc, modifier, y0_top)

    def _m_UpdateDMABUF(self, p, _inv):
        x, y, w, h = (p.get_child_value(i).unpack() for i in range(4))
        self.emit("dmabuf-update", x, y, w, h)

    def _m_Disable(self, _p, _inv):
        self.emit("disabled")

    def _m_MouseSet(self, p, _inv):
        x, y, on = (p.get_child_value(i).unpack() for i in range(3))
        self.emit("mouse-set", x, y, bool(on))

    def _m_CursorDefine(self, p, _inv):
        w, h, hx, hy = (p.get_child_value(i).unpack() for i in range(4))
        data = p.get_child_value(4).get_data_as_bytes().get_data()
        self.emit("cursor-define", w, h, hx, hy, data)

    def _mouse_props(self, proxy, changed, _invalidated):
        v = changed.lookup_value("IsAbsolute", None)
        if v is not None:
            self.absolute = bool(v.unpack())
            self.emit("mouse-mode", self.absolute)

    # --- input (us -> QEMU) -------------------------------------------------------------

    def _call(self, proxy, method: str, sig: str, *args) -> None:
        if proxy is None or self.conn is None or self.conn.is_closed():
            return
        proxy.call(method, GLib.Variant(f"({sig})", args) if sig else None,
                   Gio.DBusCallFlags.NONE, -1, None, None)

    def key_press(self, qnum: int) -> None:
        self._call(self.keyboard, "Press", "u", qnum)

    def key_release(self, qnum: int) -> None:
        self._call(self.keyboard, "Release", "u", qnum)

    def mouse_abs(self, x: int, y: int) -> None:
        if self.width and self.height:
            x = max(0, min(self.width - 1, int(x)))
            y = max(0, min(self.height - 1, int(y)))
        self._call(self.mouse, "SetAbsPosition", "uu", x, y)

    def mouse_rel(self, dx: int, dy: int) -> None:
        self._call(self.mouse, "RelMotion", "ii", int(dx), int(dy))

    def mouse_button(self, button: int, pressed: bool) -> None:
        self._call(self.mouse, "Press" if pressed else "Release", "u", button)

    def set_ui_info(self, width: int, height: int, width_mm: int = 0, height_mm: int = 0) -> None:
        self._call(self.console, "SetUIInfo", "qqiiuu", width_mm, height_mm, 0, 0, width, height)

    # --- clipboard ----------------------------------------------------------------------

    def _setup_clipboard(self) -> None:
        try:
            info = Gio.DBusNodeInfo.new_for_xml(CLIPBOARD_XML)
            self._reg_ids.append(self.conn.register_object(
                "/org/qemu/Display1/Clipboard", info.interfaces[0], self._on_clip_call,
                lambda *a: GLib.Variant("as", []), None))
            self.clipboard = self._proxy("/org/qemu/Display1/Clipboard", "org.qemu.Display1.Clipboard")
            self.clipboard.call("Register", None, Gio.DBusCallFlags.NONE, -1, None,
                                self._clip_registered_cb)
        except GLib.Error as e:
            log.debug("clipboard unavailable: %s", e.message)

    def _clip_registered_cb(self, proxy, res):
        try:
            proxy.call_finish(res)
            self._clip_registered = True
        except GLib.Error as e:
            log.debug("clipboard register failed (no qemu-vdagent?): %s", e.message)

    @property
    def clipboard_available(self) -> bool:
        return self._clip_registered

    def host_clipboard_changed(self) -> None:
        """Tell the guest the host clipboard has text (it asks for it on paste)."""
        if not self._clip_registered:
            return
        self._clip_serial += 1
        self.clipboard.call("Grab", GLib.Variant("(uuas)", (0, self._clip_serial, TEXT_MIMES[:2])),
                            Gio.DBusCallFlags.NONE, -1, None, None)

    def _on_clip_call(self, _conn, _sender, _path, _iface, method, params, inv):
        if method == "Grab":
            sel, serial, mimes = params.unpack()
            self._clip_serial = max(self._clip_serial, serial)
            if sel == 0 and any(m in TEXT_MIMES for m in mimes):
                self.clipboard.call("Request", GLib.Variant("(uas)", (0, TEXT_MIMES)),
                                    Gio.DBusCallFlags.NONE, -1, None, self._got_guest_text)
            inv.return_value(None)
        elif method == "Request":
            sel, _mimes = params.unpack()

            def reply(text):
                if text is None:
                    inv.return_dbus_error("org.qemu.Display1.Error.Failed", "clipboard empty")
                else:
                    inv.return_value(GLib.Variant("(say)", (TEXT_MIMES[0], text.encode())))
            if self.host_clipboard_reader:
                self.host_clipboard_reader(reply)
            else:
                reply(None)
        else:  # Register / Unregister / Release
            inv.return_value(None)

    def _got_guest_text(self, proxy, res):
        try:
            mime, data = proxy.call_finish(res).unpack()
        except GLib.Error as e:
            log.debug("clipboard request failed: %s", e.message)
            return
        text = bytes(data).decode("utf-8", errors="replace").rstrip("\x00")
        self.emit("guest-clipboard", text)
