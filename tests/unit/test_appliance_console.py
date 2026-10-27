"""The appliance's start screen and maintenance console (docs/APPLIANCE.md, D42)."""

import shutil
import types
from pathlib import Path

import pytest

from rehome.appliance import build, console, maintenance, runtime
from rehome.errors import RehomeError
from rehome.util import toml


def _kiosk(monkeypatch, results, settings=None, keys=None, conf=None):
    """Run kiosk() with ``results`` as the sessions' outcomes; returns (said, intent)."""
    cfg = types.SimpleNamespace(name="Old PC", machine=types.SimpleNamespace(memory_mib=512, cpus=1))
    said, intent, waits = [], {}, []
    results = iter(results)
    keys = iter(keys or [])
    monkeypatch.setattr(runtime, "load_config", lambda: conf or {"kiosk": {"countdown": 3}})
    monkeypatch.setattr(runtime, "load_settings", lambda: settings or {})
    monkeypatch.setattr(runtime, "the_vm", lambda: cfg)
    monkeypatch.setattr(runtime, "cmdline", lambda text=None: {})
    monkeypatch.setattr(runtime, "_disk_numbers", lambda c: (10 * 2**30, 0))
    monkeypatch.setattr(runtime, "_say", lambda lines: said.append(lines))

    def wait_key(seconds):
        waits.append(seconds)
        return next(keys, "")
    monkeypatch.setattr(runtime, "_wait_key", wait_key)
    monkeypatch.setattr(runtime, "_write_json",
                        lambda path, data: intent.update(data) if path == runtime.INTENT else None)
    monkeypatch.setattr(runtime, "_read_json", lambda path: next(results))
    monkeypatch.setattr(runtime.hardware, "gather", lambda: None)
    monkeypatch.setattr(runtime.hardware, "start_checks",
                        lambda *a, **k: types.SimpleNamespace(notes=[]))
    monkeypatch.setattr(runtime.hardware, "has_gpu_acceleration", lambda: True)
    monkeypatch.setattr(runtime.hardware, "screen_size", lambda: (1920, 1080))
    monkeypatch.setattr(runtime.subprocess, "run",
                        lambda argv, env=None, check=False: types.SimpleNamespace(returncode=0))
    assert runtime.kiosk() == 0
    return said, intent, waits


def test_the_start_screen_does_not_mention_the_maintenance_console(monkeypatch):
    said, intent, waits = _kiosk(monkeypatch, [{"outcome": "poweroff", "reason": "guest-shutdown"}])
    assert said[0] == ["Starting Old PC..."]
    assert not any("maintenance" in line.lower() or "press" in line.lower()
                   for lines in said for line in lines)
    assert waits == [3]
    assert intent["outcome"] == "poweroff"


def test_m_still_opens_the_maintenance_console(monkeypatch):
    said, intent, _ = _kiosk(monkeypatch, [], keys=["m"])
    assert intent == {"outcome": "maintenance"}
    assert said[-1] == ["Opening the maintenance console..."]


def test_the_wait_comes_from_the_console_settings(monkeypatch):
    _, _, waits = _kiosk(monkeypatch, [{"outcome": "poweroff"}],
                         settings={"startup": {"delay": 10}})
    assert waits == [10]


@pytest.mark.parametrize("then, outcome", [("poweroff", "poweroff"),
                                           ("maintenance", "maintenance")])
def test_what_happens_when_the_vm_shuts_down(monkeypatch, then, outcome):
    said, intent, _ = _kiosk(monkeypatch, [{"outcome": "poweroff", "reason": "guest-shutdown"}],
                             settings={"startup": {"when_vm_shuts_down": then}})
    assert intent["outcome"] == outcome
    if outcome == "maintenance":
        assert intent["message"] == "Old PC was shut down."


def test_a_vm_that_shuts_down_can_start_again_but_the_power_button_turns_off(monkeypatch):
    said, intent, waits = _kiosk(
        monkeypatch,
        [{"outcome": "poweroff", "reason": "guest-shutdown"},
         {"outcome": "poweroff", "reason": "guest-shutdown", "power_button": True}],
        settings={"startup": {"when_vm_shuts_down": "restart", "delay": 2}})
    assert waits == [2, 2]                 # "Starting Old PC..." again, M possible again
    assert [lines[0] for lines in said].count("Starting Old PC...") >= 2
    assert intent["outcome"] == "poweroff"


def test_m_after_the_vm_shut_down_and_starts_again(monkeypatch):
    _, intent, _ = _kiosk(monkeypatch, [{"outcome": "poweroff", "reason": "guest-shutdown"}],
                          settings={"startup": {"when_vm_shuts_down": "restart"}},
                          keys=["", "m"])
    assert intent == {"outcome": "maintenance"}


def test_startup_settings_are_checked():
    conf = {"kiosk": {"countdown": 5}}
    assert runtime.startup(conf, {}) == {"delay": 5, "open": "vm", "when_vm_shuts_down": "poweroff"}
    bad = {"startup": {"delay": 999, "open": "elsewhere", "when_vm_shuts_down": "explode"}}
    assert runtime.startup(conf, bad) == {"delay": runtime.MAX_DELAY, "open": "vm",
                                          "when_vm_shuts_down": "poweroff"}
    assert runtime.startup(conf, {"startup": {"delay": "x"}})["delay"] == 3


def test_opengl_from_the_kernel_command_line_or_the_settings():
    assert runtime.opengl({}, args={})
    assert not runtime.opengl({"screen": {"gl": False}}, args={})
    assert runtime.opengl({"screen": {"gl": False}}, args={"rehome.gl": "on"})
    assert not runtime.opengl({}, args={"rehome.gl": "off"})


def test_power_on_can_open_the_maintenance_console(monkeypatch):
    started = []
    monkeypatch.setattr(runtime, "cmdline", lambda text=None: {"rehome.mode": "vm"})
    monkeypatch.setattr(runtime, "load_config", lambda: {})
    monkeypatch.setattr(runtime, "clear_recordfail", lambda: None)
    monkeypatch.setattr(runtime.subprocess, "run",
                        lambda argv, check=False: started.append(argv[-1]) or
                        types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(runtime, "load_settings", lambda: {})
    runtime.boot()
    monkeypatch.setattr(runtime, "load_settings", lambda: {"startup": {"open": "maintenance"}})
    runtime.boot()
    assert started == ["rehome-kiosk.service", "rehome-maintenance.service"]


def test_grub_takes_a_lasting_default_from_its_environment():
    cfg = build.grub_cfg("uuid", "XP", "")
    assert cfg.index('set default="${rehome_default}"') < cfg.index('set default="${next_entry}"')


def test_the_main_menu_fits_a_text_screen(monkeypatch, capsys):
    lines = console.menu_lines([(k, t) for k, t, _f in console.MENU])
    assert max(len(line) for line in lines) <= 79
    assert len(lines) == (len(console.MENU) + 1) // 2
    monkeypatch.setattr(console, "_out", lambda cmd: "")
    monkeypatch.setattr(console, "_time_synced", lambda: True)
    monkeypatch.setattr(console, "_storage_summary", lambda: "10 GiB free of 20 GiB")
    monkeypatch.setattr(runtime, "MESSAGE", Path("/nonexistent"))
    console.header({"appliance": {"built": "2026-10-24", "rehome_version": "0.3.12",
                                  "system": "Ubuntu"}}, "Windows 98", "stopped")
    out = capsys.readouterr().out.split("\n")
    # header + menu + "Choose:" (with a 4-line message on top) within 25 lines
    assert len(out) + len(lines) + 2 + 5 <= 25


class Script:
    """Answers for the console's questions, and the commands it runs."""

    def __init__(self, monkeypatch, answers, out=None):
        self.answers = list(answers)
        self.ran = []
        monkeypatch.setattr(console, "_ask", self.ask)
        monkeypatch.setattr(console, "_secret", self.ask)
        monkeypatch.setattr(console, "_pause", lambda text="": None)
        monkeypatch.setattr(console, "_clear", lambda: None)
        monkeypatch.setattr(console, "_run", lambda cmd, **kw: self.ran.append(cmd) or 0)
        monkeypatch.setattr(console, "_out", out or (lambda cmd: ""))

    def ask(self, prompt):
        return self.answers.pop(0) if self.answers else ""


def test_startup_and_shutdown_page(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(runtime, "load_settings", lambda: dict(saved))
    monkeypatch.setattr(console, "_save_settings", lambda s: saved.update(s))
    Script(monkeypatch, ["1", "2", "12", "3", "2", "2", "99", ""])
    console.startup_and_shutdown({"appliance": {"name": "Windows 98"}, "kiosk": {"countdown": 3}})
    assert saved["startup"] == {"open": "maintenance", "delay": 12,
                                "when_vm_shuts_down": "restart"}


def test_checkpoints_are_chosen_by_number(monkeypatch):
    listing = ('{"current": "b2", "checkpoints": [{"id": "a1", "name": "Fresh", "created": '
               '"2026-10-01T09:00:00"}, {"id": "b2", "name": "Drivers", "created": ""}]}')
    s = Script(monkeypatch, ["G", "2", "yes", "D", "1", "no", ""], out=lambda cmd: listing)
    console.checkpoints({"appliance": {"vm": "vm-id"}})
    assert s.ran == [[console.REHOME, "checkpoint", "revert", "vm-id", "b2"]]


def test_screen_page_sets_graphics_and_opengl(monkeypatch):
    saved = {}
    env = {"rehome_default": ""}
    monkeypatch.setattr(runtime, "load_settings", lambda: dict(saved))
    monkeypatch.setattr(console, "_save_settings", lambda s: saved.update(s))
    s = Script(monkeypatch, ["1", "2", ""],
               out=lambda cmd: "".join(f"{k}={v}\n" for k, v in env.items() if v))
    console.screen({})
    assert s.ran == [["grub-editenv", runtime.GRUBENV, "set", "rehome_default=vm-basic"]]
    assert saved["screen"] == {"gl": False}


def test_screen_page_sets_the_touchpad(monkeypatch):
    saved = {}
    monkeypatch.setattr(runtime, "load_settings", lambda: dict(saved))
    monkeypatch.setattr(console, "_save_settings", lambda s: saved.update(s))
    Script(monkeypatch, ["4", "warp", "4", "Slow", "5", ""])
    console.screen({})
    assert saved["touchpad"] == {"speed": "slow", "tap": False}


def test_the_clock_is_set_by_hand(monkeypatch):
    s = Script(monkeypatch, ["2026-02-30 10:00", "", "2026-10-24 14:05"])
    console.set_time()                               # not a date: nothing is run
    console.set_time()                               # Enter: keep
    assert s.ran == []
    console.set_time()
    assert s.ran == [["timedatectl", "set-ntp", "false"],
                     ["timedatectl", "set-time", "2026-10-24 14:05:00"]]


def test_memory_and_processors(monkeypatch):
    cfg = types.SimpleNamespace(id="vm-id", name="Windows 98", os=types.SimpleNamespace(family="win9x"),
                                machine=types.SimpleNamespace(memory_mib=256, cpus=1), cdroms=[])
    report = types.SimpleNamespace(memory_mib=8192, cpus=4)
    monkeypatch.setattr(console, "_the_vm", lambda conf: cfg)
    monkeypatch.setattr(console.hardware, "gather", lambda: report)
    s = Script(monkeypatch, ["1", "16384", "1", "512", "2", "9", "2", "2", ""])
    console.vm_settings({})
    assert s.ran == [[console.REHOME, "set", "vm-id", "--memory", "512"],
                     [console.REHOME, "set", "vm-id", "--cpus", "2"]]


# --- maintenance.py: real files, real qemu-img ----------------------------------------------------

@pytest.fixture
def appliance_vm(tmp_path, monkeypatch):
    """A VM as the appliance has it, with a CD image in its media folder."""
    if not shutil.which("qemu-img"):
        pytest.skip("qemu-img is not installed")
    from rehome.vm import create, disks, library
    raw = tmp_path / "disk.raw"
    with open(raw, "wb") as f:                        # something to find after a restore
        f.truncate(64 * 2**20)
        f.seek(3 * 2**20)
        f.write(b"before the backup")
    img = tmp_path / "disk.qcow2"
    disks.convert(str(raw), str(img), src_fmt="raw")
    cfg = create.create(create.NewVMSpec(name="Windows 98", os="win98", existing_disk=str(img),
                                         network="nat"))
    (cfg.dir / "media").mkdir()
    (cfg.dir / "media" / "drivers.iso").write_bytes(b"CD001" * 1000)
    if not cfg.cdroms:
        from rehome.vm.config import CdRom
        cfg.cdroms = [CdRom(id="cd0")]
    runtime.fit_vm(cfg, media={cfg.cdroms[0].id: "media/drivers.iso"})
    for d in cfg.disks:
        if not cfg.resolve(d.file).exists():
            disks.convert(str(img), str(cfg.resolve(d.file)), src_fmt="qcow2")
    library.save(cfg)
    monkeypatch.setattr(maintenance, "give_to_owner", lambda path: None)
    conf_path = tmp_path / "appliance.toml"
    conf = {"appliance": {"vm": cfg.id, "name": cfg.name}, "network": {"mode": "nat"}}
    toml.dump_atomic(conf, conf_path)
    return library.find(cfg.id), conf, conf_path


def test_backup_and_restore_replace_the_vm(appliance_vm, tmp_path):
    from rehome.vm import disks, library
    cfg, conf, conf_path = appliance_vm
    drive = tmp_path / "usb"
    drive.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    done = []
    path = maintenance.backup(cfg, drive, "exfat", progress=done.append, work_dir=work)
    assert path.parent == drive / maintenance.BACKUP_DIR and path.suffix == ".rehome"
    assert done and done[-1] == pytest.approx(1.0)
    assert list(work.iterdir()) == []                 # the converted disks are gone
    assert maintenance.backups_on(drive) == [path]
    info = maintenance.backup_info(path)
    assert info["name"] == "Windows 98" and info["bytes"] > 0

    conf_path.chmod(0o644)                            # as the build writes it
    restored = maintenance.restore(path, conf, config_path=conf_path)
    # The kiosk reads it as the "rehome" user: rewriting it as root kept it readable.
    assert conf_path.stat().st_mode & 0o777 == 0o644
    assert restored.id != cfg.id and restored.name == "Windows 98"
    assert [v.id for v in library.list_vms()] == [restored.id]       # the old one is gone
    assert toml.load(conf_path)["appliance"] == {"vm": restored.id, "name": "Windows 98"}
    assert restored.display.console == "kiosk"
    assert restored.cdroms[0].file == "media/drivers.iso"            # kept from the appliance
    assert (restored.dir / "media" / "drivers.iso").read_bytes() == b"CD001" * 1000
    copy = restored.resolve(restored.disks[0].file)
    raw = tmp_path / "check.raw"
    disks.convert(str(copy), str(raw), src_fmt="qcow2", dst_fmt="raw")
    assert b"before the backup" in raw.read_bytes()


def test_a_backup_imported_on_a_computer_uses_rehomes_console(appliance_vm, tmp_path):
    from rehome.vm import lifecycle
    cfg, _conf, _ = appliance_vm
    assert cfg.display.console == "kiosk"
    path = maintenance.backup(cfg, tmp_path, "ntfs")
    other = lifecycle.import_bundle(str(path))
    assert other.display.console == "dbus"         # not a full screen window that cannot close


def test_backups_too_big_for_fat32_are_refused(appliance_vm, tmp_path, monkeypatch):
    cfg, _conf, _ = appliance_vm
    monkeypatch.setattr(maintenance, "backup_size", lambda c: 5 * 2**30)
    with pytest.raises(RehomeError, match="FAT32"):
        maintenance.backup(cfg, tmp_path, "vfat")
    monkeypatch.setattr(maintenance, "_free", lambda p: 2**30)
    with pytest.raises(RehomeError, match="free"):
        maintenance.backup(cfg, tmp_path, "exfat")


def test_a_damaged_backup_leaves_the_vm_alone(appliance_vm, tmp_path):
    from rehome.vm import library
    cfg, conf, conf_path = appliance_vm
    bad = tmp_path / "broken.rehome"
    bad.write_bytes(b"not a tar file")
    with pytest.raises(RehomeError):
        maintenance.restore(bad, conf, config_path=conf_path)
    assert [v.id for v in library.list_vms()] == [cfg.id]
    assert toml.load(conf_path)["appliance"]["vm"] == cfg.id


def test_disc_images_are_found_and_copied(appliance_vm, tmp_path):
    cfg, _conf, _ = appliance_vm
    drive = tmp_path / "stick"
    (drive / "Drivers" / "Sound").mkdir(parents=True)
    (drive / "WIN98SE.ISO").write_bytes(b"x" * 5000)
    (drive / "Drivers" / "vbe.iso").write_bytes(b"y" * 10)
    (drive / "Drivers" / "Sound" / "deep.iso").write_bytes(b"z")      # three levels down
    (drive / ".hidden.iso").write_bytes(b"h")
    (drive / "notes.txt").write_text("hi")
    found = maintenance.images_on(drive)
    assert [p.relative_to(drive).as_posix() for p in found] == ["Drivers/vbe.iso", "WIN98SE.ISO"]
    dest = maintenance.copy_image(drive / "WIN98SE.ISO", cfg)
    assert dest == cfg.dir / "media" / "WIN98SE.ISO" and dest.read_bytes() == b"x" * 5000
    assert maintenance.images_in_use(cfg) == {cfg.dir / "media" / "drivers.iso"}


def test_the_maintenance_password_is_set_and_removed(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(maintenance.subprocess, "run",
                        lambda argv, **kw: ran.append((argv, kw.get("input"))) or
                        types.SimpleNamespace(returncode=0, stderr=""))
    (tmp_path / "root").mkdir()
    conf, conf_path = {"appliance": {"vm": "x"}}, tmp_path / "appliance.toml"
    toml.dump_atomic(conf, conf_path)
    conf_path.chmod(0o644)
    maintenance.set_password("s3cret: yes", conf, root=tmp_path, config_path=conf_path)
    assert conf_path.stat().st_mode & 0o777 == 0o644
    assert (["chpasswd"], "root:s3cret: yes\n") in ran
    assert maintenance.has_password(tmp_path)
    unit = (tmp_path / runtime.PASSWORD_UNIT).read_text()
    assert "agetty -o '-p -- \\\\u'" in unit           # systemd turns \\ into one backslash
    assert "appliance console" in (tmp_path / runtime.ROOT_PROFILE).read_text()
    assert toml.load(conf_path)["appliance"]["password"] is True
    with pytest.raises(RehomeError):
        maintenance.set_password("", conf, root=tmp_path, config_path=conf_path)
    maintenance.remove_password(conf, root=tmp_path, config_path=conf_path)
    assert not maintenance.has_password(tmp_path)
    assert (["passwd", "-l", "root"], None) in ran
    assert toml.load(conf_path)["appliance"]["password"] is False


def test_the_build_writes_the_same_password_files(monkeypatch):
    written = {}

    class G:
        def write(self, path, data):
            written[path] = data

        def mkdir_p(self, path):
            pass

        def rm_rf(self, path):
            pass
    monkeypatch.setattr(build.image, "run", lambda g, script, what: None)
    build._configure(G(), build.Options(output=Path("x"), maintenance_password="pw"))
    assert written["/" + runtime.PASSWORD_UNIT] == runtime.PASSWORD_UNIT_TEXT.encode()
    assert written["/" + runtime.ROOT_PROFILE] == runtime.ROOT_PROFILE_TEXT.encode()


def test_a_page_that_fails_does_not_close_the_console(monkeypatch):
    """Regression: "Save diagnostics" with one USB stick ended the console (exit 70)."""
    answers = iter(["8", "x"])
    logged, paused = [], []

    def ask(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise SystemExit(0)

    def broken(conf):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(console, "_ask", ask)
    monkeypatch.setattr(console, "_pause", lambda text="": paused.append(text))
    monkeypatch.setattr(console, "header", lambda *a: None)
    monkeypatch.setattr(console, "_vm_state", lambda vm_id: "stopped")
    monkeypatch.setattr(runtime, "load_config", lambda: {"appliance": {"vm": "x"}})
    monkeypatch.setattr(console, "MENU", [("8", "Sound", broken)])
    import syslog
    monkeypatch.setattr(syslog, "syslog", lambda prio, msg: logged.append(msg))
    with pytest.raises(SystemExit):
        console.main()
    assert paused and "\"Sound\" stopped with an error" in paused[0]
    assert "Read-only file system" in paused[0]
    assert any("OSError" in line for line in logged)          # the traceback, for diagnostics


def test_a_read_only_usb_drive_is_refused_for_writing(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(console, "USB_MOUNT", str(tmp_path / "mnt"))
    monkeypatch.setattr(console, "_run", lambda cmd, **kw: ran.append(cmd) or 0)
    monkeypatch.setattr(console.os, "statvfs",
                        lambda path: types.SimpleNamespace(f_flag=console.os.ST_RDONLY))
    with pytest.raises(RehomeError, match="can only be read"):
        with console._mounted("/dev/sdz1"):
            pass
    assert ran[-1][0] == "umount"                              # and it is unmounted again
    with console._mounted("/dev/sdz1", readonly=True) as drive:  # reading is fine
        assert drive == tmp_path / "mnt"
