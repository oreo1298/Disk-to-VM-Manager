"""A row for choosing where a new disk image is stored (rehome/vm/storage.py)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from rehome.ui.common import show_error  # noqa: E402
from rehome.vm import storage  # noqa: E402


def choose_folder(parent: Gtk.Widget, title: str, current, on_chosen) -> None:
    """Folder picker; ``on_chosen(path)`` only for folders new disks can be written to."""
    dlg = Gtk.FileDialog(title=title)
    if current:
        dlg.set_initial_folder(Gio.File.new_for_path(str(current)))

    def done(d, res):
        try:
            path = d.select_folder_finish(res).get_path()
        except GLib.Error:
            return
        if not path:
            return
        try:
            storage.check_folder(path)
        except Exception as e:  # noqa: BLE001
            show_error(parent, e)
            return
        on_chosen(path)
    dlg.select_folder(parent.get_root(), None, done)


class DiskLocationRow(Adw.ActionRow):
    """Where a new disk goes: the default folder (Preferences) or one chosen here.

    ``folder`` is None for the default, as ``storage.new_disk_path`` expects.
    """

    def __init__(self, cfg=None, title: str = "Disk location", need: int = 0):
        super().__init__(title=title)
        self.cfg, self.need, self.folder = cfg, need, None
        self.reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Use the default location", visible=False)
        self.reset.add_css_class("flat")
        self.reset.connect("clicked", lambda *_: self._set(None))
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", lambda *_: choose_folder(
            self, "Choose a folder for the disk image",
            self.folder or storage.default_disk_dir(), self._set))
        self.add_suffix(self.reset)
        self.add_suffix(change)
        self.refresh()

    def set_need(self, need: int) -> None:
        self.need = need
        self.refresh()

    def _set(self, folder) -> None:
        self.folder = folder
        self.refresh()

    def refresh(self) -> None:
        folder = self.folder or storage.default_disk_dir()
        text = storage.describe(folder, self.cfg, self.need)
        self.set_subtitle(GLib.markup_escape_text(text))
        self.reset.set_visible(self.folder is not None)
