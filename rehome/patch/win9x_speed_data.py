"""Byte patterns of the Windows 9x "CPU speed" fix, from patcher9x.

Generated from patcher9x's cpuspeed_*_patch_v*.h (commit b6e30d4, May 2026) by the
Rehome developers; used by rehome/patch/win9x_speed.py. Each patch is (description,
original bytes, bitmap of the bytes that must match, new bytes, bitmap of the bytes
to change); bitmaps are most significant bit first. Bytes outside the first
bitmap are addresses the system's loader relocates.

patcher9x: https://github.com/JHRobotics/patcher9x

MIT License

Copyright (c) 2022 JHRobotics

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

NDIS = [
    ("NDIS.VXD, Windows 95 and 98 FE (1 048 576 LOOPs)",
     bytes.fromhex(
         "29c9cd203f00010089c281c28000000041cd203f00010039c277f5890d00000000e421eb0050b0fe"
         "e621cd203f00010089c2cd203f00010039c274f689c2b90000100090e2fecd203f00010029d0b9e8"
         "030000f7e1a30000000089c1b80000100029d2f7f140a30000000058e621fcc3"),
     bytes.fromhex("7f3ffe787fff3f33fff3fc0fbe1f"),
     bytes.fromhex(
         "29c9cd203f00010089c281c28000000041cd203f00010039c277f5890d00000000e421eb0050b0fe"
         "e621cd203f00010089c2cd203f00010039c274f689c2b900004006e2fecd203f00010029d0750140"
         "6a0a59f7e1a30000000089c1b80000100029d2f7f140a30000000058e621fcc3"),
     bytes.fromhex("00000000000000007fffe0000000"),
     ),
    ("NDIS.VXD, Windows 98 SE (1 048 576 LOOPs)",
     bytes.fromhex(
         "29c9cd203f00010089c281c28000000041cd203f00010039c277f5890d00000000e421eb0050b0fe"
         "e6216a00cd203f00010089c2cd203f00010039c274f689c2b9000010002e89c0e2fecd203f000100"
         "29d059750909c975046a01ebcf40b9e8030000f7e1a30000000089c1b80000100029d2f7f140a300"
         "00000058e621fcc3"),
     bytes.fromhex("7f3ffe787fffcfccfdff3bfffc0fbe1f"),
     bytes.fromhex(
         "29c9cd203f00010089c281c28000000041cd203f00010039c277f5890d00000000e421eb0050b0fe"
         "e6216a00cd203f00010089c2cd203f00010039c274f689c2b9000040062e89c0e2fecd203f000100"
         "29d059750909c975046a01ebcf40b90a000000f7e1a30000000089c1b80000100029d2f7f140a300"
         "00000058e621fcc3"),
     bytes.fromhex("00000000000000001800000180000000"),
     ),
    ("NDIS.VXD with rloew's patch (10 485 760 LOOPs)",
     bytes.fromhex(
         "29c9cd203f00010089c281c28000000041cd203f00010039c277f5890d00000000e421eb0050b0fe"
         "e621cd203f00010089c2cd203f00010039c274f689c2b90000a00090e2fecd203f00010029d0b964"
         "000000f7e1a30000000089c1b80000100029d2f7f140a30000000058e621fcc3"),
     bytes.fromhex("7f3ffe787fff3f33fff3fc0fbe1f"),
     bytes.fromhex(
         "29c9cd203f00010089c281c28000000041cd203f00010039c277f5890d00000000e421eb0050b0fe"
         "e621cd203f00010089c2cd203f00010039c274f689c2b900004006e2fecd203f00010029d0750140"
         "6a0a59f7e1a30000000089c1b80000100029d2f7f140a30000000058e621fcc3"),
     bytes.fromhex("00000000000000007fffe0000000"),
     ),
    ("NDIS.386, Windows for Workgroups 3.11",
     bytes.fromhex(
         "29c9cd203f00010089c281c2800000009041cd203f00010039c277f5890da00e0000e421eb0050b0"
         "fee621cd203f00010089c2cd203f00010039c274f689c2b90000100090e2fecd203f00010029d0b9"
         "e8030000f7e1a3a80e000089c1b80000100029d2f7f140a3a40e000058e621fcc3"),
     bytes.fromhex("7f3fff3c3fff9f99fff9fe07df0fff"),
     bytes.fromhex(
         "29c9cd203f00010089c281c2800000009041cd203f00010039c277f5890da00e0000e421eb0050b0"
         "fee621cd203f00010089c2cd203f00010039c274f689c2b900004006e2fecd203f00010029d07501"
         "406a0a59f7e1a3a80e000089c1b80000100029d2f7f140a3a40e000058e621fcc3"),
     bytes.fromhex("00000000000000003ffff000000000"),
     ),
]

DRIVERS = [
    ("1 000 000 LOOPs",
     bytes.fromhex(
         "b940420f00cd203f00010089c6e2fecd203f00010029f050b840420f0029d2b910270000f7e159f7"
         "f129d2b999000000f7f1894714"),
     bytes.fromhex("ffe7f9fbffbfff"),
     bytes.fromhex(
         "b900b4c404cd203f00010089c6e2fecd203f00010029f075014050b878fa070029d2b910270000f7"
         "e159f7f1909090909090894714"),
     bytes.fromhex("780001bffeffc0"),
     ),
    ("2 000 000 LOOPs",
     bytes.fromhex(
         "b980841e00cd203f00010089c6e2fecd203f00010029f050b880841e0029d2b910270000f7e159f7"
         "f129d2b999000000f7f1894714"),
     bytes.fromhex("ffe7f9fbffbfff"),
     bytes.fromhex(
         "b900b4c404cd203f00010089c6e2fecd203f00010029f075014050b878fa070029d2b910270000f7"
         "e159f7f1909090909090894714"),
     bytes.fromhex("780001fffeffc0"),
     ),
    ("10 000 000 LOOPs, FIX95",
     bytes.fromhex(
         "b980969800cd203f00010089c6e2fecd203f00010029f050b84fff000029d2b910270000f7e159f7"
         "f1894714"),
     bytes.fromhex("ffe7f9fbffff"),
     bytes.fromhex(
         "b900b4c404cd203f00010089c6e2fecd203f00010029f0750140506a4059b8be9fde0490f7e159f7"
         "f1894714"),
     bytes.fromhex("780001fff000"),
     ),
    ("10 000 000 LOOPs, rloew's patch",
     bytes.fromhex(
         "b980969800cd203f00010089c6e2fecd203f00010029f050b84fff000029d2b910270000f7e159f7"
         "f1909090909090909090894714"),
     bytes.fromhex("ffe7f9fbffffff"),
     bytes.fromhex(
         "b900b4c404cd203f00010089c6e2fecd203f00010029f075014050b878fa070029d2b910270000f7"
         "e159f7f1909090909090894714"),
     bytes.fromhex("780001fffef000"),
     ),
    ("1 000 000 LOOPs",
     bytes.fromhex(
         "c8040000505152b940420f00cd203f0001008945fce2fecd203f0001002b45fc50b840420f0029d2"
         "b910270000f7e159f7f129d2b999000000f7f1a3041d00005a5958c9c3"),
     bytes.fromhex("fffffffffdffdff0ff"),
     bytes.fromhex(
         "c8040000505152b900b4c404cd203f0001008945fce2fecd203f0001002b45fc75014050b878fa07"
         "0029d2b910270000f7e159f7f1909090909090a3041d00005a5958c9c3"),
     bytes.fromhex("00f00000dfff7fe000"),
     ),
    ("2 000 000 LOOPs",
     bytes.fromhex(
         "c8040000505152b980841e00cd203f0001008945fce2fecd203f0001002b45fc50b880841e0029d2"
         "b910270000f7e159f7f129d2b999000000f7f1a3041d00005a5958c9c3"),
     bytes.fromhex("fffffffffdffdff0ff"),
     bytes.fromhex(
         "c8040000505152b900b4c404cd203f0001008945fce2fecd203f0001002b45fc75014050b878fa07"
         "0029d2b910270000f7e159f7f1909090909090a3041d00005a5958c9c3"),
     bytes.fromhex("00f00000ffff7fe000"),
     ),
    ("10 000 000 LOOPs, FIX95",
     bytes.fromhex(
         "c8040000505152b980969800cd203f0001008945fce2fecd203f0001002b45fc50b84fff000029d2"
         "b910270000f7e159f7f1909090909090909090a3041d00005a5958c9c3"),
     bytes.fromhex("fffffffffdfffff0ff"),
     bytes.fromhex(
         "c8040000505152b900b4c404cd203f0001008945fce2fecd203f0001002b45fc75014050b878fa07"
         "0029d2b910270000f7e159f7f1909090909090a3041d00005a5958c9c3"),
     bytes.fromhex("00f00000ffff780000"),
     ),
    ("10 000 000 LOOPs, rloew's patch",
     bytes.fromhex(
         "c8040000505152b980969800cd203f0001008945fce2fecd203f0001002b45fc50b84fff000029d2"
         "b910270000f7e159f7f1a3701300005a5958c9c3"),
     bytes.fromhex("fffffffffdffe1ff"),
     bytes.fromhex(
         "c8040000505152b900b4c404cd203f0001008945fce2fecd203f0001002b45fc750140506a4059b8"
         "be9fde0490f7e159f7f1a3701300005a5958c9c3"),
     bytes.fromhex("00f00000fff80000"),
     ),
]
