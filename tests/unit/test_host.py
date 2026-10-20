import json
import os

import pytest

from rehome.host import cpu, firmware, memory, system
from rehome.host.info import HostInfo, choose_accel
from rehome.host.qemu import QemuCaps, parse_help_list, parse_version, distinct_arches

INTEL = """processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Intel(R) Core(TM) i7-8650U CPU @ 1.90GHz
flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic vmx est tm2 ssse3 sse4_1 sse4_2 popcnt
vmx flags\t: vnmi preemption_timer invvpid ept_x_only ept_ad
"""
AMD_DISABLED = """processor\t: 0
vendor_id\t: AuthenticAMD
model name\t: AMD Ryzen 7 5800X 8-Core Processor
flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic sse4_2 popcnt
"""
GUEST = """vendor_id\t: GenuineIntel
model name\t: Intel Xeon Processor
flags\t\t: fpu vme hypervisor sse4_2
"""
ARM = """processor\t: 0
BogoMIPS\t: 48.00
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32
CPU implementer\t: 0x41
CPU architecture: 8
"""


def test_parse_cpuinfo_intel():
    c = cpu.parse_cpuinfo(INTEL, "x86_64")
    assert c.vendor_label == "Intel"
    assert c.virt_extension == "vmx" and c.virt_name == "Intel VT-x"
    assert c.vmx_flags_line
    assert not c.running_under_hypervisor


def test_parse_cpuinfo_amd_disabled_in_firmware():
    c = cpu.parse_cpuinfo(AMD_DISABLED, "x86_64")
    assert c.vendor_label == "AMD"
    assert c.virt_extension == ""


def test_parse_cpuinfo_guest_and_arm():
    assert cpu.parse_cpuinfo(GUEST, "x86_64").running_under_hypervisor
    a = cpu.parse_cpuinfo(ARM, "aarch64")
    assert a.vendor == "ARM"
    assert "asimd" in a.flags


def test_canonical_arch():
    assert cpu.canonical_arch("amd64") == "x86_64"
    assert cpu.canonical_arch("i686") == "i386"
    assert cpu.canonical_arch("arm64") == "aarch64"


def test_detect_kvm_fake_sysfs(tmp_path):
    (tmp_path / "module/kvm_amd/parameters").mkdir(parents=True)
    (tmp_path / "module/kvm_amd/parameters/nested").write_text("1\n")
    c = cpu.parse_cpuinfo(AMD_DISABLED.replace("apic", "apic svm"), "x86_64")
    st = cpu.detect_kvm(c, dev=str(tmp_path / "nokvm"), sys_root=str(tmp_path))
    assert st.module == "kvm_amd" and st.module_loaded and st.nested is True
    assert not st.device_exists


@pytest.mark.parametrize("text,family,pid", [
    ('NAME="Arch Linux"\nID=arch\nPRETTY_NAME="Arch Linux"\n', "arch", "arch"),
    ('NAME="Manjaro Linux"\nID=manjaro\nID_LIKE=arch\n', "arch", "manjaro"),
    ('NAME="EndeavourOS"\nID="endeavouros"\nID_LIKE="arch"\n', "arch", "endeavouros"),
    ('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n', "debian", "ubuntu"),
    ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "debian", "linuxmint"),
    ('ID=debian\nVERSION_ID="13"\n', "debian", "debian"),
    ('ID=fedora\nVERSION_ID=41\nVARIANT_ID=workstation\n', "fedora", "fedora"),
    ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "suse", "opensuse-tumbleweed"),
    ('ID="opensuse-leap"\nID_LIKE="suse opensuse"\nVERSION_ID="15.6"\n', "suse", "opensuse-leap"),
])
def test_os_release_family(text, family, pid):
    o = system.parse_os_release(text)
    assert o.family == family
    assert o.id == pid


def test_detect_session():
    s = system.detect_session({"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland",
                               "WAYLAND_DISPLAY": "wayland-0"})
    assert s.session_type == "wayland" and s.is_graphical
    assert system.desktop_family("ubuntu:GNOME") == "gnome"
    s2 = system.detect_session({"DISPLAY": ":0"})
    assert s2.session_type == "x11"


def test_meminfo_and_sizing():
    m = memory.parse_meminfo("MemTotal:       32768000 kB\nMemAvailable:   20000000 kB\n")
    assert m.total_mib == 32000
    assert m.host_reserve_mib == 8000
    assert m.default_vm_mib() == 4096
    assert m.default_vm_mib(recommended=65536) == 16000  # capped to half the host
    small = memory.parse_meminfo("MemTotal: 4000000 kB\nMemFree: 1000 kB\n")
    assert small.max_vm_mib == 3906 - 2048
    assert small.default_vm_mib() <= small.max_vm_mib


def _descriptor(path, name, code, nvram, features, machines, fmt="raw", device="flash"):
    data = {
        "description": name, "interface-types": ["uefi"],
        "mapping": {"device": device, "executable": {"filename": str(code), "format": fmt},
                    "nvram-template": {"filename": str(nvram), "format": fmt}},
        "targets": [{"architecture": "x86_64", "machines": machines}],
        "features": features,
    }
    path.write_text(json.dumps(data))


def test_firmware_selection(tmp_path):
    d = tmp_path / "fw"
    d.mkdir()
    code, vars_, ms_code, ms_vars = (tmp_path / n for n in ("c.fd", "v.fd", "msc.qcow2", "msv.qcow2"))
    for f in (code, vars_, ms_code, ms_vars):
        f.write_bytes(b"x")
    _descriptor(d / "40-sb-enrolled.json", "sb", ms_code, ms_vars,
                ["enrolled-keys", "secure-boot", "requires-smm"], ["pc-q35-*"], fmt="qcow2")
    _descriptor(d / "60-plain.json", "plain", code, vars_, ["acpi-s3"], ["pc-i440fx-*", "pc-q35-*"])
    (d / "50-masked.json").write_text("")
    descs = firmware.load_descriptors([str(d)])
    assert [f.description for f in descs] == ["sb", "plain"]
    req = firmware.FirmwareRequest("x86_64", "pc-q35-9.1", uefi=True)
    assert firmware.select(req, descs).description == "plain"
    req_sb = firmware.FirmwareRequest("x86_64", "pc-q35-9.1", uefi=True, secure_boot=True,
                                      enrolled_keys=True)
    sb = firmware.select(req_sb, descs)
    assert sb.description == "sb" and sb.executable_format == "qcow2" and sb.requires_smm
    # Secure Boot is not offered on i440fx by these descriptors.
    req_pc = firmware.FirmwareRequest("x86_64", "pc-i440fx-9.1", uefi=True, secure_boot=True)
    assert firmware.select(req_pc, descs) is None


def test_firmware_user_override(tmp_path):
    sysdir, userdir = tmp_path / "sys", tmp_path / "user"
    sysdir.mkdir(), userdir.mkdir()
    a, b = tmp_path / "a.fd", tmp_path / "b.fd"
    a.write_bytes(b"a"), b.write_bytes(b"b")
    _descriptor(sysdir / "60-x.json", "system", a, a, [], ["pc-q35-*"])
    _descriptor(userdir / "60-x.json", "user", b, b, [], ["pc-q35-*"])
    descs = firmware.load_descriptors([str(sysdir), str(userdir)])
    assert [f.description for f in descs] == ["user"]


def test_qemu_parsers():
    assert parse_version("QEMU emulator version 9.1.0 (qemu-9.1.0-1.fc41)\n")[0] == (9, 1, 0)
    assert parse_help_list("Available display backend types:\nnone\ngtk\ndbus\n") == {"none", "gtk", "dbus"}
    assert distinct_arches({"x86_64": "/usr/bin/qemu-system-x86_64",
                            "x86_64-spice": "/usr/bin/qemu-system-x86_64-spice",
                            "i386": "/usr/bin/qemu-system-i386"}) == {"x86_64", "i386"}
    caps = QemuCaps(binary="/x")
    from rehome.host.qemu import Machine
    caps.machines = {"pc-q35-9.1": Machine("pc-q35-9.1", alias="q35")}
    assert caps.resolve_machine("q35") == "pc-q35-9.1"
    assert QemuCaps.from_dict(caps.to_dict()).resolve_machine("q35") == "pc-q35-9.1"


def _host(kvm_ok=True, arch="x86_64"):
    c = cpu.parse_cpuinfo(INTEL, arch)
    k = cpu.KvmStatus(device_exists=kvm_ok, accessible=kvm_ok)
    h = HostInfo(cpu=c, kvm=k, memory=memory.MemoryInfo(16000, 8000), topology=(1, 4, 8),
                 os_release=system.OsRelease(id="arch"), package_manager="pacman",
                 session=system.Session())
    caps = QemuCaps(binary="/usr/bin/qemu-system-x86_64", accels={"kvm", "tcg"})
    h._caps["/usr/bin/qemu-system-x86_64"] = caps
    return h


def test_choose_accel(monkeypatch):
    from rehome.host import qemu as q
    monkeypatch.setattr(q, "system_binary", lambda arch: "/usr/bin/qemu-system-x86_64")
    assert choose_accel(_host(), "x86_64").accel == "kvm"
    assert choose_accel(_host(), "i686").accel == "kvm"
    nokvm = choose_accel(_host(kvm_ok=False), "x86_64")
    assert nokvm.accel == "tcg" and "slower" in nokvm.warning
    cross = choose_accel(_host(), "aarch64")
    assert cross.accel == "tcg" and "cannot use KVM" in cross.reason
    forced = choose_accel(_host(), "i386", prefer="tcg", prefer_reason="Windows 95 needs a slow CPU")
    assert forced.accel == "tcg" and forced.warning == ""
