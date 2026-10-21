"""Summary tab: what the VM is, where its disks come from, import results, guest tools."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from rehome.ui.common import confirm, run_async, show_error  # noqa: E402
from rehome.util.units import human  # noqa: E402
from rehome.vm import guest_tools, runner  # noqa: E402

FW = {"bios": "BIOS", "uefi": "UEFI", "uefi-secure": "UEFI with Secure Boot"}


def _row(title, value, icon=""):
    r = Adw.ActionRow(title=GLib.markup_escape_text(title))
    r.set_subtitle(GLib.markup_escape_text(value or "—"))
    r.set_subtitle_selectable(True)
    if icon:
        r.add_prefix(Gtk.Image.new_from_icon_name(icon))
    return r


class SummaryPage(Gtk.ScrolledWindow):
    def __init__(self, page):
        super().__init__(vexpand=True)
        self.page = page
        self.pref = Adw.PreferencesPage()
        self.set_child(self.pref)
        self.groups: list[Adw.PreferencesGroup] = []
        self._sig = None

    def update(self):
        cfg = self.page.cfg
        sig = (cfg.to_dict().__repr__(), self.page.item.status)
        if sig == self._sig:
            return
        self._sig = sig
        for g in self.groups:
            self.pref.remove(g)
        self.groups = []
        self._unattended(cfg)
        self._first_boot(cfg)
        self._system(cfg)
        self._import(cfg)
        self._tools(cfg)

    def _group(self, title, desc=""):
        g = Adw.PreferencesGroup(title=title, description=desc)
        self.pref.add(g)
        self.groups.append(g)
        return g

    def _unattended(self, cfg):
        from rehome.media import unattended
        try:
            pending = unattended.pending(cfg)
        except Exception:  # noqa: BLE001 - a broken state file must not hide the page
            pending = False
        if not pending:
            return
        g = self._group("Automatic installation",
                        "The answer files contain your password (or its hash). When the "
                        "installation has finished, remove them together with the answer disc.")
        r = Adw.ActionRow(title="Answer files and disc")
        btn = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
        btn.set_sensitive(self.page.item.status == runner.STOPPED)
        if not btn.get_sensitive():
            r.set_subtitle("Shut the VM down first")
        btn.connect("clicked", lambda *_: run_async(
            lambda: unattended.cleanup(self.page.cfg),
            lambda _: (self.page.reload(), self.page.window.toast("Answer files removed")),
            lambda e: show_error(self, e)))
        r.add_suffix(btn)
        g.add(r)

    def _first_boot(self, cfg):
        ii = cfg.import_info
        if not ii or ii.first_boot_ok:
            return
        g = self._group("First start", "Start the VM and watch it in the console. When the system "
                                       "has started correctly, confirm it here.")
        r = Adw.ActionRow(title="Did the system start correctly?")
        yes = Gtk.Button(label="Yes, It Works", valign=Gtk.Align.CENTER)
        yes.add_css_class("suggested-action")
        yes.connect("clicked", lambda *_: self._confirm_boot())
        r.add_suffix(yes)
        if ii.patched:
            undo = Gtk.Button(label="Undo Fixes", valign=Gtk.Align.CENTER)
            undo.connect("clicked", lambda *_: self._rollback())
            r.add_suffix(undo)
        g.add(r)

    def _confirm_boot(self):
        run_async(lambda: guest_tools.mark_first_boot_ok(self.page.cfg),
                  lambda _: (self.page.reload(), self.page.window.toast("Great! Guest tools are next.")),
                  lambda e: show_error(self, e))

    def _system(self, cfg):
        m = cfg.machine
        g = self._group("System")
        g.add(_row("Operating system", cfg.os.name or cfg.os.family, "computer-symbolic"))
        accel = {"auto": "automatic (KVM when available)", "kvm": "KVM", "tcg": "emulation (TCG)"}[m.accel] \
            if m.accel in ("auto", "kvm", "tcg") else m.accel
        g.add(_row("Processor", f"{m.cpus} virtual CPU{'s' if m.cpus != 1 else ''}, {m.cpu_model}, {accel}"))
        if m.accel == "tcg" and m.accel_reason:
            g.add(_row("Why emulation", m.accel_reason, "dialog-information-symbolic"))
        g.add(_row("Memory", human(m.memory_mib * 2**20), "media-flash-symbolic"))
        g.add(_row("Firmware", f"{FW.get(m.firmware, m.firmware)}, TPM {m.tpm if m.tpm != 'none' else 'off'}, "
                               f"{'Q35' if m.type == 'q35' else 'i440FX'} chipset, clock in "
                               f"{'local time' if m.rtc == 'localtime' else 'UTC'}",
                   "application-x-firmware-symbolic"))
        for d in cfg.disks:
            src = ""
            if d.source:
                where = d.source.label or d.source.path
                src = f" — {'linked to' if d.source.mode == 'linked' else 'copied from'} {where}"
            g.add(_row(f"Disk {d.id}", f"{d.bus.upper()} {d.file}{src}", "drive-harddisk-symbolic"))
        for c in cfg.cdroms:
            g.add(_row(f"CD/DVD {c.id}", c.file or "empty", "media-optical-symbolic"))
        for n in cfg.nics:
            mode = {"nat": "NAT (shares this computer's connection)", "bridge": f"bridged to {n.bridge}",
                    "none": "not connected"}.get(n.mode, n.mode)
            g.add(_row(f"Network {n.id}", f"{n.model}, {mode}, {n.mac}", "network-wired-symbolic"))
        g.add(_row("Display & sound", f"{cfg.display.device}{' with 3D' if cfg.display.gl else ''}; "
                                      f"sound {cfg.sound.device}", "video-display-symbolic"))
        if self.page.item.status == runner.RUNNING and cfg.guest.agent:
            self._guest_ips(g, cfg)

    def _guest_ips(self, g, cfg):
        r = _row("IP addresses", "asking the guest agent…", "network-workgroup-symbolic")
        g.add(r)

        def got(ifaces):
            if not ifaces:
                r.set_subtitle("unknown (install the QEMU guest agent in the VM)")
                return
            ips = [a["ip-address"] for i in ifaces for a in i.get("ip-addresses", [])
                   if not a["ip-address"].startswith(("127.", "::1", "fe80"))]
            r.set_subtitle(", ".join(ips) or "none")
        run_async(lambda: runner.guest_agent(cfg, "guest-network-get-interfaces"), got, lambda e: None)

    def _import(self, cfg):
        ii = cfg.import_info
        if not ii:
            return
        g = self._group("Import", f"Imported {ii.created[:10]} in {ii.mode} mode, profile {ii.profile}.")
        summary = cfg.dir / "logs" / "patch-summary.txt"
        if summary.exists():
            lines = [ln for ln in summary.read_text().splitlines() if ln.startswith("[")]
            applied = [ln for ln in lines if ln.startswith("[applied]")]
            exp = Adw.ExpanderRow(title="Automatic fixes",
                                  subtitle=f"{len(applied)} applied, {len(lines) - len(applied)} other")
            for ln in lines:
                status, _, rest = ln.partition("] ")
                title, _, msg = rest.partition(": ")
                er = Adw.ActionRow(title=GLib.markup_escape_text(title),
                                   subtitle=GLib.markup_escape_text(msg))
                er.add_prefix(Gtk.Image.new_from_icon_name({
                    "[applied": "emblem-ok-symbolic", "[failed": "dialog-error-symbolic",
                    "[skipped": "dialog-warning-symbolic"}.get(status, "dialog-information-symbolic")))
                exp.add_row(er)
            g.add(exp)
        for n in ii.notes:
            g.add(_row("Note", n, "dialog-warning-symbolic"))
        if ii.patched:
            r = Adw.ActionRow(title="Undo automatic fixes",
                              subtitle="Reverts the VM's disk to how it was imported and restores the settings.")
            b = Gtk.Button(label="Undo…", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: self._rollback())
            r.add_suffix(b)
            g.add(r)
        if any(d.source and d.source.sha256_before for d in cfg.disks):
            r = Adw.ActionRow(title="Source integrity",
                              subtitle="Re-read the original disk and compare its SHA-256 with the one "
                                       "recorded before the import.")
            b = Gtk.Button(label="Verify…", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: self._verify())
            r.add_suffix(b)
            g.add(r)
        if any(d.source and d.source.mode == "linked" for d in cfg.disks):
            r = Adw.ActionRow(title="Linked to the original disk",
                              subtitle="The source must stay connected. Make a standalone copy to "
                                       "remove that requirement.")
            b = Gtk.Button(label="Make Standalone…", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: self._standalone())
            r.add_suffix(b)
            g.add(r)

    def _rollback(self):
        from rehome.patch import engine
        cfg = self.page.cfg
        confirm(self, "Undo all automatic fixes?",
                "The VM's disk returns to exactly how it was imported, and its settings are restored. "
                "Changes made inside the VM since then are lost.", "Undo Fixes",
                lambda: run_async(lambda: engine.rollback(cfg),
                                  lambda _: (self.page.reload(), self.page.window.toast("Fixes undone")),
                                  lambda e: show_error(self, e)), destructive=True)

    def _verify(self):
        from rehome import importer
        cfg = self.page.cfg
        self.page.window.toast("Verifying the source… this reads the whole disk")
        run_async(lambda: importer.verify(cfg),
                  lambda res: self.page.window.toast("The source is unchanged (SHA-256 matches)", 6),
                  lambda e: show_error(self, e))

    def _standalone(self):
        from rehome.vm import lifecycle
        cfg = self.page.cfg
        confirm(self, "Make a standalone copy?",
                "The VM's disk becomes independent of the original (it needs as much space as the "
                "used part of the source). Checkpoints are removed.", "Make Standalone",
                lambda: run_async(lambda: lifecycle.make_standalone(cfg),
                                  lambda _: (self.page.reload(), self.page.window.toast("The VM is standalone now")),
                                  lambda e: show_error(self, e)))

    def _tools(self, cfg):
        ii = cfg.import_info
        if ii and not ii.first_boot_ok:
            return
        plan = guest_tools.plan(cfg)
        if plan.kind == "none":
            return
        g = self._group("Guest tools", "Faster virtual hardware, clipboard sharing, automatic "
                                       "resolution and clean shutdowns.")
        r = Adw.ActionRow(title="Install guest tools", subtitle=GLib.markup_escape_text(plan.message))
        if plan.kind == "windows-iso":
            b = Gtk.Button(label="Download…" if plan.needs_download else "Insert Disc", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: self._tools_iso(plan))
            r.add_suffix(b)
        elif plan.command:
            r.set_subtitle(GLib.markup_escape_text(plan.message + "\n" + plan.command))
            b = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Copy command")
            b.connect("clicked", lambda *_: (b.get_clipboard().set(plan.command),
                                             self.page.window.toast("Command copied")))
            r.add_suffix(b)
        g.add(r)
        if guest_tools.paravirt_changes(cfg) and not cfg.guest.paravirt:
            r = Adw.ActionRow(title="Switch to virtio devices",
                              subtitle="Once the guest tools are installed, use the fast paravirtual "
                                       "disk, network and display. A checkpoint is taken first.")
            b = Gtk.Button(label="Switch…", valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_: self._switch())
            r.add_suffix(b)
            g.add(r)

    def _tools_iso(self, plan):
        cfg = self.page.cfg
        if plan.needs_download:
            confirm(self, "Download virtio-win?",
                    f"Rehome will download the driver disc from the Fedora project:\n{guest_tools.VIRTIO_WIN_URL}",
                    "Download", lambda: run_async(
                        guest_tools.download_virtio_win,
                        lambda iso: (guest_tools.attach_tools(cfg, iso), self.page.reload(),
                                     self.page.window.toast("Driver disc inserted")),
                        lambda e: show_error(self, e)))
            return
        run_async(lambda: guest_tools.attach_tools(cfg, plan.iso),
                  lambda _: (self.page.reload(), self.page.window.toast("Driver disc inserted")),
                  lambda e: show_error(self, e))

    def _switch(self):
        cfg = self.page.cfg
        confirm(self, "Switch to virtio devices?",
                "Only do this after the guest tools/drivers are installed. If the VM does not start "
                "afterwards, revert to the checkpoint \"Before switching to virtio\".", "Switch",
                lambda: run_async(lambda: guest_tools.switch_to_paravirt(cfg),
                                  lambda log: (self.page.reload(),
                                               self.page.window.toast("Switched: " + "; ".join(log), 6)),
                                  lambda e: show_error(self, e)))
