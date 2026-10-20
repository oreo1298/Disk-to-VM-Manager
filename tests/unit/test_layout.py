"""packaging/layout.py (the install layout), install.sh basics and the packaging
files that must stay in sync with data/packages.toml and the version."""

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import rehome
from rehome.host import tools

ROOT = Path(__file__).resolve().parents[2]
APP_ID = "io.github.oreo1298.Rehome"


def _load_layout():
    spec = importlib.util.spec_from_file_location("rehome_layout", ROOT / "packaging" / "layout.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rehome_layout"] = mod
    spec.loader.exec_module(mod)
    return mod


layout = _load_layout()


def test_version_matches_package():
    assert layout.read_version(ROOT) == rehome.__version__


def test_default_layout_paths():
    lay = layout.Layout()
    files = lay.files()
    assert "/usr/local/bin/rehome" in files
    assert "/usr/local/lib/rehome/rehome/__init__.py" in files
    assert "/usr/local/lib/rehome/rehome/cli/main.py" in files
    assert "/usr/local/share/rehome/packages.toml" in files
    assert "/usr/local/libexec/rehome/rehome-helper" in files
    assert f"/usr/share/polkit-1/actions/{APP_ID}.policy" in files
    assert f"/usr/local/share/applications/{APP_ID}.desktop" in files
    assert f"/usr/local/share/metainfo/{APP_ID}.metainfo.xml" in files
    assert f"/usr/local/share/icons/hicolor/scalable/apps/{APP_ID}.svg" in files
    assert f"/usr/local/share/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg" in files
    # Templates, the helper source and caches are not copied verbatim.
    assert not any(f.endswith((".in", ".pyc")) or "__pycache__" in f for f in files)
    assert "/usr/local/lib/rehome/rehome/helper/rehome-helper" not in files
    assert not any(f.startswith("/usr/local/share/rehome/icons/") for f in files)
    assert len(files) == len(set(files))
    for f in files:
        assert lay.owns(f), f


@pytest.mark.parametrize("prefix,libexec", [
    ("/usr/local", None),                    # install.sh / make install
    ("/usr", "/usr/lib/rehome"),             # Arch
    ("/usr", "/usr/libexec/rehome"),         # .deb / .rpm
])
def test_helper_location_is_known_to_doctor(prefix, libexec):
    helper = layout.Layout(prefix=prefix, libexecdir=libexec).helper
    src = Path(tools.__file__).read_text()
    assert f'"{helper}"' in src, f"{helper} missing from tools.helper_installed()"


def test_install_renders_templates(tmp_path):
    dest = tmp_path / "stage"
    lay = layout.Layout(prefix="/usr", libexecdir="/usr/libexec/rehome",
                        python="/usr/bin/python3", destdir=str(dest))
    res = layout.install(lay)
    assert res["files"] == lay.files()
    launcher = (dest / "usr/bin/rehome").read_text()
    assert launcher.startswith("#!/usr/bin/python3\n")
    assert "sys.path.insert(0, '/usr/lib/rehome')" in launcher
    assert "'/usr/share/rehome'" in launcher and "'/usr/libexec/rehome/rehome-helper'" in launcher
    assert "pycache_prefix" in launcher
    compile(launcher, "rehome", "exec")
    helper = dest / "usr/libexec/rehome/rehome-helper"
    assert helper.read_text().splitlines()[0] == "#!/usr/bin/python3 -I"
    assert helper.read_text().splitlines()[1:] == \
        (ROOT / "rehome/helper/rehome-helper").read_text().splitlines()[1:]
    policy = (dest / f"usr/share/polkit-1/actions/{APP_ID}.policy").read_text()
    assert "@HELPER@" not in policy
    assert policy.count("/usr/libexec/rehome/rehome-helper") >= 4
    desktop = (dest / f"usr/share/applications/{APP_ID}.desktop").read_text()
    assert "Exec=/usr/bin/rehome" in desktop and "@" not in desktop
    for f in res["files"]:
        mode = stat.S_IMODE((dest / f.lstrip("/")).stat().st_mode)
        expected = 0o755 if f in ("/usr/bin/rehome", "/usr/libexec/rehome/rehome-helper") else 0o644
        assert mode == expected, f
    for d in (dest / "usr/lib/rehome").rglob("*"):
        if d.is_dir():
            assert stat.S_IMODE(d.stat().st_mode) == 0o755


def test_installed_launcher_runs(tmp_path):
    """The generated launcher imports Rehome from the staged tree."""
    dest = tmp_path / "stage"
    lay = layout.Layout(prefix="/usr", python=sys.executable, destdir=str(dest))
    layout.install(lay)
    # Point the launcher at the staged tree instead of /usr.
    text = (dest / "usr/bin/rehome").read_text().replace("'/usr/lib/rehome'", repr(str(dest / "usr/lib/rehome")))
    env = dict(os.environ, XDG_CACHE_HOME=str(tmp_path / "cache"))
    out = subprocess.run([sys.executable, "-c", text.split("\n", 1)[1], "--version"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"rehome {rehome.__version__}"
    # Bytecode went to the cache, not next to the installed sources.
    assert not list((dest / "usr/lib/rehome").rglob("__pycache__"))
    assert list((tmp_path / "cache/rehome/pycache").rglob("*.pyc"))


def _copy_source(tmp_path):
    src = tmp_path / "src"
    for name in ("rehome", "data", "packaging"):
        shutil.copytree(ROOT / name, src / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "install.sh", src / "install.sh")
    return src


def test_manifest_install_update_uninstall_roundtrip(tmp_path):
    src = _copy_source(tmp_path)
    dest = tmp_path / "root"
    lay = layout.Layout(destdir=str(dest), source=src)
    extra = {"packages": {"manager": "apt", "installed": ["qemu-utils"]},
             "groups": [{"user": "alice", "group": "kvm"}]}
    layout.install(lay, installer_copy=True, manifest=True, manifest_data=extra)
    mpath = dest / "usr/local/share/rehome/install-manifest.json"
    m = json.loads(mpath.read_text())
    assert m["app"] == "rehome" and m["version"] == rehome.__version__
    assert m["prefix"] == "/usr/local" and m["libexecdir"] == "/usr/local/libexec/rehome"
    assert m["packages"]["installed"] == ["qemu-utils"] and m["groups"][0]["user"] == "alice"
    assert "/usr/local/share/rehome/install-manifest.json" in m["files"]
    assert "/usr/local/share/rehome/installer/install.sh" in m["files"]
    assert "/usr/local/share/metainfo" in m["directories"]
    for f in m["files"]:
        assert (dest / f.lstrip("/")).is_file(), f

    # "Update": new version, one data file gone, one new; the old one is pruned.
    (src / "rehome/__init__.py").write_text(
        (src / "rehome/__init__.py").read_text().replace(rehome.__version__, "99.0.0"))
    profile = next((src / "data/profiles").glob("*.toml"))
    profile.unlink()
    (src / "data/profiles/zz-new.toml").write_text("# new\n")
    res = layout.install(lay, installer_copy=True, manifest=True, manifest_data=extra)
    assert f"/usr/local/share/rehome/profiles/{profile.name}" in res["removed_stale"]
    assert not (dest / f"usr/local/share/rehome/profiles/{profile.name}").exists()
    m2 = json.loads(mpath.read_text())
    assert m2["version"] == "99.0.0" and m2["installed_at"] == m["installed_at"]
    assert "/usr/local/share/metainfo" in m2["directories"]

    # Bytecode written next to the sources (e.g. run as root without the launcher).
    pyc = dest / "usr/local/lib/rehome/rehome/__pycache__/__init__.cpython-312.pyc"
    pyc.parent.mkdir()
    pyc.write_bytes(b"x")
    removed = layout.uninstall(lay, json.loads(mpath.read_text()))
    assert "/usr/local/bin/rehome" in removed
    assert list(dest.rglob("*")) == [], "uninstall left files or directories behind"


def test_uninstall_refuses_paths_outside_rehome(tmp_path):
    dest = tmp_path / "root"
    lay = layout.Layout(destdir=str(dest))
    victim = dest / "etc/passwd"
    victim.parent.mkdir(parents=True)
    victim.write_text("root:x:0:0\n")
    layout.uninstall(lay, {"app": "rehome", "files": ["/etc/passwd", "/usr/local/bin/../../../etc/passwd"]})
    assert victim.exists()


def test_uninstall_without_manifest(tmp_path):
    dest = tmp_path / "root"
    lay = layout.Layout(prefix="/usr", destdir=str(dest))
    layout.install(lay)
    stray = dest / "usr/share/rehome/profiles/user-added-by-old-version.toml"
    stray.write_text("")
    layout.uninstall(lay)
    left = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    assert left == []


def test_cli_list_and_deps():
    def run(*args):
        return subprocess.run([sys.executable, str(ROOT / "packaging/layout.py"), *args],
                              capture_output=True, text=True, check=True).stdout
    assert run("version").strip() == rehome.__version__
    listed = run("list", "--prefix", "/usr").split()
    assert listed == layout.Layout(prefix="/usr").files()
    req = run("deps", "--family", "debian").split()
    opt = run("deps", "--family", "debian", "--optional").split()
    assert "qemu-system-x86" in req and "python3-gi" in req
    assert "virt-v2v" in opt and not set(req) & set(opt)
    assert run("deps", "--family", "arch", "--format", "deb").strip().startswith("qemu-desktop, ")
    tsv = [line.split("\t") for line in run("deps", "--family", "fedora", "--all", "--format", "tsv").splitlines()]
    assert {row[0] for row in tsv} == set(layout.load_components(ROOT))
    assert all(len(row) == 4 for row in tsv)


# --- packaging files stay in sync ------------------------------------------------------

def _bash_array(text, name):
    m = re.search(rf"^{name}=\((.*?)^\)", text, re.S | re.M)
    assert m, name
    return re.findall(r"'([^']*)'", m.group(1))


def _expected(family):
    comps = layout.load_components(ROOT)
    req = [p for c in comps.values() if c.get("required") for p in c.get(family, [])]
    opt = [p for c in comps.values() if not c.get("required") for p in c.get(family, [])]
    return list(dict.fromkeys(req)), list(dict.fromkeys(opt))


def test_pkgbuild_matches_packages_toml():
    text = (ROOT / "packaging/arch/PKGBUILD").read_text()
    assert re.search(rf"^pkgver={re.escape(rehome.__version__)}$", text, re.M)
    req, opt = _expected("arch")
    depends = [d.split(">")[0].split("=")[0] for d in _bash_array(text, "depends")]
    assert depends == req
    assert [o.split(":")[0] for o in _bash_array(text, "optdepends")] == opt
    srcinfo = (ROOT / "packaging/arch/.SRCINFO").read_text()
    assert f"\tpkgver = {rehome.__version__}" in srcinfo
    assert [ln.split(" = ", 1)[1] for ln in srcinfo.splitlines() if ln.startswith("\tdepends = ")] == \
        _bash_array(text, "depends")


def _spec_requires(text, branch):
    m = re.search(r"^%if 0%\{\?suse_version\}\n(.*?)^%else\n(.*?)^%endif", text, re.S | re.M)
    assert m
    block = m.group(1 if branch == "suse" else 2)
    req = [ln.split(":", 1)[1].split()[0] for ln in block.splitlines() if ln.startswith("Requires:")]
    rec = [ln.split(":", 1)[1].split()[0] for ln in block.splitlines() if ln.startswith("Recommends:")]
    return req, rec


@pytest.mark.parametrize("family", ["fedora", "suse"])
def test_rpm_spec_matches_packages_toml(family):
    text = (ROOT / "packaging/rpm/rehome.spec").read_text()
    assert re.search(rf"^Version:\s+{re.escape(rehome.__version__)}$", text, re.M)
    req, rec = _spec_requires(text, family)
    exp_req, exp_opt = _expected(family)
    assert req == exp_req
    assert rec == exp_opt


def test_install_sh_detects_families(tmp_path):
    cases = {
        ("arch", ""): "arch", ("manjaro", "arch"): "arch", ("endeavouros", "arch"): "arch",
        ("garuda", "arch"): "arch", ("cachyos", "arch"): "arch",
        ("debian", ""): "debian", ("ubuntu", "debian"): "debian",
        ("linuxmint", "ubuntu debian"): "debian", ("pop", "ubuntu debian"): "debian",
        ("fedora", ""): "fedora", ("rocky", "rhel centos fedora"): "fedora",
        ("opensuse-tumbleweed", "opensuse suse"): "suse", ("opensuse-leap", "suse opensuse"): "suse",
        ("sles", ""): "suse", ("gentoo", ""): "unknown", ("nixos", ""): "unknown",
    }
    osr = tmp_path / "os-release"
    for (osid, like), fam in cases.items():
        osr.write_text(f'NAME="X"\nID="{osid}"\nID_LIKE="{like}"\nPRETTY_NAME="Test {osid}"\n')
        out = subprocess.run(["bash", str(ROOT / "install.sh"), "--detect"], capture_output=True,
                             text=True, check=True, env=dict(os.environ, REHOME_OS_RELEASE=str(osr)))
        assert f"family={fam}\n" in out.stdout, (osid, out.stdout)


def test_install_sh_syntax_and_help():
    subprocess.run(["bash", "-n", str(ROOT / "install.sh")], check=True)
    out = subprocess.run(["bash", str(ROOT / "install.sh"), "--help"], capture_output=True,
                         text=True, check=True).stdout
    for opt in ("--update", "--uninstall", "--purge", "--yes", "--dry-run", "--with-optional",
                "--skip-deps", "--keep-deps", "REHOME_SOURCE"):
        assert opt in out
