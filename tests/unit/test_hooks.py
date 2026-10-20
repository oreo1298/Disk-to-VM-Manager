import stat

import pytest

from rehome.errors import RehomeError
from rehome.vm import create, hooks, runner


def _hook(name, body):
    d = hooks.hooks_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


@pytest.fixture
def vm():
    return create.create(create.NewVMSpec(name="Hook test", os="freedos1.3", disk_size=0,
                                          network="none"))


def test_prepare_hook_adds_qemu_args(vm, tmp_path):
    _hook("10-args", 'echo "# a comment" >> "$REHOME_QEMU_ARGS_FILE"\n'
                     'echo "-device" >> "$REHOME_QEMU_ARGS_FILE"\n'
                     'echo "vfio-pci,host=0000:01:00.0" >> "$REHOME_QEMU_ARGS_FILE"\n'
                     '[ "$1" = prepare ] && [ "$REHOME_VM_NAME" = "Hook test" ]\n')
    args = hooks.run("prepare", vm, args_file=tmp_path / "args")
    assert args == ["-device", "vfio-pci,host=0000:01:00.0"]
    assert not (tmp_path / "args").exists()
    assert hooks.run("started", vm) == []


def test_failing_prepare_hook_blocks_start(vm):
    _hook("10-no", "echo nope >&2; exit 3\n")
    with pytest.raises(RehomeError, match="stopped the VM from starting") as e:
        hooks.run("prepare", vm)
    assert "nope" in e.value.details


def test_stopped_hook_runs_when_start_fails_after_prepare(vm, tmp_path, monkeypatch):
    marker = tmp_path / "phases"
    _hook("10-log", f'echo "$1" >> {marker}\n')

    def broken_build(*a, **k):
        raise RehomeError("simulated failure")
    monkeypatch.setattr(runner, "build", broken_build)
    with pytest.raises(RehomeError, match="simulated"):
        runner.start(vm, headless=True)
    assert marker.read_text().split() == ["prepare", "stopped"]
