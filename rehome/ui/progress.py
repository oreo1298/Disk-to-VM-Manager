"""A progress panel bound to a rehome.jobs.Progress (thread-safe)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from rehome import jobs  # noqa: E402
from rehome.util.units import human_duration  # noqa: E402


class ProgressPanel(Gtk.Box):
    def __init__(self, title: str = ""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER,
                         margin_start=36, margin_end=36)
        self.title = Gtk.Label(label=title, wrap=True)
        self.title.add_css_class("title-2")
        self.stage = Gtk.Label(wrap=True)
        self.bar = Gtk.ProgressBar(show_text=True)
        self.detail = Gtk.Label(wrap=True, ellipsize=0)
        self.detail.add_css_class("dim-label")
        self.eta = Gtk.Label()
        self.eta.add_css_class("dim-label")
        self.eta.add_css_class("numeric")
        self.cancel_btn = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        self.cancel_btn.add_css_class("pill")
        for w in (self.title, self.stage, self.bar, self.detail, self.eta, self.cancel_btn):
            self.append(w)
        self.progress: jobs.Progress | None = None
        self.cancel_btn.connect("clicked", lambda *_: self.cancel())
        self._pending = False

    def bind(self, progress: jobs.Progress) -> jobs.Progress:
        self.progress = progress
        self.cancel_btn.set_sensitive(True)
        self.cancel_btn.set_label("Cancel")
        progress.on_change = self._changed
        return progress

    def cancel(self):
        if self.progress:
            self.progress.cancel()
            self.cancel_btn.set_sensitive(False)
            self.cancel_btn.set_label("Cancelling…")

    def _changed(self, _p):
        if not self._pending:
            self._pending = True
            GLib.idle_add(self._refresh)

    def _refresh(self):
        self._pending = False
        p = self.progress
        if p is None:
            return False
        self.stage.set_label(p.stage_name or "")
        self.detail.set_label(p.detail or "")
        f = p.fraction
        if f <= 0.0 and p.stage_fraction == 0.0:
            self.bar.pulse()
            self.bar.set_text("")
        else:
            self.bar.set_fraction(f)
            self.bar.set_text(f"{f * 100:.0f} %")
        eta = p.eta_seconds
        self.eta.set_label(f"About {human_duration(eta)} left in this step" if eta and eta > 3 else "")
        return False
