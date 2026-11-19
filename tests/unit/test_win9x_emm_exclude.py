"""SYSTEM.INI's [386Enh] EMMExclude for the BIOS area (win9x.emm_exclude, D53)."""

import contextlib

import pytest

from rehome.patch import legacy

INI = "/WINDOWS/SYSTEM.INI"


class FakeGuest:
    def __init__(self, text: bytes):
        self.files = {INI: text}

    def read_file(self, path):
        return self.files[path]

    def write(self, path, data):
        self.files[path] = data


class Ctx:
    def __init__(self, text: bytes):
        self.g = FakeGuest(text)
        self.root = "/dev/sda1"
        self.os = type("OS", (), {"details": {"windir": "/WINDOWS"}})()


@pytest.fixture(autouse=True)
def no_guestfs(monkeypatch):
    monkeypatch.setattr(legacy, "mounted", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(legacy, "ci_path", lambda g, p: p if p in g.files else None)


@pytest.mark.parametrize(("before", "after"), [
    (b"[boot]\r\nshell=Explorer.exe\r\n[386Enh]\r\ndevice=*vmm\r\n",
     b"[boot]\r\nshell=Explorer.exe\r\n[386Enh]\r\nEMMExclude=C000-EFFF\r\ndevice=*vmm\r\n"),
    (b"[boot]\r\nshell=Explorer.exe\r\n[386Enh]",                    # header, no line break
     b"[boot]\r\nshell=Explorer.exe\r\n[386Enh]\r\nEMMExclude=C000-EFFF\r\n"),
    (b"[boot]\r\nshell=Explorer.exe\r\n",                            # no [386Enh] at all
     b"[boot]\r\nshell=Explorer.exe\r\n\r\n[386Enh]\r\nEMMExclude=C000-EFFF\r\n"),
    (b"[386enh]\ndevice=*vmm\n",                                     # any case, LF only
     b"[386enh]\nEMMExclude=C000-EFFF\r\ndevice=*vmm\n"),
    (b"[386Enh]\r\nEMMExclude=E000-EFFF\r\n",                        # other ranges stay
     b"[386Enh]\r\nEMMExclude=C000-EFFF\r\nEMMExclude=E000-EFFF\r\n"),
    (b"[386Enh]\r\n; caf\xe9\r\n",                                   # bytes kept as they are
     b"[386Enh]\r\nEMMExclude=C000-EFFF\r\n; caf\xe9\r\n"),
])
def test_the_bios_area_is_excluded(before, after):
    ctx = Ctx(before)
    result = legacy.emm_exclude(ctx)
    assert result.status == "applied" and ctx.g.files[INI] == after
    assert legacy.emm_exclude(ctx).status == "not-needed"            # once only
    assert ctx.g.files[INI] == after


def test_an_existing_exclusion_is_recognised():
    ctx = Ctx(b"[386Enh]\r\n  emmexclude = c000-efff \r\n")
    assert legacy.emm_exclude(ctx).status == "not-needed"
    assert ctx.g.files[INI] == b"[386Enh]\r\n  emmexclude = c000-efff \r\n"


def test_no_system_ini_is_skipped():
    ctx = Ctx(b"")
    ctx.g.files.clear()
    assert legacy.emm_exclude(ctx).status == "skipped"
