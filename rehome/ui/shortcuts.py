"""The keyboard shortcuts window (Ctrl+?)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

GROUPS = [
    ("General", [("<Control>n", "New virtual machine"), ("<Control>i", "Import a disk or image"),
                 ("<Control>comma", "Preferences"), ("<Control>question", "Keyboard shortcuts"),
                 ("<Control>q", "Quit")]),
    ("Console", [("<Control><Alt>", "Release the keyboard and mouse"),
                 ("<Control><Alt>f", "Full screen")]),
]


def build() -> Gtk.ShortcutsWindow:
    """Built from XML so it also works with GTK < 4.14 (no add_group API)."""
    groups = []
    for title, items in GROUPS:
        rows = "".join(
            f'<child><object class="GtkShortcutsShortcut"><property name="accelerator">'
            f'{accel.replace("<", "&lt;").replace(">", "&gt;")}</property>'
            f'<property name="title">{label}</property></object></child>'
            for accel, label in items)
        groups.append(f'<child><object class="GtkShortcutsGroup"><property name="title">{title}'
                      f'</property>{rows}</object></child>')
    xml = ('<interface><object class="GtkShortcutsWindow" id="win"><property name="modal">true</property>'
           '<child><object class="GtkShortcutsSection"><property name="section-name">main</property>'
           + "".join(groups) + "</object></child></object></interface>")
    return Gtk.Builder.new_from_string(xml, -1).get_object("win")
