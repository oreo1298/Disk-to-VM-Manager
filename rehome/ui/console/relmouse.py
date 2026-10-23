"""Mouse input for guests that only have a relative mouse (PS/2: DOS, Windows 3.x/9x).

Such a guest moves its own cursor by the deltas it receives, so the console
captures the pointer and sends the host pointer's movement. GTK 4 can neither
lock nor warp the host pointer, which caused two problems, reported with
Windows 98 ("the mouse doesn't move across the screen"):

* Windows 3.x/9x (and many DOS drivers) accelerate: a move of more than 6
  counts in one packet goes twice as far. Fast host movements therefore moved
  the guest cursor further than the host pointer, and the two drifted apart.
* When the hidden host pointer reached the edge of the window, movement
  stopped, with the guest cursor short of its own edge.

So moves are sent in steps of at most 6 counts, which Windows does not
accelerate, and when the host pointer reaches an edge of the guest image the
guest cursor is pushed all the way to the same edge, which lines the two up
again. QEMU makes one PS/2 packet of each step, but its PS/2 queue holds only
16 bytes (5 packets): what does not fit is added to the next packet, which then
gets accelerated, or (a push) cancels the moves after it. Everything is
therefore queued here and sent at most BURST packets every TICK_MS, buttons
included so that clicks land where the cursor has got to. A move that still did
not fit stays inside QEMU until the next input event, so the console sends an
empty move once the queue is empty.
"""

from __future__ import annotations

import math
from collections import deque

STEP = 6            # Windows' MouseThreshold1: larger moves per packet are accelerated
PACKET = 127        # largest move in one PS/2 packet
BURST = 3           # packets sent at a time: QEMU's PS/2 queue holds 5
TICK_MS = 3         # between bursts: ~1000 packets/s, which an emulated (TCG) guest keeps up
                    # with even on a busy host (4 per 2 ms already merged now and then)
BACKLOG = 64        # queued packets (~32 ms) after which moves are merged to catch up


class RelMouse:
    """Queue of PS/2-sized events for a relative-mouse guest.

    Items are ("rel", dx, dy), ("push", dx, dy) (a move to an edge) or ("btn",
    button, pressed); the console sends ``take()`` every TICK_MS while ``pending()``.
    """

    def __init__(self) -> None:
        self.queue: deque[tuple] = deque()
        self.reset()

    def reset(self) -> None:
        self.queue.clear()
        self._rx = self._ry = 0.0
        self._edge = (0, 0)

    def pending(self) -> bool:
        return bool(self.queue)

    def motion(self, dx: float, dy: float) -> None:
        """Queue a move (guest pixels) as steps of at most STEP per axis; fractions are kept."""
        self._rx += dx
        self._ry += dy
        ix, iy = int(self._rx), int(self._ry)
        self._rx -= ix
        self._ry -= iy
        n = math.ceil(max(abs(ix), abs(iy)) / STEP)
        sx = sy = 0
        for i in range(1, n + 1):
            tx, ty = round(ix * i / n), round(iy * i / n)
            self.queue.append(("rel", tx - sx, ty - sy))
            sx, sy = tx, ty

    def button(self, button: int, pressed: bool) -> None:
        self.queue.append(("btn", button, pressed))

    def edge(self, x: float, y: float, layout, size: tuple[int, int], margin: float = 1.0) -> bool:
        """The host pointer is at (x, y): if it has just reached an edge of the guest image
        (``layout`` = x, y, w, h where it is drawn), queue a push of the guest cursor to that
        edge. ``size`` is the guest screen's size in pixels."""
        lx, ly, lw, lh = layout
        ex = -1 if x <= lx + margin else 1 if x >= lx + lw - margin else 0
        ey = -1 if y <= ly + margin else 1 if y >= ly + lh - margin else 0
        old, self._edge = self._edge, (ex, ey)
        px = ex if ex and ex != old[0] else 0
        py = ey if ey and ey != old[1] else 0
        if px or py:
            self._push(px, py, size)
            return True
        return False

    def left(self, x: float, y: float, layout, size: tuple[int, int]) -> None:
        """The host pointer left the view from (x, y): push to the edge it went out by."""
        lx, ly, lw, lh = layout
        fx = (x - lx - lw / 2) / (lw / 2 or 1)
        fy = (y - ly - lh / 2) / (lh / 2 or 1)
        if abs(fx) >= abs(fy):
            self._push(1 if fx > 0 else -1, 0, size)
        else:
            self._push(0, 1 if fy > 0 else -1, size)
        self._edge = (0, 0)

    def align(self, x: float, y: float, size: tuple[int, int]) -> None:
        """Queue moves that take the guest cursor to (x, y) in guest pixels: to the
        top-left corner, then across in unaccelerated steps (used when the pointer is
        captured, so the guest cursor starts where the host pointer is)."""
        self._push(-1, -1, size)
        self._rx = self._ry = 0.0
        self.motion(x, y)

    def _push(self, px: int, py: int, size: tuple[int, int]) -> None:
        # Enough unaccelerated packets to cross the whole screen on each pushed axis.
        extent = max(size[0] if px else 0, size[1] if py else 0) + 2 * PACKET
        for _ in range(math.ceil(extent / PACKET)):
            self.queue.append(("push", px * PACKET, py * PACKET))

    def take(self, n: int = BURST) -> list[tuple]:
        """The next events to send. Far behind (a fast flick), moves are merged into
        fewer, bigger packets: the guest accelerates those, and the next edge lines the
        cursors up again."""
        if len(self.queue) > BACKLOG:
            self._merge()
        return [self.queue.popleft() for _ in range(min(n, len(self.queue)))]

    def _merge(self) -> None:
        merged: list[tuple] = []
        sx = sy = 0

        def flush():
            nonlocal sx, sy
            while sx or sy:
                cx, cy = max(-PACKET, min(PACKET, sx)), max(-PACKET, min(PACKET, sy))
                merged.append(("rel", cx, cy))
                sx, sy = sx - cx, sy - cy
        for item in self.queue:
            if item[0] == "rel":
                sx, sy = sx + item[1], sy + item[2]
            else:           # buttons and pushes keep their place: the guest clamps at edges
                flush()
                merged.append(item)
        flush()
        self.queue = deque(merged)
