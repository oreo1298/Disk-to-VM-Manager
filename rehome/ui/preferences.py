"""Preferences window and the first-run / update-check flow."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from rehome import paths, update  # noqa: E402
from rehome.ui.common import run_async  # noqa: E402
from rehome.util import toml  # noqa: E402

FIRST_RUN_KEY = "first_run_done"
UPDATE_EXPLANATION = ("Once a day, Rehome asks GitHub for the number of the latest release. "
                      "Nothing about you or your VMs is sent, and there is no telemetry.")


def _mark_first_run_done() -> None:
    data = update.load_settings()
    data[FIRST_RUN_KEY] = True
    toml.dump_atomic(data, update.settings_path())


def first_run(window) -> None:
    settings = update.load_settings()
    if not settings.get(FIRST_RUN_KEY):
        check = Gtk.CheckButton(label="Check for updates", active=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(check)
        note = Gtk.Label(label=UPDATE_EXPLANATION, wrap=True, xalign=0, max_width_chars=50)
        note.add_css_class("dim-label")
        box.append(note)
        dlg = Adw.AlertDialog(heading="Welcome to Rehome",
                              body="Rehome turns installed systems into virtual machines without ever "
                                   "changing the original disk, and creates new VMs too.")
        dlg.set_extra_child(box)
        dlg.add_response("ok", "Get Started")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def done(_d, _r):
            update.set_enabled(check.get_active())
            _mark_first_run_done()
        dlg.connect("response", done)
        dlg.present(window)
    _background_doctor(window)
    run_async(update.maybe_check, lambda res: _show_update(window, res), lambda e: None)


def _background_doctor(window) -> None:
    from rehome.host import doctor

    def done(report):
        errors = [c for c in report.checks if c.status == "error"]
        if errors:
            t = Adw.Toast(title=f"This computer needs attention: {errors[0].title}", timeout=10,
                          button_label="Host Check")
            t.set_action_name("app.doctor")
            window.toast_overlay.add_toast(t)
    run_async(lambda: doctor.run(deep=False), done, lambda e: None)


def _show_update(window, result) -> None:
    if not result or not result.available:
        return
    t = Adw.Toast(title=f"Rehome {result.latest} is available", timeout=0, button_label="How to Update")
    t.connect("button-clicked", lambda *_: _how_to_update(window, result))
    window.toast_overlay.add_toast(t)


def _how_to_update(window, result) -> None:
    method = update.install_method()
    cmds = "\n".join(method.update_commands) or "Use your distribution's package manager."
    dlg = Adw.AlertDialog(heading=f"Update to Rehome {result.latest}",
                          body=f"Rehome was installed with {method.kind}. Run:\n\n{cmds}")
    dlg.add_response("ok", "OK")
    dlg.present(window)


class PreferencesWindow(Adw.PreferencesWindow):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Preferences")
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Updates", description=UPDATE_EXPLANATION)
        sw = Adw.SwitchRow(title="Check for updates", active=update.is_enabled())
        sw.connect("notify::active", lambda r, _p: update.set_enabled(r.get_active()))
        g.add(sw)
        now = Adw.ActionRow(title="Check now")
        b = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)

        def checked(res):
            if res.error:
                now.set_subtitle(f"Could not check: {res.error}")
            elif res.available:
                now.set_subtitle(f"Rehome {res.latest} is available (you have {res.current}).")
            else:
                now.set_subtitle(f"You have the latest version ({res.current}).")
        b.connect("clicked", lambda *_: (now.set_subtitle("Checking…"),
                                         run_async(lambda: update.check(force=True), checked)))
        now.add_suffix(b)
        g.add(now)
        page.add(g)
        g = Adw.PreferencesGroup(title="Storage")
        r = Adw.ActionRow(title="VM library", subtitle=GLib.markup_escape_text(str(paths.vms_dir())))
        r.set_subtitle_selectable(True)
        open_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Open")
        open_btn.add_css_class("flat")
        open_btn.connect("clicked", lambda *_: Gtk.FileLauncher.new(
            Gio.File.new_for_path(str(paths.vms_dir()))).launch(self, None, None))
        r.add_suffix(open_btn)
        g.add(r)
        g.add(self._disk_folder_row())
        page.add(g)
        self.add(page)

    def _disk_folder_row(self) -> Adw.ActionRow:
        """Default folder for new disk images (e.g. a larger second drive)."""
        from rehome.ui.common import show_error
        from rehome.ui.disk_location import choose_folder
        from rehome.vm import storage
        row = Adw.ActionRow(title="Folder for new disks")
        reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER,
                           tooltip_text="Store new disks in each VM's folder")
        reset.add_css_class("flat")
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)

        def show():
            folder = storage.default_disk_dir()
            row.set_subtitle(GLib.markup_escape_text(
                storage.describe(folder) if folder else
                "In each VM's folder, in the VM library. Choose a folder on a larger drive to keep "
                "new disks off this one."))
            reset.set_visible(folder is not None)

        def chosen(path):
            try:
                storage.set_default_disk_dir(path)
            except Exception as e:  # noqa: BLE001
                show_error(self, e)
            show()
        reset.connect("clicked", lambda *_: (storage.set_default_disk_dir(None), show()))
        change.connect("clicked", lambda *_: choose_folder(
            row, "Folder for new disk images", storage.default_disk_dir(), chosen))
        row.add_suffix(reset)
        row.add_suffix(change)
        show()
        return row
