"""Build an appliance from a VM (docs/APPLIANCE.md): options, progress, result."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from rehome import jobs  # noqa: E402
from rehome.appliance import base, build  # noqa: E402
from rehome.errors import Cancelled, RehomeError  # noqa: E402
from rehome.ui.common import combo_row, combo_value, run_async, show_error  # noqa: E402
from rehome.ui.progress import ProgressPanel  # noqa: E402
from rehome.util.units import human  # noqa: E402
from rehome.vm import library  # noqa: E402


def _page(title: str, child: Gtk.Widget, button: Gtk.Widget | None = None) -> Adw.NavigationPage:
    tv = Adw.ToolbarView()
    hb = Adw.HeaderBar()
    if button is not None:
        hb.pack_end(button)
    tv.add_top_bar(hb)
    tv.set_content(child)
    return Adw.NavigationPage(title=title, child=tv)


class ApplianceDialog(Adw.Window):
    def __init__(self, parent, cfg):
        super().__init__(transient_for=parent, modal=True, title="Build Appliance",
                         default_width=640, default_height=700)
        self.set_size_request(360, 400)
        self.main = parent
        self.cfg = cfg
        self.output: Path | None = None
        self.nav = Adw.NavigationView()
        self.set_content(self.nav)
        self.nav.add(self._options_page())

    # --- options ---------------------------------------------------------------------------

    def _options_page(self):
        pref = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup(description=(
            f"An appliance is a drive image that starts \"{self.cfg.name}\" full screen when a "
            "computer boots from it, with the computer's screen, keyboard, mouse, sound and USB "
            "ports. Shutting the VM down turns the computer off. A boot-menu entry (press Esc "
            "while it starts) opens a maintenance console instead."))
        pref.add(intro)

        where = Adw.PreferencesGroup(title="Image file")
        self.file_row = Adw.ActionRow(title="Save as", subtitle="Not chosen yet")
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose_file())
        self.file_row.add_suffix(choose)
        self.file_row.set_activatable_widget(choose)
        where.add(self.file_row)
        pref.add(where)

        opts = Adw.PreferencesGroup(title="Options")
        has_media = any(d.file for d in list(self.cfg.cdroms) + list(self.cfg.floppies))
        self.media_row = Adw.SwitchRow(
            title="Include install media",
            subtitle="Copy the VM's ISO and floppy images along, e.g. to finish an installation "
                     "on the target computer", active=has_media)
        self.media_row.set_sensitive(has_media)
        self.usb_row = Adw.SwitchRow(
            title="Give USB devices to the VM",
            subtitle="Plugged-in devices go to the VM automatically (not keyboards and mice)",
            active=True)
        self.net_row = combo_row("Network", ["Shared with the computer (NAT)", "None"],
                                 ["nat", "none"], "nat" if self.cfg.nics else "none")
        self.pw_row = Adw.PasswordEntryRow(title="Maintenance password (optional)")
        for r in (self.media_row, self.usb_row, self.net_row, self.pw_row):
            opts.add(r)
        pref.add(opts)

        info = Adw.PreferencesGroup(title="Good to know")
        cached = base.cached()
        notes = []
        if cached is None or base.is_stale(cached):
            notes.append("The first appliance downloads and prepares a base system (Ubuntu with "
                         "QEMU, about 1.2 GB). Later ones reuse it.")
        notes.append("On the target computer, turn Secure Boot off and virtualization (VT-x / "
                     "AMD-V) on in the firmware settings.")
        try:
            pl = build.plan(self.cfg, build.Options(output=Path("/nonexistent")))
            notes.append(f"The image will be about {human(pl.total_estimate)}; writing it to a "
                         "drive replaces everything on that drive.")
            notes += pl.warnings
            self.blocker = None
        except RehomeError as e:
            self.blocker = e
            notes.insert(0, e.message + (f" {e.fix}" if e.fix else ""))
        for n in notes:
            lbl = Gtk.Label(label=n, wrap=True, xalign=0)
            lbl.add_css_class("dim-label")
            info.add(lbl)
        pref.add(info)

        self.build_btn = Gtk.Button(label="Build")
        self.build_btn.add_css_class("suggested-action")
        self.build_btn.set_sensitive(False)
        self.build_btn.connect("clicked", lambda *_: self._build())
        return _page("Build Appliance", pref, self.build_btn)

    def _choose_file(self):
        dlg = Gtk.FileDialog(title="Save Appliance Image",
                             initial_name=f"{library.slug(self.cfg.name)}-appliance.img")

        def chosen(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            self.output = Path(f.get_path())
            self.file_row.set_subtitle(str(self.output))
            self.build_btn.set_sensitive(self.blocker is None)
        dlg.save(self, None, chosen)

    # --- build ------------------------------------------------------------------------------

    def _build(self):
        if self.output is None:
            return
        if self.output.exists():
            # The file dialog already asked about replacing it.
            self.output.unlink()
        opts = build.Options(output=self.output, include_media=self.media_row.get_active(),
                             maintenance_password=self.pw_row.get_text(),
                             network=combo_value(self.net_row), usb_auto=self.usb_row.get_active())
        panel = ProgressPanel("Building the appliance")
        prog = panel.bind(jobs.Progress(title="Building"))
        page = _page("Building", panel)
        page.set_can_pop(False)
        self.nav.push(page)

        def failed(e):
            self.nav.pop()
            if not isinstance(e, Cancelled):
                show_error(self, e, "The appliance could not be built")
        run_async(lambda: build.build(self.cfg, opts, progress=prog), self._done, failed)

    def _done(self, path: Path):
        page = Adw.StatusPage(
            icon_name="emblem-ok-symbolic", title="The appliance is ready",
            description=(f"{path.name} ({human(path.stat().st_size)} on disk). Write it to a drive "
                         "with Disks (\"Restore Disk Image…\"), balenaEtcher or dd, then boot the "
                         "target computer from that drive."))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        show = Gtk.Button(label="Show in Files")
        show.add_css_class("pill")
        close = Gtk.Button(label="Close")
        close.add_css_class("pill")
        close.add_css_class("suggested-action")
        box.append(show)
        box.append(close)
        page.set_child(box)
        nav_page = _page("Done", page)
        nav_page.set_can_pop(False)
        self.nav.push(nav_page)
        show.connect("clicked", lambda *_: Gtk.FileLauncher(file=Gio.File.new_for_path(
            str(path))).open_containing_folder(self, None, None))
        close.connect("clicked", lambda *_: self.close())
