import re
from pathlib import Path

import pytest

from rehome.inspect import guestfs_ctypes
from rehome.inspect.guestfs_sigs import FUNCS

ROOT = Path(__file__).resolve().parents[2]
# Code that holds libguestfs handles in a variable called "g" (the GUI and
# CLI use "g" for widget/argparse groups, so they are left out).
SCANNED = ["rehome/inspect", "rehome/patch", "rehome/disk", "rehome/vm", "rehome/importer.py",
           "rehome/media", "tests/fixtures", "tests/integration"]


def test_every_libguestfs_call_has_a_c_signature():
    called = set()
    for part in SCANNED:
        p = ROOT / part
        for f in ([p] if p.is_file() else p.rglob("*.py")):
            called |= set(re.findall(r"\bg\.([a-z_0-9]+)\(", f.read_text()))
    missing = called - set(FUNCS) - {"close"}
    assert not missing, ("libguestfs calls without a ctypes signature; add them to USED in "
                         f"scripts/gen_guestfs_sigs.py and regenerate: {sorted(missing)}")


def test_int_results_are_classified():
    kinds = {v[1] for v in FUNCS.values()}
    assert kinds <= {"err", "bool", "int", "int64", "str", "conststr", "strlist", "hash", "buf",
                     "struct:version", "structlist:partition", "structlist:hivex_node",
                     "structlist:hivex_value"}
    assert FUNCS["inspect_get_major_version"][1] == "int"
    assert FUNCS["exists"][1] == "bool" and FUNCS["mount_ro"][1] == "err"
    assert FUNCS["inspect_get_mountpoints"][1] == "hash"


@pytest.mark.skipif(not guestfs_ctypes.available(), reason="libguestfs is not installed")
def test_handle_basics_without_launching():
    g = guestfs_ctypes.GuestFS()
    try:
        v = g.version()
        assert v["major"] == 1 and isinstance(v["extra"], str)
        assert g.set_memsize(512) is None
        with pytest.raises(TypeError):
            g.add_drive_opts("x", nonsense=1)
        with pytest.raises(AttributeError):
            g.no_such_call()
        with pytest.raises(RuntimeError):
            g.inspect_os()                    # not launched: libguestfs reports an error
    finally:
        g.close()
    with pytest.raises(RuntimeError, match="closed"):
        g.version()
