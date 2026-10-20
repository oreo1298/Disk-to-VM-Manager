"""Checkpoints tab: the snapshot tree with a "Now" marker (like Hyper-V)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from rehome.ui.common import confirm, run_async, show_error  # noqa: E402
from rehome.vm import snapshots  # noqa: E402


class CheckpointsPage(Gtk.Box):
    def __init__(self, page):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                         margin_bottom=12, margin_start=18, margin_end=18)
        self.page = page
        bar = Gtk.Box(spacing=6)
        take = Gtk.Button()
        take.set_child(Adw.ButtonContent(label="Take Checkpoint", icon_name="camera-photo-symbolic"))
        take.add_css_class("suggested-action")
        take.connect("clicked", lambda *_: self.take())
        bar.append(take)
        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.connect("clicked", lambda *_: self.revert())
        self.start_btn = Gtk.Button(label="Start From Here")
        self.start_btn.connect("clicked", lambda *_: self.start_from())
        self.delete_btn = Gtk.Button(label="Delete")
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.connect("clicked", lambda *_: self.delete())
        for b in (self.revert_btn, self.start_btn, self.delete_btn):
            bar.append(b)
        self.append(bar)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.connect("row-selected", lambda *_: self._sync_buttons())
        self.listbox.set_placeholder(Gtk.Label(label="No checkpoints yet. A checkpoint saves the "
                                               "VM's disks (and memory, if it is running) so you "
                                               "can go back to this moment later.", wrap=True,
                                               margin_top=24, margin_bottom=24, margin_start=24,
                                               margin_end=24))
        self.append(Gtk.ScrolledWindow(child=self.listbox, vexpand=True))
        self._sig = None

    def update(self):
        cfg = self.page.cfg
        sig = (tuple((s.id, s.name, s.parent) for s in cfg.snapshots), cfg.current_snapshot)
        if sig == self._sig:
            self._sync_buttons()
            return
        self._sig = sig
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)

        def add(node, depth):
            s = node.snapshot
            if s is None:
                r = Adw.ActionRow(title="▶ Now", subtitle="The VM's current state")
                r.snapshot_id = None
                r.set_activatable(False)
                r.set_selectable(False)
            else:
                kind = {"pre-patch": " · automatic, before fixes", "pre-tools": " · automatic"}.get(s.kind, "")
                sub = s.created.replace("T", " ")[:19] + (" · with memory" if s.has_state else "") + kind
                if s.description:
                    sub += f"\n{s.description}"
                r = Adw.ActionRow(title=GLib.markup_escape_text(s.name), subtitle=GLib.markup_escape_text(sub))
                r.add_prefix(Gtk.Image.new_from_icon_name("camera-photo-symbolic"))
                r.snapshot_id = s.id
            r.set_margin_start(depth * 24)
            self.listbox.append(r)
            for c in node.children:
                add(c, depth + 1)
        for root in snapshots.tree(cfg):
            add(root, 0)
        self._sync_buttons()

    def _selected(self):
        row = self.listbox.get_selected_row()
        sid = getattr(row, "snapshot_id", None) if row else None
        return self.page.cfg.snapshot(sid) if sid else None

    def _sync_buttons(self):
        s = self._selected()
        active = self.page.item.status in ("running", "paused")
        self.revert_btn.set_sensitive(s is not None and (s.has_state or not active))
        self.start_btn.set_sensitive(s is not None and s.has_state and not active)
        self.delete_btn.set_sensitive(s is not None)

    def _done(self, msg):
        self.page.reload()
        self.page.window.toast(msg)

    def take(self):
        cfg = self.page.cfg
        entry = Gtk.Entry(text=snapshots.default_name(cfg), activates_default=True)
        dlg = Adw.AlertDialog(heading="Take Checkpoint",
                              body="A running VM's memory is included, so you can return to this exact "
                                   "moment.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Take Checkpoint")
        dlg.set_default_response("ok")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r == "ok":
                self.page.window.toast("Taking a checkpoint…")
                run_async(lambda: snapshots.create(cfg, entry.get_text().strip()),
                          lambda s: self._done(f"Checkpoint \"{s.name}\" taken"),
                          lambda e: show_error(self, e))
        dlg.connect("response", resp)
        dlg.present(self.get_root())

    def revert(self):
        s = self._selected()
        if not s:
            return
        cfg = self.page.cfg
        confirm(self, f"Revert to \"{s.name}\"?",
                "The VM's current state is lost unless you take a checkpoint of it first.", "Revert",
                lambda: run_async(lambda: snapshots.revert(cfg, s.id),
                                  lambda _: self._done(f"Reverted to \"{s.name}\""),
                                  lambda e: show_error(self, e)), destructive=True)

    def start_from(self):
        s = self._selected()
        if not s:
            return
        cfg = self.page.cfg
        run_async(lambda: snapshots.start_from(cfg, s.id),
                  lambda _: (self._done(f"Started from \"{s.name}\""), self.page.open_console()),
                  lambda e: show_error(self, e))

    def delete(self):
        s = self._selected()
        if not s:
            return
        cfg = self.page.cfg
        confirm(self, f"Delete \"{s.name}\"?",
                "You can no longer return to this checkpoint. The VM's current state is not affected.",
                "Delete", lambda: run_async(lambda: snapshots.delete(cfg, s.id),
                                            lambda _: self._done("Checkpoint deleted"),
                                            lambda e: show_error(self, e)), destructive=True)
