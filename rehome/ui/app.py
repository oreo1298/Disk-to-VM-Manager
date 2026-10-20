"""Rehome's GTK application."""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from rehome import APP_ID, APP_NAME, __version__  # noqa: E402
from rehome.ui import theme  # noqa: E402
from rehome.ui.common import show_error  # noqa: E402

MIN_ADW = (1, 4)


class RehomeApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.consoles: dict[str, Gtk.Window] = {}
        self.add_main_option("console", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Open the console of a VM", "VM")

    def do_startup(self):
        Adw.Application.do_startup(self)
        theme.setup()
        icon_dir = self._icon_dir()
        if icon_dir:
            Gtk.IconTheme.get_for_display(Gtk.Window().get_display()).add_search_path(icon_dir)
        Gtk.Window.set_default_icon_name(APP_ID)
        for name, fn, accel in (
                ("new-vm", self.new_vm, "<Control>n"), ("import", self.import_disk, "<Control>i"),
                ("quick-create", self.quick_create, None), ("import-bundle", self.import_bundle, None),
                ("doctor", self.doctor, None), ("preferences", self.preferences, "<Control>comma"),
                ("about", self.about, None), ("quit", self.quit_app, "<Control>q")):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, f=fn: f())
            self.add_action(a)
            if accel:
                self.set_accels_for_action(f"app.{name}", [accel])

    def _icon_dir(self):
        from rehome import paths
        d = paths.data_files_dir() / "icons"
        return str(d) if d.is_dir() else None

    def do_command_line(self, cmdline):
        opts = cmdline.get_options_dict().end().unpack()
        self.activate()
        if opts.get("console"):
            from rehome.vm import library
            try:
                self.open_console(library.find(opts["console"]))
            except Exception as e:  # noqa: BLE001
                show_error(self.window, e)
        return 0

    def do_activate(self):
        if (Adw.get_major_version(), Adw.get_minor_version()) < MIN_ADW:
            dlg = Gtk.AlertDialog(message="libadwaita is too old",
                                  detail=f"Rehome's interface needs libadwaita {MIN_ADW[0]}.{MIN_ADW[1]} or "
                                         "newer. Use the command line (rehome --help) on this system.")
            dlg.show(None)
            return
        if self.window is None:
            from rehome.ui.window import MainWindow
            self.window = MainWindow(self)
            if not os.environ.get("REHOME_NO_FIRST_RUN"):
                GLib.idle_add(self._first_run_check)
        self.window.present()

    def _first_run_check(self):
        from rehome.ui import preferences
        preferences.first_run(self.window)
        return False

    # --- actions ----------------------------------------------------------------------

    def open_console(self, cfg):
        from rehome.ui.console.window import ConsoleWindow
        win = self.consoles.get(cfg.id)
        if win is None or not win.get_visible():
            win = ConsoleWindow(self, cfg)
            self.consoles[cfg.id] = win
            win.connect("close-request", lambda *_: self.consoles.pop(cfg.id, None) and False)
        win.present()

    def new_vm(self):
        from rehome.ui.wizards.new_vm import NewVmWizard
        NewVmWizard(self.window).present()

    def quick_create(self):
        from rehome.ui.wizards.new_vm import NewVmWizard
        NewVmWizard(self.window, start_with_catalog=True).present()

    def import_disk(self):
        from rehome.ui.wizards.import_vm import ImportWizard
        ImportWizard(self.window).present()

    def import_bundle(self):
        dlg = Gtk.FileDialog(title="Import an exported VM")
        f = Gtk.FileFilter()
        f.set_name("Rehome exports")
        f.add_pattern("*.rehome")
        dlg.set_default_filter(f)

        def done(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            from rehome.ui.common import run_async
            from rehome.vm import lifecycle
            self.window.toast("Importing…")
            run_async(lambda: lifecycle.import_bundle(path),
                      lambda cfg: (self.window.toast(f"Imported {cfg.name}"), self.window.select(cfg.id)),
                      lambda e: show_error(self.window, e))
        dlg.open(self.window, None, done)

    def doctor(self):
        from rehome.ui.doctor_window import DoctorWindow
        DoctorWindow(self.window).present()

    def preferences(self):
        from rehome.ui.preferences import PreferencesWindow
        PreferencesWindow(self.window).present()

    def about(self):
        about = Adw.AboutWindow(transient_for=self.window, application_name=APP_NAME,
                                application_icon=APP_ID, version=__version__,
                                developer_name="Rehome contributors",
                                license_type=Gtk.License.GPL_3_0,
                                website="https://github.com/oreo1298/Disk-to-VM-Manager",
                                issue_url="https://github.com/oreo1298/Disk-to-VM-Manager/issues",
                                comments="Turn installed operating systems into virtual machines, "
                                         "and create and manage VMs.")
        about.present()

    def quit_app(self):
        for w in list(self.get_windows()):
            w.close()
        self.quit()


def run(argv: list[str]) -> int:
    app = RehomeApp()
    return app.run(argv if argv else sys.argv[:1])
