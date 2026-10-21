"""The VM console window (like Hyper-V's VMConnect)."""

from __future__ import annotations

import logging
import os
import subprocess
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from rehome.ui.common import after_start, confirm, run_async, show_error  # noqa: E402
from rehome.ui.console.dbus_display import DisplayClient  # noqa: E402
from rehome.ui.console.view import DisplayView  # noqa: E402
from rehome.vm import library, runner, usb  # noqa: E402

log = logging.getLogger(__name__)


# Combinations the host desktop would otherwise catch.
KEY_COMBOS = [("Ctrl+Alt+Delete", "ctrl-alt-delete"), ("Ctrl+Alt+Backspace", "ctrl-alt-backspace"),
              ("Ctrl+Alt+F1", "ctrl-alt-f1"), ("Ctrl+Alt+F2", "ctrl-alt-f2"),
              ("Ctrl+Alt+F7", "ctrl-alt-f7"), ("Alt+Tab", "alt-tab"), ("Alt+F4", "alt-f4"),
              ("Print Screen", "print"), ("Super (Windows key)", "meta")]


class ConsoleWindow(Adw.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app, title=cfg.name, default_width=1060, default_height=760)
        self.cfg = cfg
        self.client: DisplayClient | None = None
        self.view: DisplayView | None = None
        self._ignore_clip = False
        self._inhibited = False
        self._poll_id = 0

        self.toast_overlay = Adw.ToastOverlay()
        self.toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title=cfg.name, subtitle="")
        header.set_title_widget(self.title)

        self.cad_btn = Gtk.Button(icon_name="input-keyboard-symbolic",
                                  tooltip_text="Send Ctrl+Alt+Delete")
        self.cad_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Send Ctrl+Alt+Delete"])
        self.cad_btn.connect("clicked", lambda *_: self.view and self.view.send_combo(["ctrl", "alt", "delete"]))
        header.pack_start(self.cad_btn)

        self.usb_btn = Gtk.MenuButton(icon_name="media-removable-symbolic",
                                      tooltip_text="USB devices")
        self.usb_btn.update_property([Gtk.AccessibleProperty.LABEL], ["USB devices"])
        self.usb_popover = Gtk.Popover()
        self.usb_btn.set_popover(self.usb_popover)
        self.usb_popover.connect("show", lambda *_: self._fill_usb())
        header.pack_start(self.usb_btn)

        fs = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="Full screen (Ctrl+Alt+F)")
        fs.update_property([Gtk.AccessibleProperty.LABEL], ["Full screen"])
        fs.connect("clicked", lambda *_: self.toggle_fullscreen())
        header.pack_end(fs)

        menu = Gio.Menu()
        power = Gio.Menu()
        power.append("Start", "win.start")
        power.append("Shut Down", "win.shutdown")
        power.append("Pause / Resume", "win.pause")
        power.append("Reset", "win.reset")
        power.append("Save State", "win.save")
        power.append("Turn Off", "win.force-off")
        menu.append_section(None, power)
        view = Gio.Menu()
        view.append("Scale to Window", "win.scale")
        view.append("Resize Guest with Window", "win.resize-guest")
        view.append("Take Screenshot…", "win.screenshot")
        menu.append_section(None, view)
        keys = Gio.Menu()
        for label, combo in KEY_COMBOS:
            keys.append(label, f"win.send-keys::{combo}")
        menu.append_submenu("Send Keys", keys)
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="VM menu")
        mb.update_property([Gtk.AccessibleProperty.LABEL], ["VM menu"])
        header.pack_end(mb)
        self._actions()

        self.toolbar.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.off_page = Adw.StatusPage(icon_name="system-shutdown-symbolic", title="The VM is off")
        start = Gtk.Button(label="Start", halign=Gtk.Align.CENTER)
        start.add_css_class("pill")
        start.add_css_class("suggested-action")
        start.connect("clicked", lambda *_: self.activate_action("win.start"))
        self._start_btn = start
        self.off_page.set_child(start)
        self.stack.add_named(self.off_page, "off")
        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        spinner_box.append(spinner)
        spinner_box.append(Gtk.Label(label="Connecting…"))
        self.stack.add_named(spinner_box, "connecting")
        self.display_holder = Gtk.Box()
        self.stack.add_named(self.display_holder, "display")
        self.toast_overlay.set_child(self.stack)
        self.toolbar.set_content(self.toast_overlay)
        self.set_content(self.toolbar)

        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control><Alt>f"),
                                           Gtk.CallbackAction.new(lambda *_: self.toggle_fullscreen() or True)))
        self.add_controller(keys)
        self.connect("close-request", self._on_close)
        self.connect("notify::fullscreened", self._on_fullscreen)
        self._poll_id = GLib.timeout_add_seconds(2, self._poll_state)
        self.refresh()

    # --- actions -----------------------------------------------------------------------

    def _actions(self):
        def act(name, fn):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: fn())
            self.add_action(a)
        act("start", self.start_vm)
        act("shutdown", lambda: self._op(runner.shutdown, "Asked the guest to shut down"))
        act("pause", self._pause_toggle)
        act("reset", lambda: confirm(self, "Reset the VM?", "Unsaved work in the VM is lost, like "
                                     "pressing a computer's reset button.", "Reset",
                                     lambda: self._op(runner.reset, "Reset"), destructive=True))
        act("save", lambda: self._op(runner.save, "State saved"))
        act("force-off", lambda: confirm(self, "Turn the VM off?", "This is like pulling the power "
                                         "plug: unsaved work in the VM is lost.", "Turn Off",
                                         lambda: self._op(runner.force_off, "Turned off"),
                                         destructive=True))
        act("screenshot", self._screenshot)
        send = Gio.SimpleAction.new("send-keys", GLib.VariantType.new("s"))
        send.connect("activate", lambda _a, combo: self.view and
                     self.view.send_combo(combo.get_string().split("-")))
        self.add_action(send)
        scale = Gio.SimpleAction.new_stateful("scale", None, GLib.Variant("b", True))
        scale.connect("change-state", self._scale_changed)
        self.add_action(scale)
        resize = Gio.SimpleAction.new_stateful("resize-guest", None, GLib.Variant("b", True))
        resize.connect("change-state", self._resize_changed)
        self.add_action(resize)

    def _op(self, fn, message):
        def done(_):
            self.toast(message)
            self.refresh()
        run_async(lambda: fn(self.cfg), done, lambda e: show_error(self, e))

    def _pause_toggle(self):
        st = runner.state(self.cfg).status
        self._op(runner.resume if st == runner.PAUSED else runner.pause,
                 "Resumed" if st == runner.PAUSED else "Paused")

    def _scale_changed(self, action, value):
        action.set_state(value)
        if self.view:
            self.view.scale_to_fit = value.get_boolean()
            self.view.queue_draw()

    def _resize_changed(self, action, value):
        action.set_state(value)
        if self.view:
            self.view.resize_guest = value.get_boolean()

    def toast(self, text: str):
        self.toast_overlay.add_toast(Adw.Toast(title=text, timeout=3))

    def start_vm(self):
        self.stack.set_visible_child_name("connecting")

        def started(_):
            after_start(self.cfg, self.toast)
            self.cfg = library.reload(self.cfg)
            self.refresh()
        run_async(lambda: runner.start(self.cfg), started,
                  lambda e: (show_error(self, e, "The VM could not start"), self.refresh()))

    # --- connection ------------------------------------------------------------------

    def refresh(self):
        if getattr(self, "_refreshing", False):
            return
        self._refreshing = True

        def done(st):
            self._refreshing = False
            self._apply_state(st)
        run_async(lambda: runner.state(self.cfg, timeout=1.0), done,
                  lambda e: setattr(self, "_refreshing", False))

    def _apply_state(self, st):
        self.title.set_subtitle(st.status.capitalize())
        if st.active:
            if self.client is None:
                self._connect()
        else:
            self._disconnect()
            self.stack.set_visible_child_name("off")
            self.off_page.set_child(self._start_btn)
            saved = st.status == runner.SAVED
            self.off_page.set_title("The VM's state is saved" if saved else "The VM is off")
            self.off_page.set_description("Start it to continue where it left off." if saved else None)

    def _poll_state(self):
        self.refresh()
        return True

    def _connect(self):
        self.stack.set_visible_child_name("connecting")
        client = DisplayClient()
        try:
            client.connect_qmp(runner.runtime(self.cfg).qmp_ui)
        except Exception as e:  # noqa: BLE001 - shown in the UI
            log.warning("console connection failed: %s", e)
            self._show_unavailable()
            return
        self.client = client
        client.connect("closed", lambda *_: GLib.idle_add(self.refresh))
        client.connect("guest-clipboard", self._guest_clipboard)
        client.host_clipboard_reader = self._read_host_clipboard
        self.view = DisplayView(client)
        self.view.connect("grab-changed", self._grab_changed)
        self.view.connect("release-combo", lambda *_: self.toast("Keyboard and mouse released"))
        child = self.display_holder.get_first_child()
        if child:
            self.display_holder.remove(child)
        self.display_holder.append(self.view)
        self.stack.set_visible_child_name("display")
        self.view.grab_focus()
        clip = self.get_display().get_clipboard()
        self._clip_handler = clip.connect("changed", self._host_clipboard_changed)
        if not client.absolute:
            self.toast("Click into the screen to capture the mouse; press Ctrl+Alt to release it", 5)

    def _show_unavailable(self):
        self.stack.set_visible_child_name("off")
        self.off_page.set_title("Cannot show this VM's screen")
        try:
            argv = runner.external_viewer(self.cfg)
        except Exception as e:  # noqa: BLE001 - explained on the page
            argv = None
            hint = getattr(e, "fix", "") or ""
        if argv:
            self.off_page.set_description("QEMU's D-Bus display module is not installed, so this VM "
                                          "uses a VNC or SPICE display. Open it in an external "
                                          "viewer, or run `rehome doctor` to fix the module.")
            btn = Gtk.Button(label="Open in External Viewer", halign=Gtk.Align.CENTER)
            btn.add_css_class("pill")
            btn.add_css_class("suggested-action")
            btn.connect("clicked", lambda *_: subprocess.Popen(
                argv, start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            self.off_page.set_child(btn)
        else:
            self.off_page.set_description("The VM is running, but its display could not be opened. "
                                          + (hint or "Run `rehome doctor`."))
            self.off_page.set_child(None)

    def _disconnect(self):
        if self.client is not None:
            try:
                self.get_display().get_clipboard().disconnect(self._clip_handler)
            except (TypeError, AttributeError):
                pass
            self.client.close()
            self.client = None
        if self.view is not None:
            self.display_holder.remove(self.view)
            self.view = None
        self._set_inhibit(False)

    # --- clipboard -----------------------------------------------------------------------

    def _host_clipboard_changed(self, clip):
        if self._ignore_clip:
            self._ignore_clip = False
            return
        if self.client and not clip.is_local():
            self.client.host_clipboard_changed()

    def _read_host_clipboard(self, reply):
        clip = self.get_display().get_clipboard()

        def done(c, res):
            try:
                reply(c.read_text_finish(res))
            except GLib.Error:
                reply(None)
        clip.read_text_async(None, done)

    def _guest_clipboard(self, _client, text):
        self._ignore_clip = True
        self.get_display().get_clipboard().set(text)

    # --- grab, fullscreen ---------------------------------------------------------------

    def _grab_changed(self, view, grabbed):
        self._set_inhibit(grabbed or self.is_fullscreen())
        if grabbed:
            self.toast("Press Ctrl+Alt to release the mouse and keyboard")

    def _set_inhibit(self, on: bool):
        surface = self.get_surface()
        if surface is None or not hasattr(surface, "inhibit_system_shortcuts"):
            return
        if on and not self._inhibited:
            surface.inhibit_system_shortcuts(None)
            self._inhibited = True
        elif not on and self._inhibited:
            surface.restore_system_shortcuts()
            self._inhibited = False

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen(self, *_):
        fs = self.is_fullscreen()
        self.toolbar.set_reveal_top_bars(not fs)
        self._set_inhibit(fs)
        if fs:
            self.toast("Press Ctrl+Alt+F to leave full screen")

    # --- USB -----------------------------------------------------------------------------

    def _fill_usb(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        if not runner.is_running(self.cfg):
            box.append(Gtk.Label(label="Start the VM to connect USB devices."))
        elif self.cfg.usb.controller == "none":
            box.append(Gtk.Label(label="This VM has no USB controller.", wrap=True))
        else:
            current = usb.attached(self.cfg)
            devices = usb.list_host_devices()
            if not devices:
                box.append(Gtk.Label(label="No USB devices found."))
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            lb.add_css_class("boxed-list")
            for dev in devices:
                row = Adw.SwitchRow(title=GLib.markup_escape_text(dev.label),
                                    subtitle=f"{dev.vendor_id}:{dev.product_id} on bus {dev.bus}")
                row.set_active(dev.qom_id in current)
                row.connect("notify::active", self._usb_toggled, dev)
                lb.append(row)
            box.append(lb)
            note = Gtk.Label(label="Connected devices disappear from this computer while the VM uses them.",
                             wrap=True, max_width_chars=40, xalign=0)
            note.add_css_class("dim-label")
            note.add_css_class("caption")
            box.append(note)
        self.usb_popover.set_child(box)

    def _usb_toggled(self, row, _pspec, dev):
        fn = usb.attach if row.get_active() else usb.detach
        msg = f"{'Connected' if row.get_active() else 'Disconnected'} {dev.label}"
        run_async(lambda: fn(self.cfg, dev), lambda _: self.toast(msg),
                  lambda e: (show_error(self, e), row.set_active(not row.get_active())))

    # --- misc ----------------------------------------------------------------------------

    def _screenshot(self):
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser("~")
        path = os.path.join(pictures, f"{self.cfg.name} {time.strftime('%Y-%m-%d %H-%M-%S')}.png")
        run_async(lambda: runner.screenshot(self.cfg, path), lambda _: self.toast(f"Saved {path}"),
                  lambda e: show_error(self, e))

    def _on_close(self, *_):
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        self._disconnect()
        return False
