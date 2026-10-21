import json
import os
import stat

from rehome.host import firmware as fw


def _descriptor(d, name, code, vars_, features, machines=("pc-q35-*",)):
    data = {"description": name, "interface-types": ["uefi"],
            "mapping": {"device": "flash", "mode": "split",
                        "executable": {"filename": str(code), "format": "raw"},
                        "nvram-template": {"filename": str(vars_), "format": "raw"}},
            "targets": [{"architecture": "x86_64", "machines": list(machines)}],
            "features": list(features)}
    (d / name).write_text(json.dumps(data))


def _arch_like(tmp_path):
    d = tmp_path / "firmware"
    d.mkdir()
    code, sbcode, vars_ = tmp_path / "CODE.fd", tmp_path / "CODE.secboot.fd", tmp_path / "VARS.fd"
    for f in (code, sbcode, vars_):
        f.write_bytes(b"\0" * 4096)
    _descriptor(d, "50-secure.json", sbcode, vars_, ["requires-smm", "secure-boot"])
    _descriptor(d, "60-plain.json", code, vars_, [], ("pc-q35-*", "pc-i440fx-*"))
    return d


def _fake_tool(tmp_path, monkeypatch):
    log = tmp_path / "calls"
    tool = tmp_path / "bin" / "virt-fw-vars"
    tool.parent.mkdir()
    tool.write_text(f"""#!/bin/sh
echo "$@" >> {log}
while [ $# -gt 0 ]; do
  case "$1" in --input) in="$2"; shift;; --output) out="$2"; shift;; esac; shift
done
cp "$in" "$out"
""")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    return log


def _req(**kw):
    return fw.FirmwareRequest(arch="x86_64", machine="pc-q35-9.1", **kw)


def test_no_tool_no_enrolled_firmware(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    descs = fw.load_descriptors([str(_arch_like(tmp_path))])
    assert fw.self_enrolled(descs, "x86_64") == []
    assert fw.select(_req(secure_boot=True, enrolled_keys=True), descs) is None


def test_enrolls_once_and_is_selected_for_windows_11(tmp_path, monkeypatch):
    log = _fake_tool(tmp_path, monkeypatch)
    descs = fw.load_descriptors([str(_arch_like(tmp_path))])
    extra = fw.self_enrolled(descs, "x86_64")
    assert len(extra) == 1 and extra[0].enrolled_keys and extra[0].secure_boot
    assert extra[0].nvram_template.startswith(str(fw.enrolled_dir()))
    assert "--enroll-redhat --secure-boot" in log.read_text()
    chosen = fw.select(_req(secure_boot=True, enrolled_keys=True), descs + extra)
    assert chosen is extra[0] and chosen.executable.endswith("CODE.secboot.fd")
    # Plain Secure Boot (e.g. Linux with own keys) still prefers the empty store.
    assert not fw.select(_req(secure_boot=True), descs + extra).enrolled_keys
    # Second discovery reuses the enrolled copy.
    assert len(fw.self_enrolled(descs, "x86_64")) == 1
    assert len(log.read_text().splitlines()) == 1


def test_distro_enrolled_store_wins(tmp_path, monkeypatch):
    log = _fake_tool(tmp_path, monkeypatch)
    d = _arch_like(tmp_path)
    _descriptor(d, "40-enrolled.json", tmp_path / "CODE.secboot.fd", tmp_path / "VARS.fd",
                ["requires-smm", "secure-boot", "enrolled-keys"])
    assert fw.self_enrolled(fw.load_descriptors([str(d)]), "x86_64") == []
    assert not log.exists()
