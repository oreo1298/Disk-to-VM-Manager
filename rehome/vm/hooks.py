"""User hook scripts — the extension point for GPU pass-through and similar.

Executable files in ``~/.config/rehome/hooks.d/`` run at each phase with
``argv = [script, PHASE, VM_ID]`` and the environment variables
``REHOME_PHASE``, ``REHOME_VM_ID``, ``REHOME_VM_NAME``, ``REHOME_VM_DIR``.

Phases: ``prepare`` (before QEMU starts; a non-zero exit aborts the start),
``started``, ``stopped`` (after Rehome observed the VM stop, and also when a
start fails after ``prepare`` ran, so hooks can undo what they prepared).

In the ``prepare`` phase ``REHOME_QEMU_ARGS_FILE`` names a file a hook may
append QEMU arguments to, one per line (blank lines and ``#`` comments are
ignored). This is the supported way to experiment with VFIO/GPU pass-through
without Rehome implementing it. See docs/HOOKS.md.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from rehome import paths
from rehome.errors import RehomeError
from rehome.vm.config import VMConfig

log = logging.getLogger(__name__)


def hooks_dir():
    return paths.config_dir() / "hooks.d"


def run(phase: str, cfg: VMConfig, args_file: Path | None = None) -> list[str]:
    """Run the hooks for ``phase``; returns QEMU arguments added by ``prepare`` hooks."""
    d = hooks_dir()
    if not d.is_dir():
        return []
    env = dict(os.environ, REHOME_PHASE=phase, REHOME_VM_ID=cfg.id,
               REHOME_VM_NAME=cfg.name, REHOME_VM_DIR=str(cfg.dir))
    if args_file is not None:
        args_file.write_text("")
        env["REHOME_QEMU_ARGS_FILE"] = str(args_file)
    for script in sorted(d.iterdir()):
        if not (script.is_file() and os.access(script, os.X_OK)):
            continue
        try:
            cp = subprocess.run([str(script), phase, cfg.id], env=env, capture_output=True,
                                text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            if phase == "prepare":
                raise RehomeError(f"The hook script {script.name} failed.", details=str(e)) from e
            log.warning("hook %s failed: %s", script, e)
            continue
        if cp.returncode != 0:
            if phase == "prepare":
                raise RehomeError(f"The hook script {script.name} stopped the VM from starting.",
                                  details=(cp.stdout or "") + (cp.stderr or ""))
            log.warning("hook %s exited with %s", script, cp.returncode)
    if args_file is None:
        return []
    lines = [ln.strip() for ln in args_file.read_text().splitlines()]
    args_file.unlink(missing_ok=True)
    return [ln for ln in lines if ln and not ln.startswith("#")]
