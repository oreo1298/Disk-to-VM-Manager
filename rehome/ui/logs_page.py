"""Logs tab: QEMU, serial console, import and patch logs, and "Copy diagnostics"."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from rehome.ui.common import combo_row, combo_value, run_async, show_error  # noqa: E402

LOGS = [("qemu.log", "QEMU"), ("serial.log", "Serial console"), ("import.log", "Import"),
        ("patch-summary.txt", "Automatic fixes"), ("patch.log", "Automatic fixes (detailed)"),
        ("virt-v2v.log", "virt-v2v"), ("swtpm.log", "TPM")]


class LogsPage(Gtk.Box):
    def __init__(self, page):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12,
                         margin_bottom=12, margin_start=18, margin_end=18)
        self.page = page
        bar = Gtk.Box(spacing=6)
        self.which = combo_row("Log", [l for _, l in LOGS], [f for f, _ in LOGS], "qemu.log")
        self.which.connect("notify::selected", lambda *_: self.reload())
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, hexpand=True)
        lb.add_css_class("boxed-list")
        lb.append(self.which)
        bar.append(lb)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload", valign=Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self.reload())
        bar.append(refresh)
        diag = Gtk.Button(label="Copy Diagnostics", valign=Gtk.Align.CENTER,
                          tooltip_text="Copy host and VM details for a bug report")
        diag.connect("clicked", lambda *_: self.copy_diagnostics())
        bar.append(diag)
        self.append(bar)
        self.view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, left_margin=6)
        sw = Gtk.ScrolledWindow(child=self.view, vexpand=True)
        sw.add_css_class("card")
        self.append(sw)
        self.connect("map", lambda *_: self.reload())

    def reload(self):
        f = self.page.cfg.dir / "logs" / combo_value(self.which)
        try:
            lines = f.read_text(errors="replace").splitlines()[-2000:]
            text = "\n".join(lines) or "(empty)"
        except OSError:
            text = "(no log yet)"
        buf = self.view.get_buffer()
        buf.set_text(text)
        buf.place_cursor(buf.get_end_iter())
        self.view.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)

    def copy_diagnostics(self):
        from rehome.vm import diagnostics
        cfg = self.page.cfg
        run_async(lambda: diagnostics.collect(cfg),
                  lambda text: (self.get_clipboard().set(text),
                                self.page.window.toast("Diagnostics copied to the clipboard")),
                  lambda e: show_error(self, e))
