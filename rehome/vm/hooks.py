"""User hook scripts — the extension point for GPU pass-through and similar.

Executable files in ``~/.config/rehome/hooks.d/`` run at each phase with
``argv = [script, PHASE, VM_ID]`` and the environment variables
``REHOME_PHASE``, ``REHOME_VM_ID``, ``REHOME_VM_NAME``, ``REHOME_VM_DIR``.

Phases: ``prepare`` (before QEMU starts; a non-zero exit aborts the start),
``started``, ``stopped`` (after Rehome observed the VM stop).
"""

from __future__ import annotations

import logging
import os
import subprocess

from rehome import paths
from rehome.errors import RehomeError
from rehome.vm.config import VMConfig

log = logging.getLogger(__name__)


def hooks_dir():
    return paths.config_dir() / "hooks.d"


def run(phase: str, cfg: VMConfig) -> None:
    d = hooks_dir()
    if not d.is_dir():
        return
    env = dict(os.environ, REHOME_PHASE=phase, REHOME_VM_ID=cfg.id,
               REHOME_VM_NAME=cfg.name, REHOME_VM_DIR=str(cfg.dir))
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
