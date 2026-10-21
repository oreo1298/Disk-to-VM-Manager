"""The page for one VM: state, actions, summary, checkpoints, settings, logs."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from rehome.ui.common import after_start, confirm, run_async, show_error  # noqa: E402
from rehome.ui.store import load_texture  # noqa: E402
from rehome.ui.window import STATE_LABEL  # noqa: E402
from rehome.util.units import human  # noqa: E402
from rehome.vm import library, runner  # noqa: E402


def _button(label, icon, tooltip=None, css=None):
    b = Gtk.Button(tooltip_text=tooltip or label)
    content = Adw.ButtonContent(label=label, icon_name=icon)
    b.set_child(content)
    if css:
        b.add_css_class(css)
    return b


class VmPage(Gtk.Box):
    def __init__(self, window, item):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.item = item
        self.vm_id = item.cfg.id

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        more = Gio.Menu()
        more.append("Rename…", "vm.rename")
        more.append("Clone…", "vm.clone")
        more.append("Export…", "vm.export")
        more.append("Delete…", "vm.delete")
        mb = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=more, tooltip_text="More actions")
        mb.update_property([Gtk.AccessibleProperty.LABEL], ["More actions"])
        header.pack_end(mb)
        tv.add_top_bar(header)
        self._actions()

        # Top: thumbnail + actions
        top = Gtk.Box(spacing=18, margin_top=12, margin_bottom=12, margin_start=18, margin_end=18)
        self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        placeholder = Gtk.Image(icon_name="computer-symbolic", pixel_size=48)
        placeholder.add_css_class("dim-label")
        self.thumb_stack = Gtk.Stack(width_request=192, height_request=120)
        self.thumb_stack.add_named(placeholder, "off")
        self.thumb_stack.add_named(self.picture, "picture")
        pic_btn = Gtk.Button(child=self.thumb_stack, tooltip_text="Open the console", valign=Gtk.Align.CENTER)
        pic_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Open the console"])
        pic_btn.add_css_class("flat")
        pic_btn.add_css_class("card")
        pic_btn.connect("clicked", lambda *_: self.open_console())
        top.append(pic_btn)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER,
                       hexpand=True)
        self.title = Gtk.Label(xalign=0, wrap=True)
        self.title.add_css_class("title-1")
        self.state_label = Gtk.Label(xalign=0, wrap=True)
        self.state_label.add_css_class("dim-label")
        info.append(self.title)
        info.append(self.state_label)
        self.actions_box = Gtk.Box(spacing=6, halign=Gtk.Align.START)
        self.btn_connect = _button("Connect", "video-display-symbolic", "Open the console",
                                   css="suggested-action")
        self.btn_connect.connect("clicked", lambda *_: self.open_console())
        self.btn_start = _button("Start", "media-playback-start-symbolic", css="suggested-action")
        self.btn_start.connect("clicked", lambda *_: self.start())
        self.btn_shutdown = _button("Shut Down", "system-shutdown-symbolic",
                                    "Ask the guest operating system to shut down")
        self.btn_shutdown.connect("clicked", lambda *_: self._op(runner.shutdown, "Asked the guest to shut down"))
        self.btn_pause = _button("Pause", "media-playback-pause-symbolic")
        self.btn_pause.connect("clicked", lambda *_: self._pause())
        power = Gio.Menu()
        power.append("Save State and Stop", "vm.save")
        power.append("Reset", "vm.reset")
        power.append("Turn Off", "vm.force-off")
        self.btn_power = Gtk.MenuButton(icon_name="pan-down-symbolic", menu_model=power,
                                        tooltip_text="More power options")
        self.btn_power.update_property([Gtk.AccessibleProperty.LABEL], ["More power options"])
        self.btn_checkpoint = _button("Checkpoint", "camera-photo-symbolic", "Take a checkpoint")
        self.btn_checkpoint.connect("clicked", lambda *_: self.checkpoints.take())
        for b in (self.btn_connect, self.btn_start, self.btn_shutdown, self.btn_pause, self.btn_power,
                  self.btn_checkpoint):
            self.actions_box.append(b)
        info.append(self.actions_box)
        top.append(info)

        from rehome.ui.checkpoints import CheckpointsPage
        from rehome.ui.logs_page import LogsPage
        from rehome.ui.settings_page import SettingsPage
        from rehome.ui.summary_page import SummaryPage
        self.summary = SummaryPage(self)
        self.checkpoints = CheckpointsPage(self)
        self.settings = SettingsPage(self)
        self.logs = LogsPage(self)
        self.stack.add_titled_with_icon(self.summary, "summary", "Summary", "view-list-bullet-symbolic")
        self.stack.add_titled_with_icon(self.checkpoints, "checkpoints", "Checkpoints", "camera-photo-symbolic")
        self.stack.add_titled_with_icon(self.settings, "settings", "Settings", "emblem-system-symbolic")
        self.stack.add_titled_with_icon(self.logs, "logs", "Logs", "text-x-generic-symbolic")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(top)
        body.append(Gtk.Separator())
        self.stack.set_vexpand(True)
        body.append(self.stack)
        tv.set_content(body)
        tv.set_vexpand(True)
        self.append(tv)
        self.update(item)

    # --- state ---------------------------------------------------------------------------

    @property
    def cfg(self):
        return self.item.cfg

    def update(self, item):
        self.item = item
        cfg = item.cfg
        st = item.status
        self.title.set_label(cfg.name)
        extra = ""
        if st == runner.STOPPED and cfg.import_info and not cfg.import_info.first_boot_ok:
            extra = " · imported, not started yet"
        self.state_label.set_label(f"{STATE_LABEL.get(st, st)} · {cfg.os.name or cfg.os.family} · "
                                   f"{cfg.machine.cpus} CPU, {human(cfg.machine.memory_mib * 2**20)}{extra}")
        active = st in (runner.RUNNING, runner.PAUSED, runner.STARTING)
        self.btn_connect.set_visible(active)
        self.btn_start.set_visible(not active)
        self.btn_start.get_child().set_label("Resume" if st == runner.SAVED else "Start")
        for b in (self.btn_shutdown, self.btn_pause, self.btn_power):
            b.set_visible(active)
        self.btn_pause.get_child().set_label("Resume" if st == runner.PAUSED else "Pause")
        self.btn_pause.get_child().set_icon_name("media-playback-start-symbolic" if st == runner.PAUSED
                                                 else "media-playback-pause-symbolic")
        tex = load_texture(item.thumbnail) if active or st == runner.SAVED else None
        self.picture.set_paintable(tex)
        self.thumb_stack.set_visible_child_name("picture" if tex is not None else "off")
        self.summary.update()
        self.checkpoints.update()
        self.settings.update_state(active)

    def reload(self):
        self.item.cfg = library.reload(self.item.cfg)
        self.window.store.refresh_now()
        self.update(self.item)

    # --- actions -------------------------------------------------------------------------

    def _actions(self):
        group = Gio.SimpleActionGroup()

        def act(name, fn):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: fn())
            group.add_action(a)
        act("save", lambda: self._op(runner.save, "State saved"))
        act("reset", lambda: confirm(
            self, "Reset the VM?", "Unsaved work in the VM is lost.", "Reset",
            lambda: self._op(runner.reset, "Reset"), destructive=True))
        act("force-off", lambda: confirm(
            self, "Turn the VM off?", "This is like pulling the power plug: unsaved work in the VM is lost.",
            "Turn Off", lambda: self._op(runner.force_off, "Turned off"), destructive=True))
        act("rename", self._rename)
        act("clone", self._clone)
        act("export", self._export)
        act("delete", self._delete)
        self.insert_action_group("vm", group)

    def _op(self, fn, message):
        def done(_):
            self.window.toast(message)
            self.window.store.refresh_now()
        run_async(lambda: fn(self.cfg), done, lambda e: show_error(self, e))

    def start(self):
        cfg = self.cfg
        self.btn_start.set_sensitive(False)

        def done(_):
            self.btn_start.set_sensitive(True)
            self.window.store.refresh_now()
            after_start(cfg, self.window.toast)
            if cfg.import_info and not cfg.import_info.first_boot_ok:
                self.window.toast("Watch the first boot in the console; Rehome will offer guest tools "
                                  "once it works", 6)
            self.open_console()

        def failed(e):
            self.btn_start.set_sensitive(True)
            show_error(self, e, "The VM could not start")
        run_async(lambda: runner.start(cfg), done, failed)

    def _pause(self):
        if self.item.status == runner.PAUSED:
            self._op(runner.resume, "Resumed")
        else:
            self._op(runner.pause, "Paused")

    def open_console(self):
        self.window.app.open_console(self.cfg)

    def _ask_text(self, heading, initial, action, on_ok):
        entry = Gtk.Entry(text=initial, activates_default=True)
        dlg = Adw.AlertDialog(heading=heading)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", action)
        dlg.set_default_response("ok")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.connect("response", lambda d, r: r == "ok" and on_ok(entry.get_text().strip()))
        dlg.present(self.get_root())

    def _rename(self):
        def do(name):
            if not name:
                return
            from rehome.vm import edit
            run_async(lambda: edit.mutate(self.cfg, lambda c: setattr(c, "name", name)),
                      lambda _: (self.reload(), self.window.toast(f"Renamed to {name}")),
                      lambda e: show_error(self, e))
        self._ask_text("Rename VM", self.cfg.name, "Rename", do)

    def _clone(self):
        def do(name):
            from rehome.vm import lifecycle
            self.window.toast(f"Cloning {self.cfg.name}…")
            run_async(lambda: lifecycle.clone(self.cfg, name or f"{self.cfg.name} (clone)"),
                      lambda new: (self.window.toast(f"Created {new.name}"), self.window.select(new.id)),
                      lambda e: show_error(self, e))
        self._ask_text("Clone VM", f"{self.cfg.name} (clone)", "Clone", do)

    def _export(self):
        dlg = Gtk.FileDialog(title="Export VM", initial_name=f"{library.slug(self.cfg.name)}.rehome")

        def chosen(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            from rehome.vm import lifecycle
            self.window.toast("Exporting… this can take a while")
            run_async(lambda: lifecycle.export(self.cfg, f.get_path()),
                      lambda p: self.window.toast(f"Exported to {p}"), lambda e: show_error(self, e))
        dlg.save(self.get_root(), None, chosen)

    def _delete(self):
        keep = Gtk.CheckButton(label="Keep the disk files (move them aside)")
        cfg = self.cfg

        def do():
            run_async(lambda: library.delete(cfg, keep_disks=keep.get_active()),
                      lambda kept: (self.window.toast(f"Deleted {cfg.name}" + (f"; disks kept in {kept}" if kept else ""), 6),
                                    self.window.store.refresh_now()),
                      lambda e: show_error(self, e))
        linked = any(d.source and d.source.mode == "linked" for d in cfg.disks)
        body = (f"\"{cfg.name}\" and its checkpoints will be deleted.")
        if linked:
            body += " The original disk or image it was imported from is not touched."
        confirm(self, f"Delete {cfg.name}?", body, "Delete", do, destructive=True, extra=keep)
