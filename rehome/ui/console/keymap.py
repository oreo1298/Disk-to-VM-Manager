"""Linux evdev key codes -> QEMU "qnum" key numbers (XT set 1, 0xE0xx -> 0x80|xx).

GTK reports hardware keycodes on Linux as evdev code + 8 (both on X11 and
Wayland). QEMU's D-Bus display takes qnum codes. For the first 88 keys the
two numbering schemes coincide; the rest are listed explicitly (from the
keycodemapdb tables used by QEMU, spice-gtk and gtk-vnc).
"""

from __future__ import annotations

EXTRA = {
    89: 0x73,    # KEY_RO
    90: 0x78,    # KEY_KATAKANA
    91: 0x77,    # KEY_HIRAGANA
    92: 0x79,    # KEY_HENKAN
    93: 0x70,    # KEY_KATAKANAHIRAGANA
    94: 0x7B,    # KEY_MUHENKAN
    95: 0x5C,    # KEY_KPJPCOMMA
    96: 0x9C,    # KEY_KPENTER
    97: 0x9D,    # KEY_RIGHTCTRL
    98: 0xB5,    # KEY_KPSLASH
    99: 0xB7,    # KEY_SYSRQ / Print
    100: 0xB8,   # KEY_RIGHTALT
    102: 0xC7,   # KEY_HOME
    103: 0xC8,   # KEY_UP
    104: 0xC9,   # KEY_PAGEUP
    105: 0xCB,   # KEY_LEFT
    106: 0xCD,   # KEY_RIGHT
    107: 0xCF,   # KEY_END
    108: 0xD0,   # KEY_DOWN
    109: 0xD1,   # KEY_PAGEDOWN
    110: 0xD2,   # KEY_INSERT
    111: 0xD3,   # KEY_DELETE
    113: 0xA0,   # KEY_MUTE
    114: 0xAE,   # KEY_VOLUMEDOWN
    115: 0xB0,   # KEY_VOLUMEUP
    116: 0xDE,   # KEY_POWER
    117: 0x59,   # KEY_KPEQUAL
    119: 0xC6,   # KEY_PAUSE
    121: 0x7E,   # KEY_KPCOMMA
    122: 0xF2,   # KEY_HANGEUL
    123: 0xF1,   # KEY_HANJA
    124: 0x7D,   # KEY_YEN
    125: 0xDB,   # KEY_LEFTMETA
    126: 0xDC,   # KEY_RIGHTMETA
    127: 0xDD,   # KEY_COMPOSE (Menu)
    128: 0xE8,   # KEY_STOP
    140: 0xA1,   # KEY_CALC
    142: 0xDF,   # KEY_SLEEP
    143: 0xE3,   # KEY_WAKEUP
    155: 0xEC,   # KEY_MAIL
    156: 0xE6,   # KEY_BOOKMARKS
    158: 0xEA,   # KEY_BACK
    159: 0xE9,   # KEY_FORWARD
    163: 0x99,   # KEY_NEXTSONG
    164: 0xA2,   # KEY_PLAYPAUSE
    165: 0x90,   # KEY_PREVIOUSSONG
    166: 0xA4,   # KEY_STOPCD
    172: 0xB2,   # KEY_HOMEPAGE
    173: 0xE7,   # KEY_REFRESH
    217: 0xE5,   # KEY_SEARCH
    226: 0xED,   # KEY_MEDIA
}


def evdev_to_qnum(evdev: int) -> int | None:
    if 1 <= evdev <= 83 or evdev in (86, 87, 88):
        return evdev
    return EXTRA.get(evdev)


def gdk_keycode_to_qnum(hw_keycode: int) -> int | None:
    """GDK hardware keycode (evdev + 8 on Linux) -> qnum."""
    return evdev_to_qnum(hw_keycode - 8)


# A few qnums the console needs by name.
QNUM = {"ctrl": 0x1D, "alt": 0x38, "delete": 0xD3, "backspace": 0x0E, "tab": 0x0F,
        "shift": 0x2A, "meta": 0xDB, "print": 0xB7, "sysrq": 0x54, "esc": 0x01,
        **{f"f{i}": 0x3A + i for i in range(1, 11)}, "f11": 0x57, "f12": 0x58}
