#!/usr/bin/env python3
"""Generate rehome/inspect/guestfs_sigs.py from libguestfs' C header.

Rehome calls libguestfs' C library directly through ctypes when the Python
bindings are not installed (Arch's libguestfs package has none). This
script reads ``guestfs.h`` and records, for every libguestfs call Rehome
uses, the C function, its argument and return types, and the layout of its
optional-arguments struct, so no C compiler or header is needed at runtime.

    scripts/gen_guestfs_sigs.py /usr/include/guestfs.h > rehome/inspect/guestfs_sigs.py

libguestfs keeps its ABI stable (optional arguments are only ever appended,
with fixed bitmask bits), so a table made from one version works with later
ones.
"""

import re
import sys
from pathlib import Path

# The calls Rehome (and its test fixtures) make, by their Python-binding names.
USED = """
add_drive_opts blockdev_getsize64 case_sensitive_path cat chmod command cp cp_a
cryptsetup_open disk_create download exists filesize find glob_expand hivex_close
hivex_commit hivex_node_add_child hivex_node_children hivex_node_delete_child
hivex_node_get_child hivex_node_get_value hivex_node_name hivex_node_set_value
hivex_node_values hivex_open hivex_root hivex_value_key hivex_value_type
hivex_value_value inspect_get_arch inspect_get_build_id inspect_get_distro
inspect_get_hostname inspect_get_major_version inspect_get_minor_version
inspect_get_mountpoints inspect_get_osinfo inspect_get_product_name
inspect_get_product_variant inspect_get_type inspect_get_windows_current_control_set
inspect_get_windows_software_hive inspect_get_windows_system_hive
inspect_get_windows_systemroot inspect_os is_dir is_file is_symlink launch
list_devices list_filesystems list_partitions ln_sf ls mkdir mkdir_p mkfs mount
mount_options mount_ro mv part_add part_get_bootable part_get_gpt_type
part_get_mbr_id part_get_parttype part_init part_list part_set_bootable
part_set_gpt_type part_set_mbr_id part_to_dev part_to_partnum pread pread_device
pwrite_device read_file rm set_backend set_memsize set_network set_smp set_trace
shutdown syslinux umount umount_all upload version vfs_label vfs_type vfs_uuid write
""".split()

# Return types the header cannot tell apart (all "int" / "char **" in C).
# Every call returning int must be listed in exactly one of BOOL, INT, ERR.
BOOL = {"exists", "is_dir", "is_file", "is_symlink", "part_get_bootable"}
INT = {"inspect_get_major_version", "inspect_get_minor_version", "part_get_mbr_id",
       "part_to_partnum", "pwrite_device"}
ERR = set("""
add_drive_opts chmod cp cp_a cryptsetup_open disk_create download hivex_close hivex_commit
hivex_node_delete_child hivex_node_set_value hivex_open launch ln_sf mkdir mkdir_p mkfs
mount mount_options mount_ro mv part_add part_init part_set_bootable part_set_gpt_type
part_set_mbr_id rm set_backend set_memsize set_network set_smp set_trace shutdown syslinux umount
umount_all upload write
""".split())
HASH = {"inspect_get_mountpoints", "list_filesystems"}

PROTO = re.compile(r"extern GUESTFS_DLL_PUBLIC (?P<ret>[^(]*?)\s*\*?\s*\b(?P<name>guestfs_\w+) \((?P<args>[^)]*)\);")
STRUCT = re.compile(r"^struct (?P<name>guestfs_\w+) \{\n(?P<body>.*?)^\};", re.S | re.M)


def ctype(decl: str) -> str:
    decl = decl.strip()
    if decl.startswith("char *const *"):
        return "strlist"
    if decl.startswith("const char *") or decl.startswith("char *"):
        return "str"
    if decl.startswith("int64_t") or decl.startswith("uint64_t"):
        return "int64"
    if decl.startswith("int32_t") or decl.startswith("uint32_t") or decl.startswith("int "):
        return "int"
    raise ValueError(f"unsupported C type: {decl!r}")


def main(header: str) -> str:
    text = Path(header).read_text()
    protos = {}
    for m in PROTO.finditer(text):
        full = m.group(0)
        protos[m.group("name")] = (full, m.group("args"))
    structs = {m.group("name"): m.group("body") for m in STRUCT.finditer(text)}

    funcs, used_structs = {}, set()
    for py in USED:
        for cname in (f"guestfs_{py}_opts_argv", f"guestfs_{py}_argv", f"guestfs_{py}"):
            if cname in protos and "..." not in protos[cname][1]:
                break
        else:
            raise SystemExit(f"no usable C function for {py}")
        full, argstr = protos[cname]
        head = full.split(f"{cname} (")[0].replace("extern GUESTFS_DLL_PUBLIC", "").strip()
        args = [a.strip() for a in argstr.split(",")][1:]          # drop guestfs_h *g
        out_args, opts, ret = [], None, None
        i = 0
        while i < len(args):
            a = args[i]
            if a.startswith("const struct ") and a.endswith("*optargs"):
                sname = a.split()[2]
                body = structs[sname]
                # Each field follows the #define of its bitmask bit.
                fields = []
                for bit, decl in re.findall(r"# define GUESTFS_\w+_BITMASK \(UINT64_C\(1\)<<(\d+)\)\n"
                                            r"  ([^;\n]+);", body):
                    fname = re.split(r"[\s*]+", decl.strip())[-1]
                    fields.append((fname, ctype(decl), int(bit)))
                opts = fields
            elif a == "size_t *size_r":
                ret = "buf"
            elif i + 1 < len(args) and re.fullmatch(r"size_t \w+_size", args[i + 1]):
                out_args.append((a.split("*")[-1].strip(), "bufin"))
                i += 1
            else:
                out_args.append((re.split(r"[\s*]+", a)[-1], ctype(a)))
            i += 1
        if ret is None:
            if head.startswith("struct "):
                sname = head.split()[1]
                if sname.endswith("_list"):
                    ret = "structlist:" + sname[len("guestfs_"):-len("_list")]
                else:
                    ret = "struct:" + sname[len("guestfs_"):]
                used_structs.add(ret.split(":")[1])
            elif head.startswith("char **"):
                ret = "hash" if py in HASH else "strlist"
            elif head.startswith("const char *"):
                ret = "conststr"
            elif head.startswith("char *"):
                ret = "str"
            elif head.startswith("int64_t"):
                ret = "int64"
            elif head.startswith("int"):
                kinds = [k for k, group in (("bool", BOOL), ("int", INT), ("err", ERR)) if py in group]
                if len(kinds) != 1:
                    raise SystemExit(f"{py} returns int: list it in exactly one of BOOL, INT, ERR")
                ret = kinds[0]
            else:
                raise SystemExit(f"{cname}: unsupported return type {head!r}")
        funcs[py] = {"c": cname, "ret": ret, "args": out_args, "opts": opts or []}

    struct_fields = {}
    for s in sorted(used_structs):
        body = structs[f"guestfs_{s}"]
        struct_fields[s] = [(re.split(r"[\s*]+", d.strip())[-1], ctype(d))
                            for d in re.findall(r"^  ([^;\n]+);", body, re.M)]

    lines = ['"""libguestfs C signatures for the ctypes backend (rehome/inspect/guestfs_ctypes.py).',
             "",
             "Generated by scripts/gen_guestfs_sigs.py from guestfs.h; do not edit by hand.",
             '"""', "",
             "# name: (C function, return kind, [(arg, kind)], [(optarg, kind, bitmask bit)])",
             "FUNCS = {"]
    for py, f in sorted(funcs.items()):
        lines.append(f"    {py!r}: ({f['c']!r}, {f['ret']!r}, {f['args']!r}, {f['opts']!r}),")
    lines += ["}", "", "# struct name: [(field, kind)]", "STRUCTS = {"]
    for s, fields in struct_fields.items():
        lines.append(f"    {s!r}: {fields!r},")
    lines += ["}", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.write(main(sys.argv[1] if len(sys.argv) > 1 else "/usr/include/guestfs.h"))
