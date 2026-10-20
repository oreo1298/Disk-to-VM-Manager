"""Host Check window: `rehome doctor` in the GUI."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from rehome.ui.common import run_async, show_error  # noqa: E402

ICON = {"ok": "emblem-ok-symbolic", "info": "dialog-information-symbolic",
        "warning": "dialog-warning-symbolic", "error": "dialog-error-symbolic"}


class DoctorWindow(Adw.Window):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=False, title="Host Check",
                         default_width=720, default_height=720)
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        again = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Check again")
        again.connect("clicked", lambda *_: self.run(refresh=True))
        header.pack_start(again)
        tv.add_top_bar(header)
        self.stack = Gtk.Stack()
        spin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        spin.append(Gtk.Spinner(spinning=True, width_request=32, height_request=32))
        spin.append(Gtk.Label(label="Checking this computer…"))
        self.stack.add_named(spin, "busy")
        self.scroller = Gtk.ScrolledWindow()
        self.stack.add_named(self.scroller, "report")
        tv.set_content(self.stack)
        self.set_content(tv)
        self.run()

    def run(self, refresh: bool = False):
        from rehome.host import doctor
        self.stack.set_visible_child_name("busy")
        run_async(lambda: doctor.run(deep=True, refresh=refresh), self.show_report,
                  lambda e: show_error(self, e))

    def show_report(self, report):
        page = Adw.PreferencesPage()
        problems = report.problems
        summary = Adw.PreferencesGroup()
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        icon = Gtk.Image(icon_name="emblem-ok-symbolic" if not problems else "dialog-warning-symbolic",
                         pixel_size=64)
        icon.add_css_class("success" if not problems else "warning")
        title = Gtk.Label(label="Everything is ready" if not problems else
                          f"{len(problems)} thing{'s' if len(problems) != 1 else ''} to look at", wrap=True)
        title.add_css_class("title-2")
        desc = Gtk.Label(label="Rehome checked the CPU, KVM, QEMU, firmware and helper tools.", wrap=True)
        desc.add_css_class("dim-label")
        for w in (icon, title, desc):
            head.append(w)
        summary.add(head)
        page.add(summary)
        sections: dict[str, Adw.PreferencesGroup] = {}
        for chk in sorted(report.checks, key=lambda c: (c.status not in ("error", "warning"), 0)):
            g = sections.get(chk.section)
            if g is None:
                g = Adw.PreferencesGroup(title=chk.section)
                sections[chk.section] = g
                page.add(g)
            if chk.fix or chk.commands:
                row = Adw.ExpanderRow(title=GLib.markup_escape_text(chk.title),
                                      subtitle=GLib.markup_escape_text(chk.detail))
                if chk.fix:
                    fix = Adw.ActionRow(title="How to fix", subtitle=GLib.markup_escape_text(chk.fix))
                    fix.set_subtitle_selectable(True)
                    row.add_row(fix)
                for cmd in chk.commands:
                    cr = Adw.ActionRow(title=GLib.markup_escape_text(cmd))
                    cr.add_css_class("monospace")
                    cr.set_title_selectable(True)
                    copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                                      tooltip_text="Copy command")
                    copy.add_css_class("flat")
                    copy.connect("clicked", lambda b, c=cmd: b.get_clipboard().set(c))
                    cr.add_suffix(copy)
                    row.add_row(cr)
                if chk.status in ("error", "warning"):
                    row.set_expanded(True)
            else:
                row = Adw.ActionRow(title=GLib.markup_escape_text(chk.title),
                                    subtitle=GLib.markup_escape_text(chk.detail))
                row.set_subtitle_selectable(True)
            icon = Gtk.Image.new_from_icon_name(ICON.get(chk.status, "emblem-ok-symbolic"))
            if chk.status == "error":
                icon.add_css_class("error")
            elif chk.status == "warning":
                icon.add_css_class("warning")
            elif chk.status == "ok":
                icon.add_css_class("success")
            row.add_prefix(icon)
            g.add(row)
        self.scroller.set_child(page)
        self.stack.set_visible_child_name("report")
