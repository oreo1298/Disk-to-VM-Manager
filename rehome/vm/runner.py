"""Start, stop and observe VMs.

QEMU runs detached in its own session so VMs keep running when the GUI or
CLI exits. State lives in the VM's runtime directory; any Rehome process can
rediscover a running VM from its pidfile and QMP socket.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from rehome import paths
from rehome.errors import RehomeError
from rehome.host import info as host_info
from rehome.host import qemu as qemu_mod
from rehome.host import tools as tools_mod
from rehome.util.proc import quote, run as run_cmd, which
from rehome.vm import hooks
from rehome.vm.config import VMConfig
from rehome.vm.qemu_cmd import LaunchOptions, RuntimePaths, build
from rehome.vm.qmp import QMP, QmpClosed

log = logging.getLogger(__name__)

STOPPED, RUNNING, PAUSED, SAVED, STARTING, CRASHED = (
    "stopped", "running", "paused", "saved", "starting", "crashed")


@dataclass
class VMState:
    status: str
    pid: int = 0
    started_at: float = 0.0
    qemu_status: str = ""        # raw QMP status (running, paused, inmigrate, ...)
    console: str = ""

    @property
    def active(self) -> bool:
        return self.status in (RUNNING, PAUSED, STARTING)


def runtime(cfg: VMConfig) -> RuntimePaths:
    return RuntimePaths(paths.vm_runtime_dir(cfg.id))


def saved_state_path(cfg: VMConfig) -> Path:
    return cfg.dir / "saved.vmstate"


def _state_file(cfg: VMConfig) -> Path:
    return runtime(cfg).run / "state.json"


def _read_pid(pidfile: str) -> int:
    try:
        return int(Path(pidfile).read_text().strip())
    except (OSError, ValueError):
        return 0


def _pid_is_vm(pid: int, vm_id: str) -> bool:
    if pid <= 0:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    try:
        i = cmdline.index(b"-uuid")
        return cmdline[i + 1].decode() == vm_id
    except (ValueError, IndexError):
        return False


def qemu_pid(cfg: VMConfig) -> int:
    pid = _read_pid(runtime(cfg).pidfile)
    return pid if _pid_is_vm(pid, cfg.id) else 0


def is_running(cfg: VMConfig) -> bool:
    return qemu_pid(cfg) > 0


def state(cfg: VMConfig, timeout: float = 2.0) -> VMState:
    pid = qemu_pid(cfg)
    if not pid:
        if saved_state_path(cfg).exists():
            return VMState(SAVED)
        return VMState(STOPPED)
    meta = {}
    try:
        meta = json.loads(_state_file(cfg).read_text())
    except (OSError, ValueError):
        pass
    st = VMState(RUNNING, pid=pid, started_at=meta.get("started_at", 0.0),
                 console=meta.get("console", ""))
    try:
        with QMP(runtime(cfg).qmp, timeout=timeout) as q:
            qs = q.status()
    except Exception:
        return st
    st.qemu_status = qs
    if qs in ("paused", "suspended", "prelaunch", "postmigrate", "save-vm", "restore-vm"):
        st.status = PAUSED
    elif qs in ("inmigrate", "finish-migrate"):
        st.status = STARTING
    elif qs in ("guest-panicked", "internal-error", "io-error"):
        st.status = CRASHED
    return st


def qmp(cfg: VMConfig, timeout: float = 30.0) -> QMP:
    if not is_running(cfg):
        raise RehomeError(f"\"{cfg.name}\" is not running.", code="not-running")
    return QMP(runtime(cfg).qmp, timeout=timeout).connect(retries=2.0)


# --- preparation ---------------------------------------------------------------

def _prepare_nvram(cfg: VMConfig, fw) -> tuple[str, str]:
    """Create the VM's UEFI variable store from the firmware's template."""
    meta_file = cfg.dir / "nvram.json"
    nvram = cfg.dir / "nvram.qcow2"
    if nvram.exists():
        return str(nvram), "qcow2"
    if not fw.nvram_template:
        return "", "raw"
    qemu_img = qemu_mod.qemu_img()
    if not qemu_img:
        raise RehomeError("qemu-img is missing.", fix="Install the QEMU tools.")
    run_cmd([qemu_img, "convert", "-f", fw.nvram_format, "-O", "qcow2",
             fw.nvram_template, str(nvram)], what="Creating the UEFI variable store")
    meta_file.write_text(json.dumps({"code": fw.executable, "template": fw.nvram_template,
                                     "descriptor": fw.path}, indent=2))
    return str(nvram), "qcow2"


def _pinned_firmware(cfg: VMConfig, host, arch: str):
    """Keep using the firmware the variable store was created for, if still installed."""
    want_sb = cfg.machine.firmware == "uefi-secure"
    meta_file = cfg.dir / "nvram.json"
    candidates = host.firmware(arch)
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            for fw in candidates:
                if fw.executable == meta.get("code") and fw.secure_boot == want_sb:
                    return fw
        except (OSError, ValueError):
            pass
    fw = host_info.choose_firmware(host, arch, cfg.machine.type, uefi=True,
                                   secure_boot=want_sb, enrolled_keys=want_sb)
    if fw is None and want_sb:
        raise RehomeError("Secure Boot firmware is not installed.",
                          fix="Install the full OVMF/edk2 package (run `rehome doctor`).")
    return fw


def prepare_tpm(cfg: VMConfig) -> None:
    state_dir = cfg.dir / "tpm"
    if cfg.machine.tpm == "none" or (state_dir.exists() and any(state_dir.iterdir())):
        return
    setup = which("swtpm_setup")
    if not setup:
        raise RehomeError("This VM has a TPM, but swtpm_setup is not installed.",
                          fix="Install the swtpm tools (run `rehome doctor`).")
    state_dir.mkdir(mode=0o700, exist_ok=True)
    env = {"XDG_CONFIG_HOME": str(paths.ensure(paths.config_dir() / "swtpm"))}
    tpm2 = ["--tpm2"] if cfg.machine.tpm == "2.0" else []
    # Prefer a TPM with EK and platform certificates (what Windows expects of
    # real hardware); fall back to a plain TPM if the local CA can't be set up.
    run_cmd([setup, "--create-config-files", "skip-if-exist"], check=False, env=env)
    attempts = [
        [setup, *tpm2, "--tpmstate", str(state_dir), "--create-ek-cert",
         "--create-platform-cert", "--lock-nvram", "--not-overwrite"],
        [setup, *tpm2, "--tpmstate", str(state_dir), "--createek", "--lock-nvram",
         "--not-overwrite"],
    ]
    last = None
    for argv in attempts:
        cp = run_cmd(argv, check=False, env=env, timeout=120)
        if cp.returncode == 0:
            return
        last = cp
    raise RehomeError("The virtual TPM could not be initialised.",
                      details=f"$ {quote(attempts[-1])}\n{last.stderr if last else ''}")


def _audio_backend(cfg: VMConfig, caps) -> str:
    want = cfg.sound.backend
    if cfg.sound.device == "none":
        return "none"
    if want != "auto":
        return want if want in caps.audiodevs or not caps.audiodevs else "none"
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if "pipewire" in caps.audiodevs and xdg and os.path.exists(os.path.join(xdg, "pipewire-0")):
        return "pipewire"
    if "pa" in caps.audiodevs and xdg and os.path.exists(os.path.join(xdg, "pulse", "native")):
        return "pa"
    if "alsa" in caps.audiodevs and os.path.exists("/dev/snd"):
        return "alsa"
    return "none"


def running_vms_memory(exclude: str = "") -> int:
    from rehome.vm import library
    total = 0
    for v in library.list_vms():
        if v.id != exclude and is_running(v):
            total += v.machine.memory_mib
    return total


def check_resources(cfg: VMConfig, host, allow_overcommit: bool = False) -> None:
    m = cfg.machine
    if m.cpus > host.threads:
        raise RehomeError(
            f"\"{cfg.name}\" is set to {m.cpus} virtual CPUs, but this computer has only "
            f"{host.threads}.",
            fix=f"Lower the CPU count to {host.threads} or less in the VM settings.")
    if allow_overcommit or host.memory.total_mib == 0:
        return
    used = running_vms_memory(exclude=cfg.id)
    limit = host.memory.total_mib - host.memory.host_reserve_mib
    if m.memory_mib + used > limit:
        free_for_vm = max(0, limit - used)
        raise RehomeError(
            f"Starting \"{cfg.name}\" with {m.memory_mib} MiB would leave too little memory for "
            f"this computer ({host.memory.total_mib} MiB total, {used} MiB already used by "
            f"running VMs, {host.memory.host_reserve_mib} MiB kept for the host).",
            fix=f"Give the VM at most {free_for_vm} MiB, shut down another VM, or start it "
                "anyway with --allow-overcommit.", code="overcommit")


# --- start -------------------------------------------------------------------------

def start(cfg: VMConfig, *, headless: bool = False, paused: bool = False,
          restore: bool = True, loadvm: str = "", allow_overcommit: bool = False,
          extra_args: list[str] | None = None, accel: str | None = None,
          fd_provider=None) -> VMState:
    """Start a VM and return once QEMU is up and answering QMP."""
    if is_running(cfg):
        raise RehomeError(f"\"{cfg.name}\" is already running.", code="already-running")
    host = host_info.gather()
    arch = cfg.machine.arch
    caps = host.caps(arch)
    if caps is None:
        raise RehomeError(f"No QEMU emulator for {arch} guests is installed.",
                          fix="Install QEMU (run `rehome doctor`).")
    if caps.probe_error:
        raise RehomeError("QEMU could not be started to check its features.",
                          details=caps.probe_error)
    check_resources(cfg, host, allow_overcommit)
    want = accel or cfg.machine.accel
    choice = host_info.choose_accel(host, arch, prefer=want if want in ("kvm", "tcg") else "auto",
                                    prefer_reason=cfg.machine.accel_reason)
    if want == "kvm" and choice.accel != "kvm":
        raise RehomeError("This VM is set to require KVM, but KVM is not available.",
                          fix=choice.reason + " Run `rehome doctor` for details.")
    rt = runtime(cfg)
    for stale in rt.run.glob("*.sock"):
        stale.unlink(missing_ok=True)
    log_dir = paths.ensure(cfg.dir / "logs")
    opts = LaunchOptions(accel=choice.accel, headless=headless, paused=paused, loadvm=loadvm,
                         log_dir=log_dir, extra_args=list(extra_args or []))
    fw_arch = "x86_64" if arch in ("i386", "i686") else arch
    if cfg.machine.firmware != "bios":
        fw = _pinned_firmware(cfg, host, fw_arch)
        opts.firmware = fw
        if fw:
            opts.nvram_path, opts.nvram_format = _prepare_nvram(cfg, fw)
    if cfg.machine.tpm != "none":
        prepare_tpm(cfg)
        opts.swtpm = which("swtpm")
    if cfg.shares:
        opts.virtiofsd = tools_mod.virtiofsd_path()
    opts.passt = which("passt") if os.environ.get("REHOME_NO_PASST") != "1" else None
    opts.audio_backend = _audio_backend(cfg, caps)
    saved = saved_state_path(cfg)
    if restore and saved.exists():
        opts.incoming = str(saved)
    # Physical-disk sources are opened by the privileged helper; the fds are
    # inherited by QEMU and referenced as /dev/fdset/N.
    pass_fds: list[int] = []
    fd_args: list[str] = []
    opened: list[int] = []
    prepared = False
    proc = None
    try:
        block_disks = [d for d in cfg.disks
                       if d.source and d.source.mode == "linked" and d.source.kind == "block"]
        if block_disks:
            if fd_provider is None:
                from rehome.disk import privileged
                fd_provider = privileged.open_disk_readonly
            for i, d in enumerate(block_disks, start=1):
                fd = fd_provider(d.source.path)
                opened.append(fd)
                os.set_inheritable(fd, True)
                pass_fds.append(fd)
                fd_args += ["-add-fd", f"fd={fd},set={i},opaque={d.id}"]
                opts.fdsets[d.id] = i
        ewf_disks = [d for d in cfg.disks
                     if d.source and d.source.mode == "linked" and d.source.kind == "ewf"]
        if ewf_disks:
            from rehome.disk import ewf
            for d in ewf_disks:
                opts.source_paths[d.id] = ewf.ensure_mounted(d.source.path)
        # A failing prepare hook stops the start before anything ran.
        hook_args = hooks.run("prepare", cfg, args_file=rt.run / "hook-qemu-args")
        prepared = True
        opts.extra_args = fd_args + opts.extra_args + hook_args
        plan = build(cfg, caps, rt, opts)
        for w in plan.warnings:
            log.warning("%s", w)
        try:
            _start_sidecars(plan.sidecars, rt)
        except SidecarFailed as e:
            if e.name != "passt":
                raise
            # passt is an optimisation; QEMU's built-in user networking does
            # the same job more slowly.
            log.debug("passt failed: %s", e.details)
            opts.passt = None
            plan = build(cfg, caps, rt, opts)
            plan.warnings.append("passt could not start; using QEMU's built-in NAT instead.")
            _start_sidecars(plan.sidecars, rt)
        argv = plan.argv
        if cfg.machine.cpu_quota_pct and which("systemd-run") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                    f"--unit=rehome-{cfg.short_id}-{int(time.time())}",
                    "-p", f"CPUQuota={cfg.machine.cpu_quota_pct}%", "--"] + argv
        proc = _launch(cfg, argv, pass_fds, log_dir / "qemu.log", choice, plan.warnings)
        started_at = time.time()
        _wait_ready(cfg, proc, rt, log_dir / "qemu.log")
        if opts.incoming:
            _restore(cfg, saved)
    except BaseException:
        if proc is not None and proc.poll() is None:
            # Started but unusable (e.g. the saved state did not restore).
            proc.kill()
            proc.wait(timeout=30)
            _stop_sidecars(rt)
        if prepared:
            _undo_prepare(cfg)
        raise
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass
    meta = {"pid": proc.pid, "started_at": started_at, "accel": choice.accel,
            "console": plan.console, "warnings": plan.warnings, "argv": argv}
    _state_file(cfg).write_text(json.dumps(meta, indent=2))
    _CHILDREN[cfg.id] = proc
    if cfg.direct_boot and cfg.direct_boot.once:
        from rehome.vm import library
        with library.locked(cfg):
            fresh = library.reload(cfg)
            fresh.direct_boot = None
            library.save(fresh)
    hooks.run("started", cfg)
    return state(cfg)


def _undo_prepare(cfg: VMConfig) -> None:
    """The start failed after the prepare hooks ran: let them clean up."""
    try:
        hooks.run("stopped", cfg)
    except RehomeError as e:
        log.warning("%s", e)


class SidecarFailed(RehomeError):
    def __init__(self, name: str, details: str):
        super().__init__(f"{name} failed to start.", details=details, code="sidecar-failed")
        self.name = name


# Popen objects of VMs started by this process, so they can be reaped.
_CHILDREN: dict[str, subprocess.Popen] = {}


def reap() -> None:
    for vm_id, p in list(_CHILDREN.items()):
        if p.poll() is not None:
            del _CHILDREN[vm_id]


def _launch(cfg, argv, pass_fds, log_path: Path, choice, warnings) -> subprocess.Popen:
    _rotate(log_path)
    logf = open(log_path, "a", buffering=1)
    logf.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting {cfg.name} "
               f"({choice.accel}) ===\n$ {quote(argv)}\n")
    for w in warnings:
        logf.write(f"warning: {w}\n")
    if choice.warning:
        logf.write(f"note: {choice.warning}\n")
    logf.flush()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                                pass_fds=pass_fds, start_new_session=True, close_fds=True)
    except FileNotFoundError as e:
        raise RehomeError("QEMU could not be started.", fix="Install QEMU (run `rehome doctor`).",
                          details=str(e)) from e
    finally:
        logf.close()
    return proc


def _rotate(log_path: Path, keep: int = 5) -> None:
    if not log_path.exists() or log_path.stat().st_size < 2 * 2**20:
        return
    for i in range(keep - 1, 0, -1):
        src = log_path.with_name(f"{log_path.name}.{i}")
        if src.exists():
            src.rename(log_path.with_name(f"{log_path.name}.{i + 1}"))
    log_path.rename(log_path.with_name(f"{log_path.name}.1"))


def _tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def explain_qemu_failure(log_text: str) -> tuple[str, str]:
    """Turn common QEMU start-up errors into plain English."""
    t = log_text.lower()
    if "could not access kvm kernel module" in t or "failed to initialize kvm" in t:
        return ("KVM could not be used.", "Run `rehome doctor` to see how to enable KVM.")
    if "failed to get" in t and "lock" in t:
        return ("A disk of this VM is in use by another program (another VM, or a "
                "program that has the image open).",
                "Close the other program or VM that uses the disk, then try again.")
    if "permission denied" in t:
        return ("QEMU was not allowed to open a file or device.",
                "Check the permissions of the VM's disk files and the technical details.")
    if "could not open" in t and "no such file" in t:
        return ("A file the VM needs is missing.", "See the technical details for the path.")
    if "cannot allocate memory" in t or "out of memory" in t:
        return ("There is not enough free memory to start the VM.",
                "Close other programs or VMs, or give this VM less memory.")
    if "address already in use" in t:
        return ("A network port forward is already in use by another program.",
                "Change or remove the port forward in the network settings.")
    return ("QEMU refused to start the VM.", "See the technical details below.")


def _wait_ready(cfg: VMConfig, proc: subprocess.Popen, rt: RuntimePaths, log_path: Path,
                timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = _tail(log_path)
            msg, fix = explain_qemu_failure(tail)
            _stop_sidecars(rt)
            raise RehomeError(msg, fix=fix, details=tail, code="qemu-start-failed")
        if os.path.exists(rt.qmp) and _read_pid(rt.pidfile):
            try:
                with QMP(rt.qmp, timeout=5) as q:
                    q.status()
                return
            except Exception:
                pass
        time.sleep(0.05)
    proc.kill()
    raise RehomeError("QEMU did not start in time.", details=_tail(log_path))


def _restore(cfg: VMConfig, saved: Path) -> None:
    with qmp(cfg, timeout=600) as q:
        caps = host_info.gather().caps(cfg.machine.arch)
        uri = f"file:{saved}" if caps and caps.at_least(8, 2) else f"exec:cat {_shq(str(saved))}"
        q.execute("migrate-incoming", {"uri": uri})
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            st = q.execute("query-migrate")
            status = st.get("status", "")
            if status == "completed" or q.status() not in ("inmigrate",):
                break
            if status == "failed":
                raise RehomeError("The saved state could not be restored.",
                                  fix="Discard the saved state and start the VM fresh.",
                                  details=json.dumps(st))
            time.sleep(0.2)
        if q.status() in ("paused", "postmigrate", "prelaunch"):
            q.execute("cont")
    saved.unlink(missing_ok=True)


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _start_sidecars(sidecars, rt: RuntimePaths) -> None:
    started = []
    try:
        for sc in sidecars:
            if sc.socket:
                Path(sc.socket).unlink(missing_ok=True)
            out = open(sc.log, "a") if sc.log else subprocess.DEVNULL
            try:
                p = subprocess.Popen(sc.argv, stdin=subprocess.DEVNULL, stdout=out,
                                     stderr=subprocess.STDOUT if sc.log else subprocess.PIPE,
                                     start_new_session=True)
            except FileNotFoundError as e:
                raise RehomeError(f"{sc.name} could not be started.", details=str(e)) from e
            finally:
                if sc.log:
                    out.close()
            started.append((sc, p))
            if sc.pidfile and not sc.daemonizes:
                Path(sc.pidfile).write_text(f"{p.pid}\n")
            deadline = time.monotonic() + 15
            while sc.socket and not os.path.exists(sc.socket):
                ret = p.poll()
                if ret is not None and (ret != 0 or not sc.daemonizes):
                    err = p.stderr.read().decode(errors="replace") if p.stderr else ""
                    if sc.log:
                        err += _tail(Path(sc.log), 10)
                    raise SidecarFailed(sc.name, f"$ {quote(sc.argv)}\n{err}")
                if time.monotonic() > deadline:
                    raise SidecarFailed(sc.name, f"$ {quote(sc.argv)}\n(timed out)")
            if sc.socket and not sc.daemonizes:
                # Some programs create the socket and fail right after.
                time.sleep(0.2)
                if p.poll() is not None:
                    err = _tail(Path(sc.log), 10) if sc.log else ""
                    raise SidecarFailed(sc.name, f"$ {quote(sc.argv)}\n{err}")
                time.sleep(0.05)
            if sc.daemonizes:
                p.wait(timeout=10)
    except BaseException:
        for sc, p in started:
            _kill_sidecar(sc, p)
        raise


def _kill_sidecar(sc, p) -> None:
    pid = _read_pid(sc.pidfile) if sc.pidfile else 0
    if pid and not _is_sidecar(pid, Path(sc.pidfile)):
        pid = 0
    for target in filter(None, (pid, p.pid if p.poll() is None else 0)):
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
    if sc.pidfile:
        Path(sc.pidfile).unlink(missing_ok=True)


def _is_sidecar(pid: int, pidfile: Path) -> bool:
    """Is ``pid`` still the helper named by its pidfile (passt-net0.pid -> passt)?

    Guards against signalling an unrelated process that reused the PID.
    """
    want = pidfile.stem.split("-")[0]
    try:
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
    except OSError:
        return False
    return want in os.path.basename(argv0)


def _stop_sidecars(rt: RuntimePaths) -> None:
    for pidfile in rt.run.glob("*.pid"):
        if pidfile.name == "qemu.pid":
            continue
        pid = _read_pid(str(pidfile))
        if pid and _is_sidecar(pid, pidfile):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        pidfile.unlink(missing_ok=True)


# --- lifecycle operations ----------------------------------------------------------

def shutdown(cfg: VMConfig, wait: float = 0) -> None:
    """Ask the guest OS to shut down (ACPI power button)."""
    with qmp(cfg) as q:
        q.execute("system_powerdown")
    if wait:
        if not wait_stopped(cfg, wait):
            raise RehomeError(f"\"{cfg.name}\" did not shut down within {int(wait)} seconds.",
                              fix="The guest may be ignoring the power button (DOS and very old "
                                  "systems do). Use Turn Off to force it.", code="shutdown-timeout")


def force_off(cfg: VMConfig) -> None:
    pid = qemu_pid(cfg)
    if not pid:
        return
    try:
        with QMP(runtime(cfg).qmp, timeout=5).connect(retries=1) as q:
            q.execute("quit")
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if not wait_stopped(cfg, 10):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        wait_stopped(cfg, 5)
    after_stop(cfg)


def reset(cfg: VMConfig) -> None:
    with qmp(cfg) as q:
        q.execute("system_reset")


def pause(cfg: VMConfig) -> None:
    with qmp(cfg) as q:
        q.execute("stop")


def resume(cfg: VMConfig) -> None:
    with qmp(cfg) as q:
        q.execute("cont")


def save(cfg: VMConfig, progress=None) -> None:
    """Save the VM's memory to disk and stop it (Hyper-V "Save")."""
    target = saved_state_path(cfg)
    tmp = target.with_suffix(".partial")
    tmp.unlink(missing_ok=True)
    with qmp(cfg, timeout=600) as q:
        caps = host_info.gather().caps(cfg.machine.arch)
        q.execute("stop")
        uri = f"file:{tmp}" if caps and caps.at_least(8, 2) else f"exec:cat > {_shq(str(tmp))}"
        q.execute("migrate", {"uri": uri})
        while True:
            st = q.execute("query-migrate")
            s = st.get("status")
            if progress and "ram" in st:
                ram = st["ram"]
                progress(ram.get("transferred", 0), ram.get("total", 0))
            if s == "completed":
                break
            if s in ("failed", "cancelled"):
                q.execute("cont")
                raise RehomeError("The VM's state could not be saved.", details=json.dumps(st))
            time.sleep(0.2)
        tmp.rename(target)
        try:
            q.execute("quit")
        except QmpClosed:
            pass
    wait_stopped(cfg, 30)
    after_stop(cfg)


def discard_saved_state(cfg: VMConfig) -> None:
    saved_state_path(cfg).unlink(missing_ok=True)


def wait_stopped(cfg: VMConfig, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reap()
        if not is_running(cfg):
            return True
        time.sleep(0.1)
    return False


def after_stop(cfg: VMConfig) -> None:
    """Clean up after QEMU exited (sidecars, sockets, hooks)."""
    rt = runtime(cfg)
    _stop_sidecars(rt)
    for s in rt.run.glob("*.sock"):
        s.unlink(missing_ok=True)
    _state_file(cfg).unlink(missing_ok=True)
    try:
        hooks.run("stopped", cfg)
    except RehomeError as e:
        log.warning("%s", e)


def external_viewer(cfg: VMConfig) -> list[str]:
    """Command line of an external viewer for a VM whose console is VNC or SPICE.

    Used when QEMU lacks the D-Bus display module (see docs/DECISIONS.md, D7).
    """
    st = state(cfg)
    if not st.active:
        raise RehomeError(f"\"{cfg.name}\" is not running.", code="not-running")
    rt = runtime(cfg)
    if st.console not in ("vnc", "spice"):
        raise RehomeError("This VM's screen is shown in Rehome's own console window.",
                          fix=f"Run `rehome console \"{cfg.name}\"`.")
    sock = rt.vnc if st.console == "vnc" else rt.spice
    viewer = which("remote-viewer")
    if viewer:
        return [viewer, "--title", cfg.name, f"{st.console}+unix://{sock}"]
    if st.console == "vnc" and which("vncviewer"):        # TigerVNC accepts a socket path
        return [which("vncviewer"), sock]
    raise RehomeError("No external viewer is installed.",
                      fix="Install virt-viewer (it provides remote-viewer), or install QEMU's "
                          "D-Bus display module so Rehome can show the screen itself "
                          "(run `rehome doctor`).")


def screenshot(cfg: VMConfig, path: str | os.PathLike) -> None:
    with qmp(cfg) as q:
        q.screendump(str(path))


def screen_text(cfg: VMConfig) -> str:
    """Text currently shown in VGA text mode (80x25 at 0xB8000), or '' in graphics modes.

    Used by boot tests and handy for scripting DOS-era guests.
    """
    import tempfile
    with tempfile.NamedTemporaryFile(prefix="vga-", suffix=".bin",
                                     dir=str(runtime(cfg).run)) as tmp:
        with qmp(cfg) as q:
            q.execute("pmemsave", {"val": 0xB8000, "size": 4000, "filename": tmp.name})
        data = Path(tmp.name).read_bytes()
    chars = bytes(data[i] for i in range(0, len(data), 2)).decode("cp437")
    lines = [chars[r * 80:(r + 1) * 80].rstrip() for r in range(25)]
    return "\n".join(lines).rstrip()


def send_keys(cfg: VMConfig, keys: list[str], hold_ms: int = 100) -> None:
    """Send a key combination, e.g. ["ctrl", "alt", "delete"] (QEMU qcodes)."""
    with qmp(cfg) as q:
        q.execute("send-key", {"keys": [{"type": "qcode", "data": k} for k in keys],
                               "hold-time": hold_ms})


def type_text(cfg: VMConfig, text: str) -> None:
    """Type ASCII text into the guest (US layout), used by tests and automation."""
    with qmp(cfg) as q:
        for ch in text:
            keys = _char_to_qcodes(ch)
            q.execute("send-key", {"keys": [{"type": "qcode", "data": k} for k in keys]})
            time.sleep(0.02)


_SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8',
            '(': '9', ')': '0', '_': 'minus', '+': 'equal', '{': 'bracket_left',
            '}': 'bracket_right', '|': 'backslash', ':': 'semicolon', '"': 'apostrophe',
            '<': 'comma', '>': 'dot', '?': 'slash', '~': 'grave_accent'}
_PLAIN = {' ': 'spc', '\n': 'ret', '\t': 'tab', '-': 'minus', '=': 'equal',
          '[': 'bracket_left', ']': 'bracket_right', '\\': 'backslash', ';': 'semicolon',
          "'": 'apostrophe', ',': 'comma', '.': 'dot', '/': 'slash', '`': 'grave_accent'}


def _char_to_qcodes(ch: str) -> list[str]:
    if ch.isalpha():
        return ["shift", ch.lower()] if ch.isupper() else [ch]
    if ch.isdigit():
        return [ch]
    if ch in _PLAIN:
        return [_PLAIN[ch]]
    if ch in _SHIFTED:
        return ["shift", _SHIFTED[ch]]
    raise ValueError(f"cannot type {ch!r}")


def change_cd(cfg: VMConfig, cd_id: str, iso: str | None) -> None:
    with qmp(cfg) as q:
        if iso:
            q.execute("blockdev-change-medium", {"id": cd_id, "filename": iso,
                                                 "format": "raw", "read-only-mode": "read-only"})
        else:
            q.execute("eject", {"id": cd_id, "force": True})


def guest_agent(cfg: VMConfig, command: str, args: dict | None = None, timeout: float = 5.0):
    """Run a qemu-guest-agent command (returns None if no agent is answering)."""
    import socket as _socket
    path = runtime(cfg).qga
    if not os.path.exists(path):
        return None
    try:
        s = _socket.socket(_socket.AF_UNIX)
        s.settimeout(timeout)
        s.connect(path)
        sync_id = int(time.time()) & 0xFFFF
        s.sendall(json.dumps({"execute": "guest-sync", "arguments": {"id": sync_id}}).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            buf += s.recv(65536)
        msg = {"execute": command}
        if args:
            msg["arguments"] = args
        s.sendall(json.dumps(msg).encode() + b"\n")
        buf = buf.split(b"\n", 1)[1]
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        reply = json.loads(buf.split(b"\n", 1)[0])
        return reply.get("return")
    except (OSError, ValueError):
        return None
