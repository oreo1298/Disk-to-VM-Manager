"""Long-running operations: staged progress, ETA and cancellation.

A :class:`Progress` object is passed into imports, copies, downloads and
patching. The CLI renders it as a progress bar; the GUI observes it from the
main loop. Work functions call ``stage()`` when a new phase starts and
``update()`` with the fraction done within that phase, and check
``cancelled`` (or call ``check()``) regularly.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from rehome.errors import Cancelled


@dataclass
class Stage:
    name: str
    weight: float = 1.0


@dataclass
class Progress:
    title: str = ""
    stages: list[Stage] = field(default_factory=list)
    on_change: Callable[["Progress"], None] | None = None
    stage_index: int = -1
    stage_name: str = ""
    stage_fraction: float = 0.0
    detail: str = ""
    started: float = field(default_factory=time.monotonic)
    stage_started: float = field(default_factory=time.monotonic)
    _cancel: threading.Event = field(default_factory=threading.Event)
    log: list[str] = field(default_factory=list)

    # --- producer side -----------------------------------------------------------

    def stage(self, name: str, detail: str = "") -> None:
        self.check()
        names = [s.name for s in self.stages]
        if name in names:
            self.stage_index = names.index(name)
        else:
            self.stages.append(Stage(name))
            self.stage_index = len(self.stages) - 1
        self.stage_name = name
        self.stage_fraction = 0.0
        self.detail = detail
        self.stage_started = time.monotonic()
        self.log.append(name)
        self._notify()

    def update(self, fraction: float, detail: str | None = None) -> None:
        self.check()
        self.stage_fraction = max(0.0, min(1.0, fraction))
        if detail is not None:
            self.detail = detail
        self._notify()

    def note(self, message: str) -> None:
        self.log.append(message)
        self.detail = message
        self._notify()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    # --- consumer side -----------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def fraction(self) -> float:
        if not self.stages:
            return self.stage_fraction
        total = sum(s.weight for s in self.stages)
        done = sum(s.weight for s in self.stages[:max(0, self.stage_index)])
        cur = self.stages[self.stage_index].weight if self.stage_index >= 0 else 0
        return min(1.0, (done + cur * self.stage_fraction) / total) if total else 0.0

    @property
    def eta_seconds(self) -> float | None:
        """Remaining time for the current stage (stages differ too much to extrapolate)."""
        f = self.stage_fraction
        if f <= 0.02:
            return None
        elapsed = time.monotonic() - self.stage_started
        return max(0.0, elapsed / f - elapsed)

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change(self)
            except Exception:
                pass

    # Callbacks in the shape used by lower layers (qemu-img convert, hashing).
    def fraction_cb(self) -> Callable[[float], None]:
        return lambda f: self.update(f)

    def cancel_cb(self) -> Callable[[], bool]:
        return self.is_cancelled


def run_in_thread(fn: Callable[[], object], on_done: Callable[[object, BaseException | None], None]):
    """Run ``fn`` in a worker thread and call ``on_done(result, error)`` when finished."""
    def worker():
        try:
            res = fn()
        except BaseException as e:  # delivered to the caller
            on_done(None, e)
        else:
            on_done(res, None)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t
