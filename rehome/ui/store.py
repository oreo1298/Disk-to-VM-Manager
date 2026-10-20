"""Live list of VMs with their state and a thumbnail of their screen.

A background thread polls the library and each VM's state (QMP is quick but
may block), and takes a screenshot of running VMs every few seconds for the
thumbnails. Changes are delivered on the GTK main loop.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject  # noqa: E402

from rehome.vm import library, runner  # noqa: E402
from rehome.vm.config import VMConfig  # noqa: E402

log = logging.getLogger(__name__)
THUMB_INTERVAL = 6.0


@dataclass
class VmItem:
    cfg: VMConfig
    status: str = runner.STOPPED
    pid: int = 0
    thumbnail: str = ""        # PNG path
    thumb_time: float = 0.0


class VmStore(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),           # list membership/order
        "item-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),  # vm id
    }

    def __init__(self):
        super().__init__()
        self.items: dict[str, VmItem] = {}
        self.order: list[str] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh_now(self) -> None:
        self._wake.set()

    def get(self, vm_id: str) -> VmItem | None:
        return self.items.get(vm_id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception:  # keep polling no matter what
                log.exception("VM poll failed")
            self._wake.wait(2.0)
            self._wake.clear()

    def _poll(self) -> None:
        vms = library.list_vms()
        ids = [v.id for v in vms]
        states = {}
        for v in vms:
            st = runner.state(v, timeout=1.0)
            runner.reap()
            states[v.id] = st
        thumbs = {}
        now = time.monotonic()
        for v in vms:
            item = self.items.get(v.id)
            st = states[v.id]
            if st.status in (runner.RUNNING,) and (item is None or now - item.thumb_time > THUMB_INTERVAL):
                path = str(runner.runtime(v).run / "thumb.png")
                try:
                    runner.screenshot(v, path)
                    thumbs[v.id] = (path, now)
                except Exception:
                    pass
        GLib.idle_add(self._apply, vms, ids, states, thumbs)

    def _apply(self, vms, ids, states, thumbs):
        membership_changed = ids != self.order
        for v in vms:
            item = self.items.get(v.id)
            st = states[v.id]
            changed = False
            if item is None:
                item = VmItem(cfg=v)
                self.items[v.id] = item
                changed = True
            if item.cfg.to_dict() != v.to_dict():
                item.cfg = v
                changed = True
            if item.status != st.status:
                item.status, item.pid = st.status, st.pid
                changed = True
            if v.id in thumbs:
                item.thumbnail, item.thumb_time = thumbs[v.id]
                changed = True
            if not item.thumbnail:
                saved = v.dir / "thumbnail.png"
                if saved.exists():
                    item.thumbnail = str(saved)
            if st.status not in (runner.RUNNING, runner.PAUSED) and item.thumbnail.endswith("/thumb.png") \
                    and os.path.exists(item.thumbnail):
                # Keep the last picture of the screen after the VM stops.
                try:
                    import shutil
                    shutil.copy(item.thumbnail, v.dir / "thumbnail.png")
                    item.thumbnail = str(v.dir / "thumbnail.png")
                except OSError:
                    pass
            if changed:
                self.emit("item-updated", v.id)
        for gone in set(self.items) - set(ids):
            del self.items[gone]
        self.order = ids
        if membership_changed:
            self.emit("changed")
        return False


def load_texture(path: str, max_width: int = 0) -> Gdk.Texture | None:
    if not path or not os.path.exists(path):
        return None
    try:
        return Gdk.Texture.new_from_filename(path)
    except GLib.Error:
        return None
