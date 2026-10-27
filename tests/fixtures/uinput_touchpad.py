"""A touchpad made with uinput, for the appliance test (tests/integration/test_appliance.py).

Runs as root inside the emulated appliance, which has no touchpad of its own:

    python3 uinput_touchpad.py FIFO

Each line written to FIFO is a gesture, made with real multi-touch events, so that
libinput handles them as it would a laptop's touchpad:

    swipe DX DY      one finger moves DX, DY units (40 units = 1 mm), then lifts
    tap              one finger taps (a left click with tap to click on)
    tap2             two fingers tap (a right click)
    scroll DY        two fingers move up or down
"""

import fcntl
import os
import struct
import sys
import time

# linux/uinput.h and linux/input-event-codes.h
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT, UI_SET_PROPBIT = (
    0x40045564, 0x40045565, 0x40045567, 0x4004556E)
UI_DEV_SETUP, UI_ABS_SETUP, UI_DEV_CREATE = 0x405C5503, 0x401C5504, 0x5501
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BTN_LEFT, BTN_TOOL_FINGER, BTN_TOUCH = 0x110, 0x145, 0x14A
BTN_TOOL_DOUBLETAP, BTN_TOOL_TRIPLETAP = 0x14D, 0x14E
ABS_X, ABS_Y, ABS_MT_SLOT = 0x00, 0x01, 0x2F
ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x35, 0x36, 0x39
INPUT_PROP_POINTER, INPUT_PROP_BUTTONPAD = 0x00, 0x02
BUS_USB = 0x03
WIDTH, HEIGHT, RES = 4000, 3000, 40         # 100 x 75 mm
FRAME = 0.012                               # seconds between reports, like a real touchpad


class Touchpad:
    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for ev in (EV_SYN, EV_KEY, EV_ABS):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        for key in (BTN_LEFT, BTN_TOOL_FINGER, BTN_TOUCH, BTN_TOOL_DOUBLETAP,
                    BTN_TOOL_TRIPLETAP):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)
        for prop in (INPUT_PROP_POINTER, INPUT_PROP_BUTTONPAD):
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, prop)
        for code, top in ((ABS_X, WIDTH), (ABS_Y, HEIGHT), (ABS_MT_SLOT, 1),
                          (ABS_MT_POSITION_X, WIDTH), (ABS_MT_POSITION_Y, HEIGHT),
                          (ABS_MT_TRACKING_ID, 65535)):
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
            res = RES if code in (ABS_X, ABS_Y, ABS_MT_POSITION_X, ABS_MT_POSITION_Y) else 0
            fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("<H2x6i", code, 0, 0, top, 0, 0, res))
        name = b"Rehome test touchpad"
        fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("<4H80sI", BUS_USB, 0x1234, 0x5678, 1,
                                                       name, 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.tracking = 0

    def emit(self, *events):
        data = b"".join(struct.pack("<qqHHi", 0, 0, t, c, v) for t, c, v in events)
        os.write(self.fd, data + struct.pack("<qqHHi", 0, 0, EV_SYN, 0, 0))

    def fingers(self, points, first=False):
        """Report fingers at points [(x, y)...]; [] lifts them."""
        ev = []
        for slot in (0, 1):
            ev.append((EV_ABS, ABS_MT_SLOT, slot))
            if slot < len(points):
                if first:
                    self.tracking += 1
                    ev.append((EV_ABS, ABS_MT_TRACKING_ID, self.tracking))
                x, y = points[slot]
                ev += [(EV_ABS, ABS_MT_POSITION_X, x), (EV_ABS, ABS_MT_POSITION_Y, y)]
            else:
                ev.append((EV_ABS, ABS_MT_TRACKING_ID, -1))
        n = len(points)
        ev += [(EV_KEY, BTN_TOUCH, int(n > 0)), (EV_KEY, BTN_TOOL_FINGER, int(n == 1)),
               (EV_KEY, BTN_TOOL_DOUBLETAP, int(n == 2))]
        if points:
            ev += [(EV_ABS, ABS_X, points[0][0]), (EV_ABS, ABS_Y, points[0][1])]
        self.emit(*ev)
        time.sleep(FRAME)

    def move(self, starts, dx, dy, steps=25):
        self.fingers(starts, first=True)
        for i in range(1, steps + 1):
            self.fingers([(x + dx * i // steps, y + dy * i // steps) for x, y in starts])
        self.fingers([])

    def tap(self, n):
        starts = [(2000 + 600 * i, 1500) for i in range(n)]
        self.fingers(starts, first=True)
        time.sleep(0.03)
        self.fingers([])


def main(fifo):
    pad = Touchpad()
    print("ready", flush=True)
    while True:
        with open(fifo) as f:
            for line in f:
                words = line.split()
                if not words:
                    continue
                if words[0] == "swipe":
                    dx, dy = int(words[1]), int(words[2])
                    x0 = WIDTH // 2 - dx // 2
                    y0 = HEIGHT // 2 - dy // 2
                    pad.move([(x0, y0)], dx, dy)
                elif words[0] in ("tap", "tap2"):
                    pad.tap(2 if words[0] == "tap2" else 1)
                elif words[0] == "scroll":
                    dy = int(words[1])
                    y0 = HEIGHT // 2 - dy // 2
                    pad.move([(1600, y0), (2400, y0)], 0, dy)
                print("done", line.strip(), flush=True)
                time.sleep(0.3)


if __name__ == "__main__":
    main(sys.argv[1])
