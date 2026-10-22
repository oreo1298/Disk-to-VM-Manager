"""Import wizard: disk or image -> identified, patched VM."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from rehome import importer, jobs  # noqa: E402
from rehome.errors import Cancelled  # noqa: E402
from rehome.patch import profiles  # noqa: E402
from rehome.ui.common import combo_row, combo_value, run_async, show_error  # noqa: E402
from rehome.ui.progress import ProgressPanel  # noqa: E402
from rehome.util.units import human  # noqa: E402

MODE_TEXT = {
    "linked": ("Linked (fast)", "The VM reads the original disk and keeps its own changes in a small "
               "overlay file. The original must stay connected while the VM is used."),
    "copy": ("Copy", "Rehome copies the disk into a standalone image. It takes longer and needs space "
             "for the used part of the disk, but the original can be removed afterwards."),
}


def _page(title: str, child: Gtk.Widget, button: Gtk.Widget | None = None) -> Adw.NavigationPage:
    tv = Adw.ToolbarView()
    hb = Adw.HeaderBar()
    if button is not None:
        hb.pack_end(button)
    tv.add_top_bar(hb)
    tv.set_content(child)
    return Adw.NavigationPage(title=title, child=tv)


class ImportWizard(Adw.Window):
    def __init__(self, parent):
        super().__init__(transient_for=parent, modal=True, title="Import a Disk",
                         default_width=760, default_height=720)
        self.main = parent
        self.nav = Adw.NavigationView()
        self.set_content(self.nav)
        self.images: list[str] = []
        self.disk_checks: dict[str, Gtk.CheckButton] = {}
        self.analysis: importer.Analysis | None = None
        self.keys: list[str] = []
        self.nav.add(self._choose_page())

    # --- page 1: choose ------------------------------------------------------------------

    def _choose_page(self):
        pref = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup(description="Choose the disk (or disk image) the system is "
                                                 "installed on. Rehome only reads it: nothing on it "
                                                 "will ever be changed.")
        pref.add(intro)
        self.disks_group = Adw.PreferencesGroup(title="Disks in this computer")
        spinner_row = Adw.ActionRow(title="Looking for disks…")
        spinner_row.add_prefix(Gtk.Spinner(spinning=True))
        self.disks_group.add(spinner_row)
        self._spinner_row = spinner_row
        pref.add(self.disks_group)
        self.images_group = Adw.PreferencesGroup(title="Disk images",
                                                 description="Raw (.img, .dd), E01, VHD/VHDX, VMDK, VDI or qcow2.")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add image files", valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self._add_images())
        self.images_group.set_header_suffix(add)
        pref.add(self.images_group)
        mode = Adw.PreferencesGroup(title="How to use the disk")
        self.mode_rows = {}
        first = None
        for key in ("linked", "copy"):
            t, d = MODE_TEXT[key]
            r = Adw.ActionRow(title=t, subtitle=d)
            check = Gtk.CheckButton(group=first)
            if first is None:
                first = check
                check.set_active(True)
            r.add_prefix(check)
            r.set_activatable_widget(check)
            mode.add(r)
            self.mode_rows[key] = check
        from rehome.ui.disk_location import DiskLocationRow
        self.copy_loc = DiskLocationRow(title="Store the copy in")
        self.copy_loc.set_visible(False)
        self.mode_rows["copy"].connect("toggled", lambda b: self.copy_loc.set_visible(b.get_active()))
        mode.add(self.copy_loc)
        pref.add(mode)
        opts = Adw.PreferencesGroup(title="Safety")
        self.smart_row = Adw.SwitchRow(title="Check disk health first",
                                       subtitle="Reads the disk's SMART status; failing disks should be "
                                                "imaged with ddrescue instead.", active=True)
        self.verify_row = Adw.SwitchRow(title="Verify integrity",
                                        subtitle="Record a SHA-256 checksum of the source before and after "
                                                 "the import (reads the whole disk twice).", active=False)
        opts.add(self.smart_row)
        opts.add(self.verify_row)
        pref.add(opts)
        self.next_btn = Gtk.Button(label="Next", sensitive=False)
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.connect("clicked", lambda *_: self._analyze())
        from rehome.disk import sources
        run_async(lambda: sources.list_disks(), self._show_disks,
                  lambda e: (self.disks_group.remove(self._spinner_row),
                             self.disks_group.add(Adw.ActionRow(title="Disks could not be listed",
                                                                subtitle=str(e)))))
        return _page("Choose a Source", pref, self.next_btn)

    def _show_disks(self, disks):
        self.disks_group.remove(self._spinner_row)
        if not disks:
            self.disks_group.add(Adw.ActionRow(title="No other disks were found"))
        for d in disks:
            r = Adw.ActionRow(title=GLib.markup_escape_text(d.label or d.path),
                              subtitle=GLib.markup_escape_text(
                                  f"{d.path} · {human(d.size)} · {d.transport.upper() or 'disk'}"
                                  + ("" if d.safe else " — " + " ".join(d.problems))))
            r.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-usb-symbolic" if d.transport == "usb"
                                                      else "drive-harddisk-symbolic"))
            check = Gtk.CheckButton(sensitive=d.safe)
            check.connect("toggled", lambda *_: self._update_next())
            r.add_suffix(check)
            r.set_activatable_widget(check)
            r.set_sensitive(d.safe)
            self.disks_group.add(r)
            self.disk_checks[d.path] = check

    def _add_images(self):
        dlg = Gtk.FileDialog(title="Choose disk images")

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            for i in range(files.get_n_items()):
                path = files.get_item(i).get_path()
                if path:
                    self.add_image(path)
        dlg.open_multiple(self, None, done)

    def add_image(self, path: str):
        if path in self.images:
            return
        self.images.append(path)
        r = Adw.ActionRow(title=GLib.markup_escape_text(os.path.basename(path)),
                          subtitle=GLib.markup_escape_text(path))
        r.add_prefix(Gtk.Image.new_from_icon_name("media-floppy-symbolic"))
        check = Gtk.CheckButton(active=True)
        check.connect("toggled", lambda *_: self._update_next())
        r.add_suffix(check)
        r.set_activatable_widget(check)
        self.images_group.add(r)
        self.disk_checks[path] = check
        self._update_next()

    def _selected_sources(self) -> list[str]:
        return [p for p, c in self.disk_checks.items() if c.get_active()]

    def _update_next(self):
        self.next_btn.set_sensitive(bool(self._selected_sources()))

    def _mode(self) -> str:
        return "copy" if self.mode_rows["copy"].get_active() else "linked"

    # --- page 2: analysing -----------------------------------------------------------------

    def _analyze(self):
        srcs = self._selected_sources()
        panel = ProgressPanel("Examining the disk")
        prog = panel.bind(jobs.Progress(title="Examining"))
        page = _page("Examining", panel)
        self.nav.push(page)

        def done(a):
            self.analysis = a
            self._show_summary()

        def failed(e):
            self.nav.pop()
            if not isinstance(e, Cancelled):
                show_error(self, e, "The disk could not be examined")
        run_async(lambda: importer.analyze(srcs, keys=self.keys, smart_check=self.smart_row.get_active(),
                                           progress=prog), done, failed)

    # --- page 3: summary ---------------------------------------------------------------------

    def _show_summary(self):
        a = self.analysis
        pref = Adw.PreferencesPage()
        import_btn = Gtk.Button(label="Import")
        import_btn.add_css_class("suggested-action")
        import_btn.connect("clicked", lambda *_: self._run())
        if "failing-disk" in a.blocker_codes:
            g = Adw.PreferencesGroup(title="This disk is failing")
            for b in a.blockers:
                lbl = Gtk.Label(label=b, wrap=True, xalign=0, selectable=True)
                lbl.add_css_class("monospace")
                g.add(lbl)
            pref.add(g)
            import_btn.set_sensitive(False)
            self.nav.push(_page("Disk Health", pref, import_btn))
            return
        o = a.os
        g = Adw.PreferencesGroup(title="What Rehome found")
        if a.report.oses:
            labels = [x.label for x in a.report.oses]
            self.os_row = combo_row("System to boot", labels, [str(i) for i in range(len(labels))],
                                    str(a.os_index))
            self.os_row.connect("notify::selected", lambda *_: self._os_changed())
            g.add(self.os_row)
        else:
            self.os_row = None
            g.add(Adw.ActionRow(title="No operating system was identified",
                                subtitle="Choose the system's family below to import it anyway."))
        if o:
            details = [("Version", " ".join(x for x in (o.version, f"build {o.build}" if o.build else "") if x)),
                       ("Edition", o.edition or o.details.get("edition_id", "")),
                       ("Architecture", o.arch), ("Boots with", f"{o.boot_mode.upper()} {o.bootloader}".strip()),
                       ("Original disk controller", o.original_bus),
                       ("Kernel", o.kernel), ("Other hypervisors' tools", ", ".join(o.hypervisor_tools)),
                       ("Encryption", ", ".join(o.encryption))]
            for k, v in details:
                if v:
                    r = Adw.ActionRow(title=k, subtitle=GLib.markup_escape_text(str(v)))
                    r.set_subtitle_selectable(True)
                    g.add(r)
        pref.add(g)
        if a.report.encrypted_locked:
            kg = Adw.PreferencesGroup(title="Encrypted volume",
                                      description="Enter the BitLocker recovery key (48 digits) or the "
                                                  "LUKS passphrase so Rehome can identify and fix the system.")
            self.key_row = Adw.PasswordEntryRow(title="Recovery key or passphrase")
            unlock = Gtk.Button(label="Unlock", valign=Gtk.Align.CENTER)
            unlock.connect("clicked", lambda *_: self._unlock())
            self.key_row.add_suffix(unlock)
            kg.add(self.key_row)
            pref.add(kg)
        self.hib_row = None
        if o and o.hibernated:
            hg = Adw.PreferencesGroup(title="Windows is hibernated",
                                      description="Windows was hibernated or shut down with Fast Startup. "
                                                  "Resuming that session on new hardware does not work.")
            self.hib_row = combo_row("In the VM", ["Discard the hibernated session (recommended)",
                                                   "Keep it (the VM will probably not start)"],
                                     ["discard", "keep"], "discard",
                                     "Only the VM's copy is changed; the original disk is untouched. "
                                     "Unsaved work in the hibernated session is lost.")
            hg.add(self.hib_row)
            pref.add(hg)
        if a.notes:
            ng = Adw.PreferencesGroup(title="Good to know")
            for n in dict.fromkeys(a.notes):
                r = Adw.ActionRow(title=GLib.markup_escape_text(n))
                r.set_title_lines(0)
                r.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
                ng.add(r)
            pref.add(ng)
        vg = Adw.PreferencesGroup(title="Virtual machine",
                                  description="Rehome chose these settings for the system it found. "
                                              "Change them only if you know better.")
        self.name_row = Adw.EntryRow(title="Name", text=a.preview.name if a.preview else "")
        vg.add(self.name_row)
        all_profiles = [p for p in profiles.load_all() if p.supported]
        self.profile_row = combo_row("System type", [p.name for p in all_profiles],
                                     [p.id for p in all_profiles], a.profile.id if a.profile else "generic")
        vg.add(self.profile_row)
        m = a.preview.machine if a.preview else None
        self.fw_row = combo_row("Firmware", ["BIOS", "UEFI", "UEFI with Secure Boot"],
                                ["bios", "uefi", "uefi-secure"], m.firmware if m else "bios",
                                "Must match how the system was installed")
        self.bus_row = combo_row("Disk controller", ["Automatic", "SATA", "IDE", "NVMe", "VirtIO", "SCSI"],
                                 ["", "sata", "ide", "nvme", "virtio", "scsi"], "",
                                 f"Automatic: {a.preview.disks[0].bus.upper()}" if a.preview and a.preview.disks else "")
        from rehome.host import info as host_info
        host = host_info.gather()
        self.mem_row = Adw.SpinRow.new_with_range(16, max(256, host.memory.max_vm_mib), 64)
        self.mem_row.set_title("Memory (MiB)")
        self.mem_row.set_value(m.memory_mib if m else 2048)
        self.cpu_row = Adw.SpinRow.new_with_range(1, host.threads, 1)
        self.cpu_row.set_title("Virtual CPUs")
        self.cpu_row.set_value(m.cpus if m else 2)
        self.patch_row = Adw.SwitchRow(title="Apply automatic fixes",
                                       subtitle="Make the system boot on virtual hardware (only the VM's "
                                                "copy is changed; a checkpoint allows undoing it).",
                                       active=True)
        for r in (self.fw_row, self.bus_row, self.mem_row, self.cpu_row, self.patch_row):
            vg.add(r)
        if a.profile and a.profile.patches:
            fixes = Adw.ExpanderRow(title="Planned fixes", subtitle=f"{len(a.profile.patches)} steps")
            from rehome.patch import registry
            for pid in a.profile.patches:
                try:
                    fixes.add_row(Adw.ActionRow(title=registry.get(pid).title))
                except KeyError:
                    pass
            vg.add(fixes)
        pref.add(vg)
        blocked = [b for c, b in zip(a.blocker_codes, a.blockers) if c == "unsupported"]
        if blocked:
            bg = Adw.PreferencesGroup(title="Cannot import")
            for b in blocked:
                bg.add(Adw.ActionRow(title=GLib.markup_escape_text(b)))
            pref.add(bg)
            import_btn.set_sensitive(False)
        self.nav.push(_page("Review", pref, import_btn))

    def _os_changed(self):
        self.analysis.os_index = int(combo_value(self.os_row))
        importer.refresh(self.analysis, {})
        self.nav.pop()
        self._show_summary()

    def _unlock(self):
        key = self.key_row.get_text().strip()
        if key:
            self.keys.append(key)
            self.nav.pop()
            self.nav.pop()
            self._analyze()

    # --- page 4: import ----------------------------------------------------------------------

    def _overrides(self) -> dict:
        a = self.analysis
        ov = {}
        prof = combo_value(self.profile_row)
        if not a.profile or prof != a.profile.id:
            ov["profile"] = prof
        fw = combo_value(self.fw_row)
        if a.preview and fw != a.preview.machine.firmware:
            ov["firmware"] = fw
        bus = combo_value(self.bus_row)
        if bus:
            ov["disk_bus"] = bus
        ov["memory_mib"] = int(self.mem_row.get_value())
        ov["cpus"] = int(self.cpu_row.get_value())
        return ov

    def _run(self):
        a = self.analysis
        opts = importer.ImportOptions(
            name=self.name_row.get_text().strip(), mode=self._mode(),
            verify_hash=self.verify_row.get_active(), keys=self.keys, os_index=a.os_index,
            overrides=self._overrides(), patch=self.patch_row.get_active(),
            disk_dir=self.copy_loc.folder if self._mode() == "copy" else None)
        if self.hib_row is not None:
            opts.discard_hibernation = combo_value(self.hib_row) == "discard"
        panel = ProgressPanel("Importing")
        prog = panel.bind(jobs.Progress(title="Importing"))
        self.nav.push(_page("Importing", panel))

        def failed(e):
            self.nav.pop()
            if not isinstance(e, Cancelled):
                show_error(self, e, "The import failed")
        run_async(lambda: importer.run(a, opts, progress=prog), self._done, failed)

    # --- page 5: done ---------------------------------------------------------------------------

    def _done(self, cfg):
        page = Adw.StatusPage(icon_name="emblem-ok-symbolic", title=f"{cfg.name} is ready",
                              description="Start it and watch the first boot in the console. If it "
                                          "works, confirm it on the VM's page to continue with guest "
                                          "tools.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        start = Gtk.Button(label="Start and Open Console")
        start.add_css_class("pill")
        start.add_css_class("suggested-action")
        close = Gtk.Button(label="Close")
        close.add_css_class("pill")
        box.append(start)
        box.append(close)
        summary = cfg.dir / "logs" / "patch-summary.txt"
        if summary.exists():
            exp = Gtk.Expander(label="What Rehome changed in the VM's copy")
            lbl = Gtk.Label(label=summary.read_text(), xalign=0, wrap=True, selectable=True)
            lbl.add_css_class("monospace")
            exp.set_child(lbl)
            box.append(exp)
        page.set_child(box)
        nav_page = _page("Done", page)
        nav_page.set_can_pop(False)
        self.nav.push(nav_page)
        self.main.store.refresh_now()
        self.main.select(cfg.id)

        def start_vm(*_):
            from rehome.vm import runner
            start.set_sensitive(False)
            run_async(lambda: runner.start(cfg),
                      lambda _: (self.main.app.open_console(cfg), self.close()),
                      lambda e: (start.set_sensitive(True), show_error(self, e, "The VM could not start")))
        start.connect("clicked", start_vm)
        close.connect("clicked", lambda *_: self.close())
