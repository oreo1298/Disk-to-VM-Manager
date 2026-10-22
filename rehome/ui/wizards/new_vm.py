"""New VM wizard (ISO, Quick Create catalog, or empty) with optional unattended install."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from rehome import jobs  # noqa: E402
from rehome.errors import Cancelled  # noqa: E402
from rehome.host import info as host_info  # noqa: E402
from rehome.ui.common import after_start, combo_row, combo_value, run_async, show_error  # noqa: E402
from rehome.ui.progress import ProgressPanel  # noqa: E402
from rehome.ui.wizards.import_vm import _page  # noqa: E402
from rehome.util.units import human, parse_size  # noqa: E402


class NewVmWizard(Adw.Window):
    def __init__(self, parent, start_with_catalog: bool = False):
        super().__init__(transient_for=parent, modal=True, title="New Virtual Machine",
                         default_width=720, default_height=700)
        self.main = parent
        self.nav = Adw.NavigationView()
        self.set_content(self.nav)
        self.iso = ""
        self.catalog_entry = None
        self.detected = None       # rehome.media.osinfo.MediaInfo
        self.os_choice = ""        # "" = as detected; else a profile id chosen on the hardware page
        self.nav.add(self._source_page(start_with_catalog))

    # --- page 1 -----------------------------------------------------------------------------

    def _source_page(self, catalog_first):
        pref = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Installation media")
        self.src_iso = Gtk.CheckButton(active=not catalog_first)
        self.src_catalog = Gtk.CheckButton(group=self.src_iso, active=catalog_first)
        self.src_none = Gtk.CheckButton(group=self.src_iso)
        r = Adw.ActionRow(title="Installer disc image (ISO)",
                          subtitle="Rehome recognises the operating system and picks suitable settings.")
        r.add_prefix(self.src_iso)
        self.iso_btn = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        self.iso_btn.connect("clicked", lambda *_: self._choose_iso())
        r.add_suffix(self.iso_btn)
        r.set_activatable_widget(self.src_iso)
        g.add(r)
        self.iso_row = Adw.ActionRow(title="", subtitle="")
        self.iso_row.add_prefix(Gtk.Image.new_from_icon_name("media-optical-symbolic"))
        g.add(self.iso_row)
        r = Adw.ActionRow(title="Quick Create",
                          subtitle="Download a free operating system from its official website.")
        r.add_prefix(self.src_catalog)
        r.set_activatable_widget(self.src_catalog)
        g.add(r)
        r = Adw.ActionRow(title="No installation media",
                          subtitle="Create an empty VM; add a disc or network boot later.")
        r.add_prefix(self.src_none)
        r.set_activatable_widget(self.src_none)
        g.add(r)
        pref.add(g)
        self.catalog_group = Adw.PreferencesGroup(title="Free operating systems")
        pref.add(self.catalog_group)
        self._fill_catalog()
        for b in (self.src_iso, self.src_catalog, self.src_none):
            b.connect("toggled", lambda *_: self._sync())
        self.next_btn = Gtk.Button(label="Next")
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.connect("clicked", lambda *_: self._to_hardware())
        self._sync()
        return _page("New Virtual Machine", pref, self.next_btn)

    def _fill_catalog(self):
        try:
            from rehome.media import catalog
            entries = catalog.load()
        except Exception as e:  # noqa: BLE001
            self.catalog_group.add(Adw.ActionRow(title="The catalog could not be loaded", subtitle=str(e)))
            return
        first = None
        self.catalog_checks = {}
        for e in entries:
            sub = e.description + (" — opens Microsoft's download page" if e.external else "")
            r = Adw.ActionRow(title=GLib.markup_escape_text(e.name), subtitle=GLib.markup_escape_text(sub))
            c = Gtk.CheckButton(group=first)
            first = first or c
            c.connect("toggled", self._catalog_toggled, e)
            r.add_prefix(c)
            r.set_activatable_widget(c)
            if e.external:
                r.add_suffix(Gtk.Image.new_from_icon_name("web-browser-symbolic"))
            self.catalog_group.add(r)
            self.catalog_checks[e.id] = c

    def _catalog_toggled(self, button, entry):
        if button.get_active():
            self.catalog_entry = entry
            self._sync()

    def _sync(self):
        self.catalog_group.set_visible(self.src_catalog.get_active())
        self.iso_btn.set_sensitive(self.src_iso.get_active())
        self.iso_row.set_visible(bool(self.iso))
        ok = (self.src_iso.get_active() and self.iso) or self.src_none.get_active() or \
            (self.src_catalog.get_active() and self.catalog_entry is not None)
        self.next_btn.set_sensitive(bool(ok))
        entry = self.catalog_entry if self.src_catalog.get_active() else None
        self.next_btn.set_label("Open Download Page" if entry is not None and entry.external else "Next")

    def _choose_iso(self):
        f = Gtk.FileFilter()
        f.set_name("Disc images")
        for p in ("*.iso", "*.ISO", "*.img"):
            f.add_pattern(p)
        dlg = Gtk.FileDialog(title="Choose an installer disc image", default_filter=f)

        def done(d, res):
            try:
                self.iso = d.open_finish(res).get_path()
            except GLib.Error:
                return
            self.os_choice = ""
            self.iso_row.set_title(GLib.markup_escape_text(os.path.basename(self.iso)))
            self.iso_row.set_subtitle("Recognising…")
            from rehome.media import osinfo

            def detected(mi):
                self.detected = mi
                self.iso_row.set_subtitle(GLib.markup_escape_text(
                    f"Detected: {mi.label}" if mi.os else "Not recognised — choose the system type next"))
            run_async(lambda: osinfo.identify_media(self.iso), detected,
                      lambda e: self.iso_row.set_subtitle("Not recognised"))
            self._sync()
        dlg.open(self, None, done)

    # --- page 2 -----------------------------------------------------------------------------

    def _spec(self):
        from rehome.vm.create import NewVMSpec
        spec = NewVMSpec(iso=self.iso if self.src_iso.get_active() else "")
        if self.src_catalog.get_active() and self.catalog_entry:
            ids = self.catalog_entry.osinfo
            spec.os = ids[0] if ids else ""
            spec.name = self.catalog_entry.name
        elif self.os_choice:
            spec.os = self.os_choice
            spec.name = getattr(self, "_kept_name", "")
        return spec

    def _os_row(self, plan):
        """Operating system: as detected, or a system type picked by the user."""
        from rehome.patch import profiles
        order = ["windows-11", "windows-modern", "windows-vista-7", "windows-xp", "windows-2000",
                 "windows-nt4", "win98", "win95", "dos", "linux", "linux-legacy", "bsd", "reactos",
                 "generic"]
        known = {p.id: p for p in profiles.load_all() if p.supported}
        ids = [i for i in order if i in known] + sorted(set(known) - set(order))
        auto = (f"As detected: {plan.os_label}" if self.iso and self.detected is not None
                and self.detected.os else "Not recognised: choose one")
        labels, values = [auto], [""]
        if self.src_catalog.get_active():
            labels, values = [f"{plan.os_label}"], [""]
        labels += [known[i].name for i in ids]
        values += ids
        row = combo_row("Operating system", labels, values, self.os_choice,
                        subtitle="Decides the virtual hardware (drivers the system has built in).")
        row.set_sensitive(not self.src_catalog.get_active())

        def changed(r, _pspec):
            value = combo_value(r)
            if value != self.os_choice:
                self.os_choice = value
                self._kept_name = self.name_row.get_text().strip()
                self.nav.pop()
                self._to_hardware()
        row.connect("notify::selected", changed)
        return row

    def _to_hardware(self):
        if self.src_catalog.get_active():
            if self.catalog_entry is None:
                return
            if self.catalog_entry.external:
                Gtk.UriLauncher.new(self.catalog_entry.url).launch(self, None, None)
                self.main.toast("Download the ISO from Microsoft, then choose it here", 6)
                return
        from rehome.vm import create
        try:
            plan = create.plan(self._spec())
        except Exception as e:  # noqa: BLE001
            show_error(self, e)
            return
        self.plan = plan
        cfg = plan.config
        host = host_info.gather()
        pref = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title=plan.os_label, description=f"Settings recommended for "
                                                                   f"{plan.os_label}.")
        g.add(self._os_row(plan))
        self.name_row = Adw.EntryRow(title="Name", text=cfg.name)
        self.cpus = Adw.SpinRow.new_with_range(1, host.threads, 1)
        self.cpus.set_title("Virtual CPUs")
        self.cpus.set_value(cfg.machine.cpus)
        self.mem = Adw.SpinRow.new_with_range(64, max(256, host.memory.max_vm_mib), 256)
        self.mem.set_title("Memory (MiB)")
        self.mem.set_value(cfg.machine.memory_mib)
        self.disk = Adw.EntryRow(title="Disk size", text=human(plan.disk_size).replace(".0 ", " ").replace("iB", ""))
        from rehome.ui.disk_location import DiskLocationRow
        self.disk_loc = DiskLocationRow(need=plan.disk_size)

        def size_changed(*_):
            try:
                self.disk_loc.set_need(parse_size(self.disk.get_text(), "g"))
            except ValueError:
                pass
        self.disk.connect("changed", size_changed)
        self.fw = combo_row("Firmware", ["BIOS", "UEFI", "UEFI with Secure Boot"],
                            ["bios", "uefi", "uefi-secure"], cfg.machine.firmware)
        self.tpm = combo_row("TPM", ["None", "TPM 2.0"], ["none", "2.0"], cfg.machine.tpm)
        self.net = combo_row("Network", ["NAT (share this computer's connection)", "Not connected"],
                             ["nat", "none"], "nat")
        for r in (self.name_row, self.cpus, self.mem, self.disk, self.disk_loc, self.fw, self.tpm, self.net):
            g.add(r)
        pref.add(g)
        for n in plan.notes:
            ng = Adw.PreferencesGroup()
            row = Adw.ActionRow(title=GLib.markup_escape_text(n))
            row.set_title_lines(0)
            row.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
            ng.add(row)
            pref.add(ng)
        self.unattended_group = Adw.PreferencesGroup(title="Automatic installation")
        self.ua_switch = Adw.SwitchRow(title="Install automatically", active=False)
        self.ua_user = Adw.EntryRow(title="User name", text=os.environ.get("USER", "user"))
        self.ua_pass = Adw.PasswordEntryRow(title="Password")
        self.ua_key = Adw.EntryRow(title="Product key (optional)")
        for r in (self.ua_switch, self.ua_user, self.ua_pass, self.ua_key):
            self.unattended_group.add(r)
        self.unattended_group.set_visible(False)
        pref.add(self.unattended_group)
        self._check_unattended()
        create_btn = Gtk.Button(label="Create")
        create_btn.add_css_class("suggested-action")
        create_btn.connect("clicked", lambda *_: self._create())
        self.nav.push(_page("Hardware", pref, create_btn))

    def _check_unattended(self):
        iso = self.iso if self.src_iso.get_active() else ""
        entry = self.catalog_entry if self.src_catalog.get_active() else None
        if entry is not None:
            if entry.unattended:
                self.unattended_group.set_visible(True)
                self.ua_key.set_visible(False)
            return
        if not iso:
            return
        from rehome.media import unattended

        def got(support):
            if support.ok:
                self.unattended_group.set_visible(True)
                self.ua_key.set_visible(support.method == "windows")
        run_async(lambda: unattended.check_iso(iso, self.plan.config.os.osinfo_id), got, lambda e: None)

    # --- create --------------------------------------------------------------------------------

    def _answers(self):
        if not (self.unattended_group.get_visible() and self.ua_switch.get_active()):
            return None
        from rehome.media import unattended
        return unattended.Answers(user=self.ua_user.get_text().strip(), password=self.ua_pass.get_text(),
                                  product_key=self.ua_key.get_text().strip())

    def _create(self):
        spec = self._spec()
        spec.name = self.name_row.get_text().strip() or self.plan.config.name
        spec.cpus = int(self.cpus.get_value())
        spec.memory_mib = int(self.mem.get_value())
        try:
            spec.disk_size = parse_size(self.disk.get_text(), "g")
        except ValueError:
            show_error(self, ValueError(f"\"{self.disk.get_text()}\" is not a size such as 64G."))
            return
        spec.firmware = combo_value(self.fw)
        spec.tpm = combo_value(self.tpm)
        spec.network = combo_value(self.net)
        spec.disk_dir = self.disk_loc.folder
        answers = self._answers()
        panel = ProgressPanel("Creating the VM")
        prog = panel.bind(jobs.Progress(title="Creating"))
        self.nav.push(_page("Creating", panel))
        entry = self.catalog_entry if self.src_catalog.get_active() else None

        def work():
            if entry is not None:
                from rehome.media import catalog
                prog.stage("Downloading")

                def dl_progress(done, total, speed=0, eta=0):
                    if total:
                        prog.update(done / total, f"{human(done)} of {human(total)}")
                res = catalog.quick_create(entry.id, name=spec.name, answers=answers,
                                           progress=dl_progress, cancel=prog.is_cancelled,
                                           status=prog.note,
                                           spec_overrides={k: getattr(spec, k) for k in
                                                           ("cpus", "memory_mib", "disk_size", "firmware",
                                                            "tpm", "network", "disk_dir")})
                return res.config, res.notes
            from rehome.vm import create
            prog.stage("Creating the disk")
            cfg = create.create(spec)
            notes = []
            if answers is not None:
                from rehome.media import unattended
                prog.stage("Preparing the automatic installation")
                notes = unattended.prepare(cfg, answers)
            return cfg, notes

        def failed(e):
            self.nav.pop()
            if not isinstance(e, Cancelled):
                show_error(self, e, "The VM could not be created")
        run_async(work, lambda r: self._done(*r), failed)

    def _done(self, cfg, notes):
        has_media = any(c.file for c in cfg.cdroms)
        page = Adw.StatusPage(icon_name="emblem-ok-symbolic", title=f"{cfg.name} is ready",
                              description="\n".join(notes) if notes else
                              "Start it to begin the installation." if has_media else
                              "Insert an installer disc in its settings, or start it to boot "
                              "from the network.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        start = Gtk.Button(label="Start and Open Console")
        start.add_css_class("pill")
        start.add_css_class("suggested-action")
        close = Gtk.Button(label="Close")
        close.add_css_class("pill")
        box.append(start)
        box.append(close)
        page.set_child(box)
        nav_page = _page("Done", page)
        nav_page.set_can_pop(False)
        self.nav.push(nav_page)
        self.main.store.refresh_now()
        self.main.select(cfg.id)

        def start_vm(*_):
            from rehome.vm import runner
            start.set_sensitive(False)

            def go():
                runner.start(cfg)
                return cfg

            def started(c):
                self.main.app.open_console(c)
                after_start(c, self.main.toast)
                self.close()
            run_async(go, started, lambda e: (start.set_sensitive(True),
                                              show_error(self, e, "The VM could not start")))
        start.connect("clicked", start_vm)
        close.connect("clicked", lambda *_: self.close())
