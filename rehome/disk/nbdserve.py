"""A minimal read-only NBD server for one file descriptor (fallback for nbdkit).

Physical disks are opened by the privileged helper, which hands Rehome a
read-only file descriptor. Offline tools (libguestfs, qemu-img) need an NBD
URI for it. nbdkit does this best, but some distributions (Arch) do not
package it, so Rehome ships this small server: the fixed-newstyle handshake
(NBD_OPT_GO/INFO/EXPORT_NAME/LIST/ABORT), simple replies, and read, flush and
disconnect commands. Every write-type command is refused with EPERM, so the
export cannot change the source.

Standard library only: it runs as ``python3 -I nbdserve.py --fd N --socket S``.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import socket
import struct
import sys
import threading

NBDMAGIC = 0x4E42444D41474943
IHAVEOPT = 0x49484156454F5054
REPLY_MAGIC = 0x3E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY_MAGIC = 0x67446698

FLAG_FIXED_NEWSTYLE, FLAG_NO_ZEROES = 1, 2
C_FLAG_NO_ZEROES = 2
OPT_EXPORT_NAME, OPT_ABORT, OPT_LIST, OPT_INFO, OPT_GO = 1, 2, 3, 6, 7
REP_ACK, REP_SERVER, REP_INFO = 1, 2, 3
REP_ERR_UNSUP, REP_ERR_INVALID = 0x80000001, 0x80000003
INFO_EXPORT, INFO_BLOCK_SIZE = 0, 3
TFLAG_HAS_FLAGS, TFLAG_READ_ONLY, TFLAG_SEND_FLUSH, TFLAG_CAN_MULTI_CONN = 1, 2, 4, 256
TFLAGS = TFLAG_HAS_FLAGS | TFLAG_READ_ONLY | TFLAG_SEND_FLUSH | TFLAG_CAN_MULTI_CONN
CMD_READ, CMD_WRITE, CMD_DISC, CMD_FLUSH = 0, 1, 2, 3
EPERM, EIO, EINVAL = 1, 5, 22
MAX_REQUEST = 32 * 1024 * 1024


class Closed(Exception):
    pass


def _recv(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise Closed()
        buf += chunk
    return bytes(buf)


def _option_reply(conn, opt: int, rtype: int, data: bytes = b"") -> None:
    conn.sendall(struct.pack(">QIII", REPLY_MAGIC, opt, rtype, len(data)) + data)


def _negotiate(conn, size: int) -> bool:
    """Fixed-newstyle handshake. Returns True when transmission starts."""
    conn.sendall(struct.pack(">QQH", NBDMAGIC, IHAVEOPT, FLAG_FIXED_NEWSTYLE | FLAG_NO_ZEROES))
    (cflags,) = struct.unpack(">I", _recv(conn, 4))
    no_zeroes = bool(cflags & C_FLAG_NO_ZEROES)
    while True:
        magic, opt, length = struct.unpack(">QII", _recv(conn, 16))
        if magic != IHAVEOPT or length > 65536:
            return False
        data = _recv(conn, length) if length else b""
        if opt == OPT_EXPORT_NAME:
            conn.sendall(struct.pack(">QH", size, TFLAGS) + (b"" if no_zeroes else bytes(124)))
            return True
        if opt == OPT_ABORT:
            _option_reply(conn, opt, REP_ACK)
            return False
        if opt == OPT_LIST:
            _option_reply(conn, opt, REP_SERVER, struct.pack(">I", 0))
            _option_reply(conn, opt, REP_ACK)
        elif opt in (OPT_INFO, OPT_GO):
            if len(data) < 6:
                _option_reply(conn, opt, REP_ERR_INVALID)
                continue
            _option_reply(conn, opt, REP_INFO, struct.pack(">HQH", INFO_EXPORT, size, TFLAGS))
            _option_reply(conn, opt, REP_INFO,
                          struct.pack(">HIII", INFO_BLOCK_SIZE, 1, 4096, MAX_REQUEST))
            _option_reply(conn, opt, REP_ACK)
            if opt == OPT_GO:
                return True
        else:
            # STARTTLS, structured replies, meta contexts, extended headers...
            _option_reply(conn, opt, REP_ERR_UNSUP)


def _simple_reply(conn, cookie: int, error: int, data: bytes = b"") -> None:
    conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, error, cookie) + data)


def _transmit(conn, fd: int, size: int) -> None:
    while True:
        magic, _flags, cmd, cookie, offset, length = struct.unpack(">IHHQQI", _recv(conn, 28))
        if magic != REQUEST_MAGIC:
            return
        if cmd == CMD_READ:
            if length > MAX_REQUEST or offset + length > size:
                _simple_reply(conn, cookie, EINVAL)
                continue
            try:
                data = os.pread(fd, length, offset)
            except OSError:
                _simple_reply(conn, cookie, EIO)
                continue
            if len(data) < length:          # short read at the end of a growing file
                data += bytes(length - len(data))
            _simple_reply(conn, cookie, 0, data)
        elif cmd == CMD_DISC:
            return
        elif cmd == CMD_FLUSH:
            _simple_reply(conn, cookie, 0)
        else:
            if cmd == CMD_WRITE:
                _recv(conn, length)         # discard the payload
            _simple_reply(conn, cookie, EPERM)


def _serve_client(conn, fd: int, size: int) -> None:
    with conn:
        try:
            if _negotiate(conn, size):
                _transmit(conn, fd, size)
        except (Closed, OSError, struct.error):
            pass


def _die_with_parent() -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, signal.SIGTERM)       # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def serve(fd: int, sock_path: str) -> None:
    size = os.lseek(fd, 0, os.SEEK_END)
    tmp = sock_path + ".tmp"
    for p in (tmp, sock_path):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o077)
    try:
        srv.bind(tmp)
    finally:
        os.umask(old)
    srv.listen(16)
    os.rename(tmp, sock_path)               # appears only once it accepts connections
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=_serve_client, args=(conn, fd, size), daemon=True).start()
    finally:
        try:
            os.unlink(sock_path)
        except OSError:
            pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Serve a file descriptor read-only over NBD.")
    p.add_argument("--fd", type=int, required=True)
    p.add_argument("--socket", required=True)
    args = p.parse_args(argv)
    _die_with_parent()
    serve(args.fd, args.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
