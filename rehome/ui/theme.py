"""Follow the desktop's colour scheme and accent colour.

libadwaita handles light/dark everywhere and, from 1.6, the accent colour —
both read from xdg-desktop-portal's ``org.freedesktop.appearance`` settings,
which GNOME and KDE Plasma (6.x portal) both provide, and both update live.
For libadwaita 1.4–1.5 we read ``accent-color`` from the portal ourselves
and apply it with CSS, also live.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

log = logging.getLogger(__name__)

PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
          "org.freedesktop.portal.Settings")


class AccentFallback:
    def __init__(self):
        self.provider = Gtk.CssProvider()
        self.proxy: Gio.DBusProxy | None = None
        display = Gdk.Display.get_default()
        if display is None:
            return
        Gtk.StyleContext.add_provider_for_display(display, self.provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE,
                                                        None, *PORTAL, None)
        except GLib.Error as e:
            log.debug("no settings portal: %s", e.message)
            return
        self.proxy.connect("g-signal", self._on_signal)
        self._read()

    def _read(self):
        try:
            v = self.proxy.call_sync("ReadOne", GLib.Variant("(ss)", ("org.freedesktop.appearance",
                                                                       "accent-color")),
                                     Gio.DBusCallFlags.NONE, 1000, None)
            value = v.unpack()[0]
        except GLib.Error:
            return
        self._apply(value)

    def _on_signal(self, _proxy, _sender, signal, params):
        if signal != "SettingChanged":
            return
        ns, key, value = params.unpack()
        if ns == "org.freedesktop.appearance" and key == "accent-color":
            self._apply(value)

    def _apply(self, value):
        try:
            r, g, b = (float(x) for x in value)
        except (TypeError, ValueError):
            return
        if not all(0.0 <= x <= 1.0 for x in (r, g, b)):
            self.provider.load_from_string("") if hasattr(self.provider, "load_from_string") else None
            return
        rgb = f"rgb({int(r * 255)}, {int(g * 255)}, {int(b * 255)})"
        css = (f"@define-color accent_bg_color {rgb};\n@define-color accent_color {rgb};\n"
               f"@define-color accent_fg_color white;\n")
        if hasattr(self.provider, "load_from_string"):
            self.provider.load_from_string(css)
        else:
            self.provider.load_from_data(css, -1)


_fallback = None


def setup() -> None:
    global _fallback
    style = Adw.StyleManager.get_default()
    style.set_color_scheme(Adw.ColorScheme.DEFAULT)   # follow the system
    if not hasattr(Adw, "AccentColor") and _fallback is None:
        _fallback = AccentFallback()
