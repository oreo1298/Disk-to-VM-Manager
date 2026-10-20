"""Checkpoints (snapshots), shown as a tree like Hyper-V.

Every writable qcow2 disk and the UEFI variable store receive an internal
snapshot with the same tag. A checkpoint taken while the VM runs includes
its RAM (and the TPM state, which QEMU migrates with it); reverting to it
resumes exactly where it was. Offline checkpoints also copy the TPM state
directory.
"""

from __future__ import annotations

import datetime as dt
import shutil
import uuid
from dataclasses import dataclass, field

from rehome.errors import RehomeError
from rehome.vm import disks, library, runner
from rehome.vm.config import Snapshot, VMConfig, now_iso


def _targets(cfg: VMConfig) -> list[tuple[str, str]]:
    """(node name, file path) of every image that takes part in checkpoints."""
    out = []
    for d in cfg.disks:
        if d.format == "qcow2" and not d.readonly:
            out.append((d.id, str(cfg.resolve(d.file))))
    nvram = cfg.dir / "nvram.qcow2"
    if nvram.exists():
        out.append(("fw-vars", str(nvram)))
    return out


def default_name(cfg: VMConfig) -> str:
    return f"{cfg.name} - ({dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"


def _new_tag() -> str:
    return "rehome-" + uuid.uuid4().hex[:12]


def create(cfg: VMConfig, name: str = "", description: str = "", kind: str = "user",
           progress=None) -> Snapshot:
    targets = _targets(cfg)
    if not targets:
        raise RehomeError("This VM has no disks that support checkpoints.",
                          fix="Checkpoints need qcow2 disks.")
    snap = Snapshot(id=_new_tag(), name=name or default_name(cfg), description=description,
                    parent=cfg.current_snapshot, created=now_iso(), kind=kind)
    st = runner.state(cfg)
    if st.active:
        with runner.qmp(cfg, timeout=600) as q:
            job = f"snap-{snap.id}"
            q.execute("snapshot-save", {"job-id": job, "tag": snap.id,
                                        "vmstate": targets[0][0],
                                        "devices": [n for n, _ in targets]})
            q.wait_job(job, progress=progress)
        snap.has_state = True
    else:
        done = []
        try:
            for _node, path in targets:
                disks.snapshot_create(path, snap.id)
                done.append(path)
        except RehomeError:
            for path in done:
                try:
                    disks.snapshot_delete(path, snap.id)
                except RehomeError:
                    pass
            raise
        tpm = cfg.dir / "tpm"
        if tpm.is_dir():
            shutil.copytree(tpm, _tpm_copy(cfg, snap.id))
    with library.locked(cfg):
        fresh = library.reload(cfg)
        fresh.snapshots.append(snap)
        fresh.current_snapshot = snap.id
        library.save(fresh)
    cfg.snapshots, cfg.current_snapshot = fresh.snapshots, fresh.current_snapshot
    return snap


def _tpm_copy(cfg: VMConfig, tag: str):
    return cfg.dir / "snapshots" / tag / "tpm"


def revert(cfg: VMConfig, ref: str) -> Snapshot:
    snap = cfg.snapshot(ref)
    st = runner.state(cfg)
    if st.active:
        if not snap.has_state:
            raise RehomeError(
                "This checkpoint was taken while the VM was off, so the VM must be turned off "
                "before reverting to it.", fix="Turn the VM off, then revert.")
        with runner.qmp(cfg, timeout=600) as q:
            job = f"load-{snap.id}"
            q.execute("snapshot-load", {"job-id": job, "tag": snap.id,
                                        "vmstate": _targets(cfg)[0][0],
                                        "devices": [n for n, _ in _targets(cfg)]})
            q.wait_job(job)
            if q.status() != "running":
                q.execute("cont")
    else:
        for _node, path in _targets(cfg):
            disks.snapshot_apply(path, snap.id)
        saved_tpm = _tpm_copy(cfg, snap.id)
        if saved_tpm.is_dir():
            shutil.rmtree(cfg.dir / "tpm", ignore_errors=True)
            shutil.copytree(saved_tpm, cfg.dir / "tpm")
        runner.discard_saved_state(cfg)
    with library.locked(cfg):
        fresh = library.reload(cfg)
        fresh.current_snapshot = snap.id
        library.save(fresh)
    cfg.current_snapshot = snap.id
    return snap


def start_from(cfg: VMConfig, ref: str, **kw):
    """Revert to a checkpoint that has RAM state while the VM is off, and run it."""
    snap = cfg.snapshot(ref)
    if runner.state(cfg).active:
        return revert(cfg, ref)
    st = runner.start(cfg, loadvm=snap.id if snap.has_state else "", restore=False, **kw)
    with library.locked(cfg):
        fresh = library.reload(cfg)
        fresh.current_snapshot = snap.id
        library.save(fresh)
    return st


def delete(cfg: VMConfig, ref: str, recursive: bool = False) -> list[Snapshot]:
    snap = cfg.snapshot(ref)
    victims = [snap]
    if recursive:
        victims = subtree(cfg, snap.id)
    st = runner.state(cfg)
    for v in victims:
        if st.active:
            with runner.qmp(cfg, timeout=600) as q:
                job = f"del-{v.id}"
                q.execute("snapshot-delete", {"job-id": job, "tag": v.id,
                                              "devices": [n for n, _ in _targets(cfg)]})
                q.wait_job(job)
        else:
            for _node, path in _targets(cfg):
                if any(s.get("name") == v.id for s in disks.snapshot_list(path)):
                    disks.snapshot_delete(path, v.id)
        shutil.rmtree(cfg.dir / "snapshots" / v.id, ignore_errors=True)
    ids = {v.id for v in victims}
    with library.locked(cfg):
        fresh = library.reload(cfg)
        for s in fresh.snapshots:
            if s.parent in ids:
                s.parent = snap.parent      # re-parent children to the grandparent
        if fresh.current_snapshot in ids:
            fresh.current_snapshot = snap.parent
        fresh.snapshots = [s for s in fresh.snapshots if s.id not in ids]
        library.save(fresh)
    cfg.snapshots, cfg.current_snapshot = fresh.snapshots, fresh.current_snapshot
    return victims


def subtree(cfg: VMConfig, root_id: str) -> list[Snapshot]:
    by_parent: dict[str, list[Snapshot]] = {}
    for s in cfg.snapshots:
        by_parent.setdefault(s.parent, []).append(s)
    out, stack = [], [cfg.snapshot(root_id)]
    while stack:
        s = stack.pop()
        out.append(s)
        stack.extend(by_parent.get(s.id, []))
    return out


@dataclass
class TreeNode:
    snapshot: Snapshot | None          # None marks the "Now" position
    children: list["TreeNode"] = field(default_factory=list)


def tree(cfg: VMConfig) -> list[TreeNode]:
    """Return root nodes; a ``Now`` node hangs under the current checkpoint."""
    nodes = {s.id: TreeNode(s) for s in cfg.snapshots}
    roots: list[TreeNode] = []
    for s in sorted(cfg.snapshots, key=lambda s: s.created):
        parent = nodes.get(s.parent)
        (parent.children if parent else roots).append(nodes[s.id])
    now = TreeNode(None)
    cur = nodes.get(cfg.current_snapshot)
    (cur.children if cur else roots).append(now)
    return roots


def render_tree(cfg: VMConfig) -> list[str]:
    lines: list[str] = []

    def walk(n: TreeNode, prefix: str, last: bool, top: bool):
        branch = "" if top else ("└─ " if last else "├─ ")
        if n.snapshot is None:
            label = "▶ Now"
        else:
            s = n.snapshot
            extra = " [with memory]" if s.has_state else ""
            label = f"{s.name}{extra}  ({s.id})"
        lines.append(prefix + branch + label)
        child_prefix = prefix + ("" if top else ("   " if last else "│  "))
        for i, c in enumerate(n.children):
            walk(c, child_prefix, i == len(n.children) - 1, False)

    roots = tree(cfg)
    for i, r in enumerate(roots):
        walk(r, "", i == len(roots) - 1, True)
    return lines
