"""libguestfs through its C library (ctypes), for systems without the Python bindings.

Arch's libguestfs package ships the C library (``libguestfs.so.0``) and
guestfish, but no Python bindings. This module provides a ``GuestFS`` class
that behaves like the bindings' ``guestfs.GuestFS(python_return_dict=True)``
for the calls Rehome makes: same method and keyword names, same return types
(str, list, dict, bytes, bool, int), and ``RuntimeError`` with libguestfs'
message on failure. The C signatures come from ``guestfs_sigs.py``, which
``scripts/gen_guestfs_sigs.py`` generates from ``guestfs.h``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading

from rehome.inspect.guestfs_sigs import FUNCS, STRUCTS

_C = {"int": ctypes.c_int, "int64": ctypes.c_int64, "str": ctypes.c_char_p,
      "strlist": ctypes.POINTER(ctypes.c_char_p)}
_lock = threading.Lock()
_lib = None
_libc = None
_structs: dict[str, type] = {}
_optstructs: dict[str, type] = {}


def load() -> ctypes.CDLL:
    """The libguestfs C library (raises OSError if it is not installed)."""
    global _lib, _libc
    with _lock:
        if _lib is None:
            name = ctypes.util.find_library("guestfs") or "libguestfs.so.0"
            lib = ctypes.CDLL(name)
            lib.guestfs_create.restype = ctypes.c_void_p
            lib.guestfs_create.argtypes = []
            lib.guestfs_close.argtypes = [ctypes.c_void_p]
            lib.guestfs_close.restype = None
            lib.guestfs_last_error.argtypes = [ctypes.c_void_p]
            lib.guestfs_last_error.restype = ctypes.c_char_p
            lib.guestfs_set_error_handler.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            lib.guestfs_set_error_handler.restype = None
            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
            _libc.free.argtypes = [ctypes.c_void_p]
            _libc.free.restype = None
            _lib = lib
    return _lib


def available() -> bool:
    try:
        load()
        return True
    except OSError:
        return False


def _struct(name: str) -> type:
    if name not in _structs:
        fields = [(f, ctypes.c_char_p if k == "str" else _C[k]) for f, k in STRUCTS[name]]
        item = type(f"guestfs_{name}", (ctypes.Structure,), {"_fields_": fields})
        lst = type(f"guestfs_{name}_list", (ctypes.Structure,),
                   {"_fields_": [("len", ctypes.c_uint32), ("val", ctypes.POINTER(item))]})
        _structs[name] = (item, lst)
    return _structs[name]


def _optstruct(py: str) -> type:
    if py not in _optstructs:
        fields = [("bitmask", ctypes.c_uint64)] + [(f, _C[k]) for f, k, _bit in FUNCS[py][3]]
        _optstructs[py] = type(f"{py}_argv", (ctypes.Structure,), {"_fields_": fields})
    return _optstructs[py]


def _bytes(v) -> bytes | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v
    return str(v).encode("utf-8", "surrogateescape")


def _text(p: int | bytes | None) -> str:
    raw = p if isinstance(p, bytes) else ctypes.string_at(p)
    return raw.decode("utf-8", "surrogateescape")


def _strlist(items) -> ctypes.Array:
    arr = (ctypes.c_char_p * (len(items) + 1))()
    for i, s in enumerate(items):
        arr[i] = _bytes(s)
    return arr


def _row(item, name: str) -> dict:
    out = {}
    for f, k in STRUCTS[name]:
        v = getattr(item, f)
        out[f] = _text(v) if k == "str" and v is not None else v
    return out


class GuestFS:
    """Drop-in for ``guestfs.GuestFS(python_return_dict=True)`` (the calls Rehome uses)."""

    def __init__(self, python_return_dict: bool = True):
        self._lib = load()
        self._g = self._lib.guestfs_create()
        if not self._g:
            raise RuntimeError("guestfs_create: could not create a libguestfs handle")
        # Errors are reported through exceptions, not printed on stderr.
        self._lib.guestfs_set_error_handler(self._g, None, None)
        self._fns: dict[str, ctypes._CFuncPtr] = {}

    def close(self) -> None:
        if self._g:
            self._lib.guestfs_close(self._g)
            self._g = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in FUNCS:
            raise AttributeError(name)
        return lambda *args, **kw: self._call(name, args, kw)

    def _fn(self, py: str):
        fn = self._fns.get(py)
        if fn is None:
            cname, ret, args, opts = FUNCS[py]
            fn = getattr(self._lib, cname)
            argtypes = [ctypes.c_void_p]
            for _n, kind in args:
                argtypes += [ctypes.c_char_p, ctypes.c_size_t] if kind == "bufin" else [_C[kind]]
            if ret == "buf":
                argtypes.append(ctypes.POINTER(ctypes.c_size_t))
            if opts:
                argtypes.append(ctypes.POINTER(_optstruct(py)))
            fn.argtypes = argtypes
            fn.restype = {"err": ctypes.c_int, "bool": ctypes.c_int, "int": ctypes.c_int,
                          "int64": ctypes.c_int64, "conststr": ctypes.c_char_p}.get(ret, ctypes.c_void_p)
            self._fns[py] = fn
        return fn

    def _error(self, py: str) -> RuntimeError:
        msg = self._lib.guestfs_last_error(self._g) if self._g else None
        return RuntimeError(msg.decode("utf-8", "replace") if msg else f"{py}: failed")

    def _call(self, py: str, args: tuple, kw: dict):
        if not self._g:
            raise RuntimeError("guestfs: the handle is closed")
        _cname, ret, sig, opts = FUNCS[py]
        if len(args) != len(sig):
            raise TypeError(f"{py}() takes {len(sig)} positional arguments ({len(args)} given)")
        unknown = set(kw) - {o[0] for o in opts}
        if unknown:
            raise TypeError(f"{py}() got unexpected keyword arguments: {', '.join(sorted(unknown))}")
        cargs: list = [self._g]
        keep: list = []                       # keep ctypes buffers alive during the call
        for (_n, kind), v in zip(sig, args):
            if kind == "bufin":
                data = _bytes(v) or b""
                cargs += [data, len(data)]
            elif kind == "str":
                cargs.append(_bytes(v))
            elif kind == "strlist":
                arr = _strlist(list(v))
                keep.append(arr)
                cargs.append(ctypes.cast(arr, ctypes.POINTER(ctypes.c_char_p)))
            else:
                cargs.append(int(v))
        size = ctypes.c_size_t()
        if ret == "buf":
            cargs.append(ctypes.byref(size))
        if opts:
            o = _optstruct(py)()
            for fname, kind, bit in opts:
                v = kw.get(fname)
                if v is None:
                    continue                  # unset, like the bindings' defaults
                if kind == "strlist":
                    arr = _strlist(list(v))
                    keep.append(arr)
                    setattr(o, fname, ctypes.cast(arr, ctypes.POINTER(ctypes.c_char_p)))
                elif kind == "str":
                    b = _bytes(v)
                    keep.append(b)
                    setattr(o, fname, b)
                else:
                    setattr(o, fname, int(v))
                o.bitmask |= 1 << bit
            keep.append(o)
            cargs.append(ctypes.byref(o))
        r = self._fn(py)(*cargs)
        return self._result(py, ret, r, size)

    def _result(self, py: str, ret: str, r, size):
        free = _libc.free
        if ret in ("err", "bool", "int", "int64"):
            if r == -1:
                raise self._error(py)
            return None if ret == "err" else bool(r) if ret == "bool" else r
        if ret == "conststr":
            if r is None:
                raise self._error(py)
            return _text(r)
        if not r:
            raise self._error(py)
        if ret == "str":
            try:
                return _text(r)
            finally:
                free(r)
        if ret == "buf":
            try:
                return ctypes.string_at(r, size.value)
            finally:
                free(r)
        if ret in ("strlist", "hash"):
            arr = ctypes.cast(r, ctypes.POINTER(ctypes.c_void_p))
            items = []
            i = 0
            while arr[i]:
                items.append(_text(arr[i]))
                free(arr[i])
                i += 1
            free(r)
            return dict(zip(items[::2], items[1::2])) if ret == "hash" else items
        kind, name = ret.split(":")
        item_t, list_t = _struct(name)
        freeer = getattr(self._lib, f"guestfs_free_{name}_list" if kind == "structlist"
                         else f"guestfs_free_{name}")
        freeer.argtypes = [ctypes.c_void_p]
        freeer.restype = None
        try:
            if kind == "struct":
                return _row(ctypes.cast(r, ctypes.POINTER(item_t)).contents, name)
            lst = ctypes.cast(r, ctypes.POINTER(list_t)).contents
            return [_row(lst.val[i], name) for i in range(lst.len)]
        finally:
            freeer(r)
