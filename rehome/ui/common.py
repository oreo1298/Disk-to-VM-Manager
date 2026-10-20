"""Shared GTK helpers: background work, error dialogs, confirmations."""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from rehome.errors import Cancelled, RehomeError  # noqa: E402

log = logging.getLogger(__name__)


def run_async(fn: Callable[[], object], on_done: Callable[[object], None] | None = None,
              on_error: Callable[[BaseException], None] | None = None) -> threading.Thread:
    """Run ``fn`` in a worker thread; deliver its result on the main loop."""
    def worker():
        try:
            res = fn()
        except BaseException as e:  # delivered to the UI
            if not isinstance(e, (RehomeError, Cancelled)):
                log.exception("background task failed")
            GLib.idle_add(_deliver_error, e, on_error)
            return
        if on_done:
            GLib.idle_add(_deliver, on_done, res)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def _deliver(cb, res):
    cb(res)
    return False


def _deliver_error(e, cb):
    if cb:
        cb(e)
    elif not isinstance(e, Cancelled):
        show_error(None, e)
    return False


def as_rehome_error(e: BaseException) -> RehomeError:
    if isinstance(e, RehomeError):
        return e
    return RehomeError(f"Something unexpected went wrong: {e}",
                       fix="Please report this problem with the technical details.",
                       details="".join(traceback.format_exception(e)))


def _details_widget(details: str) -> Gtk.Widget:
    exp = Gtk.Expander(label="Technical details")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                        cursor_visible=False, top_margin=6, bottom_margin=6, left_margin=6,
                        right_margin=6)
    view.get_buffer().set_text(details)
    scroll = Gtk.ScrolledWindow(min_content_height=140, max_content_height=300,
                                propagate_natural_height=True, child=view)
    scroll.add_css_class("card")
    copy = Gtk.Button(label="Copy details", halign=Gtk.Align.END)
    copy.connect("clicked", lambda *_: copy.get_clipboard().set(details))
    box.append(scroll)
    box.append(copy)
    exp.set_child(box)
    return exp


def show_error(parent: Gtk.Widget | None, e: BaseException, title: str | None = None) -> None:
    err = as_rehome_error(e)
    body = err.message + (f"\n\n{err.fix}" if err.fix else "")
    window = parent.get_root() if parent is not None else None
    if hasattr(Adw, "AlertDialog"):
        dlg = Adw.AlertDialog(heading=title or "Something went wrong", body=body)
        dlg.add_response("close", "Close")
        if err.details:
            dlg.set_extra_child(_details_widget(err.details))
        dlg.present(window)
    else:  # libadwaita < 1.5
        dlg = Adw.MessageDialog(transient_for=window, heading=title or "Something went wrong", body=body)
        dlg.add_response("close", "Close")
        if err.details:
            dlg.set_extra_child(_details_widget(err.details))
        dlg.present()


def confirm(parent: Gtk.Widget, heading: str, body: str, action: str, on_yes: Callable[[], None],
            destructive: bool = False, extra: Gtk.Widget | None = None) -> None:
    window = parent.get_root() if parent is not None else None

    def responded(_dlg, response):
        if response == "yes":
            on_yes()
    if hasattr(Adw, "AlertDialog"):
        dlg = Adw.AlertDialog(heading=heading, body=body)
    else:
        dlg = Adw.MessageDialog(transient_for=window, heading=heading, body=body)
    dlg.add_response("cancel", "Cancel")
    dlg.add_response("yes", action)
    dlg.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                else Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("cancel" if destructive else "yes")
    dlg.set_close_response("cancel")
    if extra is not None:
        dlg.set_extra_child(extra)
    dlg.connect("response", responded)
    if hasattr(Adw, "AlertDialog") and isinstance(dlg, Adw.AlertDialog):
        dlg.present(window)
    else:
        dlg.present()


def toast(widget: Gtk.Widget, text: str, timeout: int = 3) -> None:
    root = widget.get_root() if widget else None
    overlay = getattr(root, "toast_overlay", None)
    if overlay is not None:
        overlay.add_toast(Adw.Toast(title=text, timeout=timeout))


def row(title: str, subtitle: str = "", icon: str = "") -> Adw.ActionRow:
    r = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
    if icon:
        r.add_prefix(Gtk.Image.new_from_icon_name(icon))
    return r


def string_list(items: list[str]) -> Gtk.StringList:
    return Gtk.StringList.new(items)


def combo_row(title: str, labels: list[str], values: list[str], current: str,
              subtitle: str = "") -> Adw.ComboRow:
    r = Adw.ComboRow(title=title, subtitle=subtitle, model=string_list(labels))
    r.values = values
    if current in values:
        r.set_selected(values.index(current))
    return r


def combo_value(r: Adw.ComboRow) -> str:
    i = r.get_selected()
    return r.values[i] if 0 <= i < len(r.values) else r.values[0]


def adw_at_least(major: int, minor: int) -> bool:
    return (Adw.get_major_version(), Adw.get_minor_version()) >= (major, minor)
