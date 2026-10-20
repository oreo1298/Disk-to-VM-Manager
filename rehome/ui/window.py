"""Main window: VM list on the left, the selected VM on the right."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from rehome import APP_NAME  # noqa: E402
from rehome.ui.store import VmStore, load_texture  # noqa: E402
from rehome.vm import runner  # noqa: E402

STATE_LABEL = {runner.RUNNING: "Running", runner.PAUSED: "Paused", runner.STOPPED: "Off",
               runner.SAVED: "Saved", runner.STARTING: "Starting", runner.CRASHED: "Crashed"}
STATE_ICON = {runner.RUNNING: "media-playback-start-symbolic", runner.PAUSED: "media-playback-pause-symbolic",
              runner.STOPPED: "system-shutdown-symbolic", runner.SAVED: "document-save-symbolic",
              runner.STARTING: "content-loading-symbolic", runner.CRASHED: "dialog-error-symbolic"}


class VmRow(Gtk.ListBoxRow):
    def __init__(self, vm_id: str):
        super().__init__()
        self.vm_id = vm_id
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        self.thumb = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.COVER,
                                 width_request=72, height_request=48)
        self.thumb.add_css_class("card")
        frame = Gtk.Frame(child=self.thumb)
        frame.set_size_request(72, 48)
        box.append(frame)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.name = Gtk.Label(xalign=0, ellipsize=3)
        self.name.add_css_class("heading")
        self.sub = Gtk.Label(xalign=0, ellipsize=3)
        self.sub.add_css_class("dim-label")
        self.sub.add_css_class("caption")
        text.append(self.name)
        text.append(self.sub)
        box.append(text)
        self.icon = Gtk.Image()
        box.append(self.icon)
        self.set_child(box)

    def update(self, item):
        cfg = item.cfg
        self.name.set_label(cfg.name)
        state = STATE_LABEL.get(item.status, item.status)
        self.sub.set_label(f"{state} · {cfg.os.name or cfg.os.family}")
        self.icon.set_from_icon_name(STATE_ICON.get(item.status, "computer-symbolic"))
        self.icon.set_tooltip_text(state)
        tex = load_texture(item.thumbnail)
        self.thumb.set_paintable(tex)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{cfg.name}, {state}"])


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=1180, default_height=780)
        self.app = app
        self.store = VmStore()
        self.rows: dict[str, VmRow] = {}
        self.pages: dict[str, Gtk.Widget] = {}

        self.toast_overlay = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=260, max_sidebar_width=340)

        # Sidebar
        side = Adw.ToolbarView()
        sh = Adw.HeaderBar()
        sh.set_title_widget(Adw.WindowTitle(title=APP_NAME))
        new_btn = Gtk.MenuButton(icon_name="list-add-symbolic", tooltip_text="New virtual machine")
        new_btn.update_property([Gtk.AccessibleProperty.LABEL], ["New virtual machine"])
        add_menu = Gio.Menu()
        add_menu.append("New Virtual Machine…", "app.new-vm")
        add_menu.append("Quick Create…", "app.quick-create")
        add_menu.append("Import Disk…", "app.import")
        add_menu.append("Import Exported VM…", "app.import-bundle")
        new_btn.set_menu_model(add_menu)
        sh.pack_start(new_btn)
        menu = Gio.Menu()
        menu.append("Host Check", "app.doctor")
        menu.append("Preferences", "app.preferences")
        menu.append("Keyboard Shortcuts", "win.show-help-overlay")
        menu.append(f"About {APP_NAME}", "app.about")
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu")
        mb.update_property([Gtk.AccessibleProperty.LABEL], ["Main menu"])
        sh.pack_end(mb)
        side.add_top_bar(sh)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._row_selected)
        self.listbox.set_placeholder(self._placeholder())
        side.set_content(Gtk.ScrolledWindow(child=self.listbox, vexpand=True))
        self.split.set_sidebar(Adw.NavigationPage(title=APP_NAME, child=side))

        # Content
        self.empty = self._empty_page()
        self.content_page = Adw.NavigationPage(title="", child=self.empty)
        self.split.set_content(self.content_page)
        self.toast_overlay.set_child(self.split)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)
        self.set_content(self.toast_overlay)

        self.store.connect("changed", self._store_changed)
        self.store.connect("item-updated", self._item_updated)
        self.connect("close-request", self._on_close)

    def _placeholder(self):
        lbl = Gtk.Label(label="No virtual machines yet", margin_top=24, margin_bottom=24)
        lbl.add_css_class("dim-label")
        return lbl

    def _empty_page(self):
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar(show_title=False))
        page = Adw.StatusPage(icon_name="io.github.oreo1298.Rehome", title="Welcome to Rehome",
                              description="Give an installed system a new home as a virtual machine, "
                                          "or create a new one.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        imp = Gtk.Button(label="Import a Disk or Image…")
        imp.add_css_class("pill")
        imp.add_css_class("suggested-action")
        imp.set_action_name("app.import")
        new = Gtk.Button(label="New Virtual Machine…")
        new.add_css_class("pill")
        new.set_action_name("app.new-vm")
        quick = Gtk.Button(label="Quick Create…")
        quick.add_css_class("pill")
        quick.set_action_name("app.quick-create")
        for b in (imp, new, quick):
            box.append(b)
        page.set_child(box)
        tv.set_content(page)
        return tv

    # --- list maintenance ------------------------------------------------------------

    def _store_changed(self, store):
        selected = self.selected_id()
        for vm_id in list(self.rows):
            if vm_id not in store.items:
                self.listbox.remove(self.rows.pop(vm_id))
                self.pages.pop(vm_id, None)
        for vm_id in store.order:
            if vm_id not in self.rows:
                row = VmRow(vm_id)
                self.rows[vm_id] = row
                self.listbox.append(row)
                row.update(store.items[vm_id])
        self.listbox.set_sort_func(lambda a, b: (store.items[a.vm_id].cfg.name.lower() >
                                                 store.items[b.vm_id].cfg.name.lower()) -
                                   (store.items[a.vm_id].cfg.name.lower() <
                                    store.items[b.vm_id].cfg.name.lower()))
        if selected and selected in self.rows:
            self.listbox.select_row(self.rows[selected])
        elif selected and selected not in self.rows:
            self.content_page.set_child(self.empty)
            self.content_page.set_title("")
        pending = getattr(self, "_select_after_refresh", None)
        if pending and pending in self.rows:
            self._select_after_refresh = None
            self.select(pending)

    def _item_updated(self, store, vm_id):
        row = self.rows.get(vm_id)
        if row:
            row.update(store.items[vm_id])
            self.listbox.invalidate_sort()
        page = self.pages.get(vm_id)
        if page is not None:
            page.update(store.items[vm_id])

    def selected_id(self) -> str | None:
        row = self.listbox.get_selected_row()
        return row.vm_id if row else None

    def select(self, vm_id: str):
        if vm_id in self.rows:
            self.listbox.select_row(self.rows[vm_id])
        else:
            self._select_after_refresh = vm_id
            self.store.refresh_now()

    def _row_selected(self, lb, row):
        if row is None:
            return
        from rehome.ui.vm_page import VmPage
        item = self.store.get(row.vm_id)
        if item is None:
            return
        page = self.pages.get(row.vm_id)
        if page is None:
            page = VmPage(self, item)
            self.pages[row.vm_id] = page
        self.content_page.set_child(page)
        self.content_page.set_title(item.cfg.name)
        self.split.set_show_content(True)

    def toast(self, text: str, timeout: int = 3):
        self.toast_overlay.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout))

    def _on_close(self, *_):
        self.store.stop()
        return False
