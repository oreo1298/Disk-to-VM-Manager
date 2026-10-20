"""Settings tab: edit a VM's hardware (while it is off)."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from rehome.host import info as host_info  # noqa: E402
from rehome.ui.common import combo_row, combo_value, confirm, run_async, show_error  # noqa: E402
from rehome.util.units import human, parse_size  # noqa: E402
from rehome.vm import config as C  # noqa: E402
from rehome.vm import disks as disk_mod  # noqa: E402
from rehome.vm import edit  # noqa: E402
from rehome.vm.config import CdRom, Disk, Nic, Share, VMConfig  # noqa: E402

DISK_BUS_LABELS = {"virtio": "VirtIO (fastest, needs drivers)", "sata": "SATA (AHCI)", "ide": "IDE",
                   "scsi": "SCSI (virtio-scsi)", "nvme": "NVMe", "usb": "USB"}
NIC_LABELS = {"virtio-net-pci": "VirtIO (fastest, needs drivers)", "e1000e": "Intel 82574 (e1000e)",
              "e1000": "Intel PRO/1000 (e1000)", "rtl8139": "Realtek RTL8139", "pcnet": "AMD PCnet",
              "ne2k_pci": "NE2000 PCI", "ne2k_isa": "NE2000 ISA", "vmxnet3": "VMware vmxnet3",
              "usb-net": "USB network"}
DISPLAY_LABELS = {"virtio-vga": "VirtIO GPU (resizable)", "qxl-vga": "QXL", "VGA": "Standard VGA",
                  "cirrus-vga": "Cirrus Logic (legacy)", "vmware-svga": "VMware SVGA",
                  "bochs-display": "Bochs display", "virtio-gpu-pci": "VirtIO GPU (no VGA)",
                  "ramfb": "RAM framebuffer", "none": "None (headless)"}
SOUND_LABELS = {"none": "None", "ich9-intel-hda": "Intel HD Audio (ICH9)", "intel-hda": "Intel HD Audio",
                "AC97": "AC'97", "sb16": "Sound Blaster 16", "ES1370": "Ensoniq ES1370",
                "usb-audio": "USB audio"}
USB_LABELS = {"none": "None", "qemu-xhci": "USB 3 (xHCI)", "ich9-usb-ehci1": "USB 2 (EHCI + UHCI)",
              "usb-ehci": "USB 2 (EHCI only)", "piix3-usb-uhci": "USB 1.1 (UHCI)",
              "piix4-usb-uhci": "USB 1.1 (PIIX4 UHCI)", "nec-usb-xhci": "USB 3 (NEC xHCI)"}
POINTER_LABELS = {"usb-tablet": "USB tablet (pointer follows the mouse)", "virtio-tablet": "VirtIO tablet",
                  "ps2": "PS/2 mouse (captured)", "usb-mouse": "USB mouse (captured)"}


def _items(d: dict) -> tuple[list[str], list[str]]:
    return list(d.values()), list(d.keys())


class SettingsPage(Gtk.Box):
    def __init__(self, page):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.page = page
        self.banner = Adw.Banner(title="Shut the VM down to change its hardware.")
        self.append(self.banner)
        bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=18, margin_end=18,
                      halign=Gtk.Align.END)
        self.revert_btn = Gtk.Button(label="Discard Changes")
        self.revert_btn.connect("clicked", lambda *_: self.load())
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", lambda *_: self.apply())
        bar.append(self.revert_btn)
        bar.append(self.apply_btn)
        self.append(bar)
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.append(self.scroller)
        self.active = False
        self.loaded_for = None
        self.load()

    def update_state(self, active: bool):
        changed = active != self.active
        self.active = active
        self.banner.set_revealed(active)
        if changed or self.loaded_for != self.page.cfg.id:
            self.load()

    # --- building -------------------------------------------------------------------

    def load(self):
        cfg = self.page.cfg
        self.loaded_for = cfg.id
        self.work = VMConfig.from_dict(cfg.to_dict())
        self.work.path = cfg.path
        host = host_info.gather()
        pref = Adw.PreferencesPage()
        hw_ok = not self.active

        g = Adw.PreferencesGroup(title="General")
        self.name_row = Adw.EntryRow(title="Name", text=cfg.name)
        self.notes_row = Adw.EntryRow(title="Notes", text=cfg.notes)
        g.add(self.name_row)
        g.add(self.notes_row)
        pref.add(g)

        m = self.work.machine
        g = Adw.PreferencesGroup(title="Processor and memory",
                                 description=f"This computer has {host.threads} CPU threads and "
                                             f"{human(host.memory.total_mib * 2**20)} of memory.")
        self.cpus = Adw.SpinRow.new_with_range(1, max(1, host.threads), 1)
        self.cpus.set_title("Virtual CPUs")
        self.cpus.set_value(m.cpus)
        self.mem = Adw.SpinRow.new_with_range(16, max(256, host.memory.max_vm_mib), 64)
        self.mem.set_title("Memory (MiB)")
        self.mem.set_subtitle(f"Up to {host.memory.max_vm_mib} MiB leaves enough for this computer")
        self.mem.set_value(m.memory_mib)
        models = ["host", "max", "qemu64", "qemu32", "kvm64", "Skylake-Client", "EPYC", "Nehalem",
                  "pentium3", "pentium2", "pentium", "486"]
        if m.cpu_model not in models:
            models.insert(0, m.cpu_model)
        self.cpu_model = combo_row("CPU model", models, models, m.cpu_model,
                                   "\"host\" passes your CPU through (fastest with KVM)")
        self.accel = combo_row("Acceleration", ["Automatic (KVM when available)", "KVM only",
                                                "Emulation (TCG)"], ["auto", "kvm", "tcg"], m.accel)
        for r in (self.cpus, self.mem, self.cpu_model, self.accel):
            g.add(r)
        pref.add(g)

        g = Adw.PreferencesGroup(title="Firmware and security")
        self.firmware = combo_row("Firmware", ["BIOS", "UEFI", "UEFI with Secure Boot"],
                                  ["bios", "uefi", "uefi-secure"], m.firmware,
                                  "Changing this on an installed system usually stops it booting")
        self.tpm = combo_row("TPM", ["None", "TPM 2.0", "TPM 1.2"], ["none", "2.0", "1.2"], m.tpm,
                             "Windows 11 and BitLocker need TPM 2.0")
        self.machine = combo_row("Chipset", ["Q35 (modern, PCIe)", "i440FX (legacy, PCI)"], ["q35", "pc"], m.type)
        self.rtc = combo_row("Hardware clock", ["UTC (Linux, BSD)", "Local time (Windows, DOS)"],
                             ["utc", "localtime"], m.rtc)
        for r in (self.firmware, self.tpm, self.machine, self.rtc):
            g.add(r)
        pref.add(g)

        g = Adw.PreferencesGroup(title="Storage")
        add_disk = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a disk", valign=Gtk.Align.CENTER)
        add_disk.add_css_class("flat")
        add_disk.connect("clicked", lambda *_: self._add_disk())
        add_disk.set_sensitive(hw_ok)
        g.set_header_suffix(add_disk)
        self.disk_rows = []
        for d in self.work.disks:
            exp = Adw.ExpanderRow(title=f"Disk {d.id}", subtitle=GLib.markup_escape_text(d.file))
            labels, values = _items(DISK_BUS_LABELS)
            bus = combo_row("Bus", labels, values, d.bus)
            exp.add_row(bus)
            size_row = Adw.ActionRow(title="Size")
            run_async(lambda d=d: disk_mod.info(self.work.resolve(d.file)) if os.path.exists(self.work.resolve(d.file)) else {},
                      lambda info, r=size_row: r.set_subtitle(human(info.get("virtual-size", 0)) if info else "?"),
                      lambda e: None)
            grow = Gtk.Button(label="Enlarge…", valign=Gtk.Align.CENTER)
            grow.connect("clicked", lambda *_, d=d: self._resize(d))
            grow.set_sensitive(hw_ok and d.format in ("qcow2", "raw"))
            size_row.add_suffix(grow)
            exp.add_row(size_row)
            if d.source:
                exp.add_row(Adw.ActionRow(title="Source", subtitle=GLib.markup_escape_text(
                    f"{d.source.mode}: {d.source.label or d.source.path}")))
            rm = Adw.ActionRow(title="Remove this disk", subtitle="The file is kept")
            rmb = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            rmb.add_css_class("flat")
            rmb.connect("clicked", lambda *_, d=d: self._remove_disk(d))
            rmb.set_sensitive(hw_ok)
            rm.add_suffix(rmb)
            exp.add_row(rm)
            g.add(exp)
            self.disk_rows.append((d, bus))
        self.cd_rows = []
        for cd in self.work.cdroms:
            r = Adw.ActionRow(title=f"CD/DVD {cd.id}", subtitle=GLib.markup_escape_text(cd.file or "empty"))
            choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
            choose.connect("clicked", lambda *_, cd=cd, r=r: self._choose_iso(cd, r))
            eject = Gtk.Button(icon_name="media-eject-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Eject")
            eject.add_css_class("flat")
            eject.connect("clicked", lambda *_, cd=cd, r=r: (setattr(cd, "file", ""), r.set_subtitle("empty")))
            r.add_suffix(choose)
            r.add_suffix(eject)
            g.add(r)
            self.cd_rows.append(cd)
        add_cd = Adw.ActionRow(title="Add a CD/DVD drive")
        b = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        b.add_css_class("flat")
        b.set_sensitive(hw_ok)
        b.connect("clicked", lambda *_: self._add_cd())
        add_cd.add_suffix(b)
        g.add(add_cd)
        boot_ids = [d.id for d in self.work.disks] + [c.id for c in self.work.cdroms]
        first = min(((d.boot_index, d.id) for d in [*self.work.disks, *self.work.cdroms] if d.boot_index),
                    default=(0, boot_ids[0] if boot_ids else ""))[1]
        self.boot_first = combo_row("Boot from", boot_ids or ["-"], boot_ids or ["-"], first)
        g.add(self.boot_first)
        pref.add(g)

        g = Adw.PreferencesGroup(title="Network")
        self.nic_rows = []
        for n in self.work.nics:
            exp = Adw.ExpanderRow(title=f"Network card {n.id}", subtitle=f"{n.model}, {n.mode}")
            mode = combo_row("Connection", ["NAT (share this computer's connection)", "Bridged",
                                            "Not connected"], ["nat", "bridge", "none"], n.mode)
            bridge = Adw.EntryRow(title="Bridge (for bridged mode)", text=n.bridge)
            labels, values = _items(NIC_LABELS)
            model = combo_row("Network card", labels, values, n.model)
            mac = Adw.EntryRow(title="MAC address", text=n.mac)
            fwd = Adw.EntryRow(title="Port forwards (NAT), e.g. tcp:2222:22", text=", ".join(n.port_forwards))
            for r in (mode, bridge, model, mac, fwd):
                exp.add_row(r)
            g.add(exp)
            self.nic_rows.append((n, mode, bridge, model, mac, fwd))
        if not self.work.nics:
            add = Adw.ActionRow(title="Add a network card")
            b = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_: (self.work.nics.append(Nic(id="net0")), self._rebuild_keep()))
            b.set_sensitive(hw_ok)
            add.add_suffix(b)
            g.add(add)
        pref.add(g)

        g = Adw.PreferencesGroup(title="Display, input and sound")
        labels, values = _items(DISPLAY_LABELS)
        self.display = combo_row("Graphics", labels, values, self.work.display.device)
        self.gl = Adw.SwitchRow(title="3D acceleration", subtitle="virgl, for Linux guests with a "
                                                                   "VirtIO GPU", active=self.work.display.gl)
        labels, values = _items(POINTER_LABELS)
        self.pointer = combo_row("Pointer", labels, values, self.work.input.pointer)
        labels, values = _items(USB_LABELS)
        self.usb = combo_row("USB controller", labels, values, self.work.usb.controller)
        labels, values = _items(SOUND_LABELS)
        self.sound = combo_row("Sound card", labels, values, self.work.sound.device)
        for r in (self.display, self.gl, self.pointer, self.usb, self.sound):
            g.add(r)
        pref.add(g)

        g = Adw.PreferencesGroup(title="Shared folders",
                                 description="Shared with the VM through virtiofs (Linux guests; Windows "
                                             "needs WinFsp and the virtio-win file system service).")
        for s in self.work.shares:
            r = Adw.ActionRow(title=GLib.markup_escape_text(s.tag), subtitle=GLib.markup_escape_text(s.path))
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            rm.add_css_class("flat")
            rm.connect("clicked", lambda *_, s=s: (self.work.shares.remove(s), self._rebuild_keep()))
            rm.set_sensitive(hw_ok)
            r.add_suffix(rm)
            g.add(r)
        add = Adw.ActionRow(title="Share a folder…")
        b = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        b.add_css_class("flat")
        b.set_sensitive(hw_ok)
        b.connect("clicked", lambda *_: self._add_share())
        add.add_suffix(b)
        g.add(add)
        pref.add(g)

        for widget in (self.cpus, self.mem, self.cpu_model, self.accel, self.firmware, self.tpm,
                       self.machine, self.rtc, self.boot_first, self.display, self.gl, self.pointer,
                       self.usb, self.sound):
            widget.set_sensitive(hw_ok)
        for row in self.disk_rows:
            row[1].set_sensitive(hw_ok)
        for row in self.nic_rows:
            for w in row[1:]:
                w.set_sensitive(hw_ok)
        self.scroller.set_child(pref)

    def _rebuild_keep(self):
        """Rebuild the form from self.work (after adding/removing list items)."""
        work = self.work
        self._collect(work)
        saved_cfg = self.page.item.cfg
        self.page.item.cfg = work
        try:
            self.load()
        finally:
            self.page.item.cfg = saved_cfg
        self.work = work

    # --- applying ------------------------------------------------------------------------

    def _collect(self, new: VMConfig) -> None:
        new.name = self.name_row.get_text().strip()
        new.notes = self.notes_row.get_text()
        if self.active:
            return
        m = new.machine
        m.cpus = int(self.cpus.get_value())
        m.memory_mib = int(self.mem.get_value())
        m.cpu_model = combo_value(self.cpu_model)
        m.accel = combo_value(self.accel)
        m.firmware = combo_value(self.firmware)
        m.tpm = combo_value(self.tpm)
        m.type = combo_value(self.machine)
        m.rtc = combo_value(self.rtc)
        for d, bus in self.disk_rows:
            for nd in new.disks:
                if nd.id == d.id:
                    nd.bus = combo_value(bus)
        for n, mode, bridge, model, mac, fwd in self.nic_rows:
            for nn in new.nics:
                if nn.id == n.id:
                    nn.mode = combo_value(mode)
                    nn.bridge = bridge.get_text().strip()
                    nn.model = combo_value(model)
                    nn.mac = mac.get_text().strip()
                    nn.port_forwards = [x.strip() for x in fwd.get_text().split(",") if x.strip()]
        new.display.device = combo_value(self.display)
        new.display.gl = self.gl.get_active()
        new.input.pointer = combo_value(self.pointer)
        new.usb.controller = combo_value(self.usb)
        new.sound.device = combo_value(self.sound)
        first = combo_value(self.boot_first)
        if first and first != "-":
            try:
                edit.set_boot_first(new, first)
            except Exception:
                pass

    def apply(self):
        new = VMConfig.from_dict(self.work.to_dict())
        new.path = self.page.cfg.path
        self._collect(new)
        run_async(lambda: edit.save_settings(new),
                  lambda _: (self.page.reload(), self.load(), self.page.window.toast("Settings saved")),
                  lambda e: show_error(self, e, "The settings were not saved"))

    # --- list operations ---------------------------------------------------------------

    def _add_disk(self):
        entry = Gtk.Entry(text="32G", activates_default=True)
        dlg = Adw.AlertDialog(heading="Add a disk", body="Size of the new disk (it grows as data is written):")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Add")
        dlg.set_default_response("ok")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "ok":
                return
            try:
                size = parse_size(entry.get_text(), "g")
            except ValueError:
                show_error(self, ValueError(f"\"{entry.get_text()}\" is not a size such as 32G."))
                return
            did = self.work.next_id("disk", self.work.disks)
            rel = f"disks/{did}.qcow2"
            path = self.work.resolve(rel)
            bus = self.work.disks[0].bus if self.work.disks else "virtio"

            def make():
                disk_mod.create(path, size)
                self.work.disks.append(Disk(id=did, file=rel, bus=bus))
            run_async(make, lambda _: self._rebuild_keep(), lambda e: show_error(self, e))
        dlg.connect("response", resp)
        dlg.present(self.get_root())

    def _remove_disk(self, d):
        confirm(self, f"Remove {d.id}?", "The disk is detached from the VM; its file is kept on disk.",
                "Remove", lambda: (self.work.disks.remove(next(x for x in self.work.disks if x.id == d.id)),
                                   self._rebuild_keep()), destructive=True)

    def _resize(self, d):
        entry = Gtk.Entry(text="", placeholder_text="e.g. 128G", activates_default=True)
        dlg = Adw.AlertDialog(heading=f"Enlarge {d.id}",
                              body="New size. The guest must then extend its partition itself.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Enlarge")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r == "ok":
                try:
                    size = parse_size(entry.get_text(), "g")
                except ValueError:
                    return
                run_async(lambda: disk_mod.resize(self.work.resolve(d.file), size),
                          lambda _: (self.page.window.toast(f"{d.id} enlarged"), self._rebuild_keep()),
                          lambda e: show_error(self, e))
        dlg.connect("response", resp)
        dlg.present(self.get_root())

    def _choose_iso(self, cd, row):
        f = Gtk.FileFilter()
        f.set_name("Disc images")
        f.add_pattern("*.iso")
        f.add_pattern("*.ISO")
        dlg = Gtk.FileDialog(title="Choose a disc image", default_filter=f)

        def done(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            cd.file = path
            row.set_subtitle(GLib.markup_escape_text(path))
            from rehome.vm import runner
            if self.active:
                run_async(lambda: runner.change_cd(self.page.cfg, cd.id, path),
                          lambda _: self.page.window.toast("Disc inserted"), lambda e: show_error(self, e))
        dlg.open(self.get_root(), None, done)

    def _add_cd(self):
        cid = self.work.next_id("cd", self.work.cdroms)
        self.work.cdroms.append(CdRom(id=cid, bus="ide" if self.work.machine.type == "pc" else "sata"))
        self._rebuild_keep()

    def _add_share(self):
        dlg = Gtk.FileDialog(title="Choose a folder to share")

        def done(d, res):
            try:
                path = d.select_folder_finish(res).get_path()
            except GLib.Error:
                return
            tag = os.path.basename(path.rstrip("/")) or "share"
            self.work.shares.append(Share(tag=tag[:30], path=path))
            self._rebuild_keep()
        dlg.select_folder(self.get_root(), None, done)
