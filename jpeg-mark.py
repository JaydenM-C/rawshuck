#!/usr/bin/env python3
"""
jpeg-mark — tag JPEG files so their checksums differ from copies iCloud
Photos has previously seen (defeats iCloud's resurrection of deleted RAWs).

The tag is appended, in place, to the very end of the file, after all image
data. Two things this deliberately avoids:

  * Inserting mid-file. Versions up to Rawshuck v1.0.0 inserted a comment
    segment after the APPn run, which silently invalidated the MPF offset
    table Canon JPEGs use to locate an embedded second image. Use --repair to
    migrate files marked the old way.

  * Rewriting via a temp file. A replaced file is a new file with no extended
    attributes, which stripped the com.apple.quarantine flag Photos puts on
    the originals it exports. A folder then holds two classes of file, macOS
    splits a mixed drag into separate open-document events, and Photos' import
    pane displays only the last one — silently importing a fraction of what
    you dropped. Appending in place preserves the inode and every xattr.

--repair cannot avoid a rewrite (it has to excise bytes from the middle), so
it copies the extended attributes across explicitly.

Usage:
    python3 jpeg-mark.py /path/to/folder          # tag all JPEGs in folder
    python3 jpeg-mark.py photo1.jpg photo2.jpg    # tag specific files
    python3 jpeg-mark.py --repair /path/to/folder # migrate v1.0.0 marks
"""

import ctypes
import ctypes.util
import os
import struct
import sys
import uuid

JPEG_EXTS = {".jpg", ".jpeg"}
MARK = b"photo-cull:"


def new_tag():
    return f"\nphoto-cull:{uuid.uuid4()}\n".encode("ascii")


def copy_xattrs(src, dst):
    """Best-effort copy of extended attributes from src to dst (macOS).

    Matters because Photos stamps com.apple.quarantine on exported originals;
    a rewritten file that loses it is a different class of object to macOS's
    drag-and-drop machinery, which splits mixed drags into separate events and
    causes Photos to import only some of them. Failures are ignored: some
    attributes (com.apple.macl and friends) are kernel-protected and cannot be
    set by an ordinary process, and none of them are worth aborting over."""
    if sys.platform != "darwin":
        return
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libSystem.dylib",
                           use_errno=True)
        libc.listxattr.restype = ctypes.c_ssize_t
        libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.c_int]
        libc.getxattr.restype = ctypes.c_ssize_t
        libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                  ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.c_uint32, ctypes.c_int]
        libc.setxattr.restype = ctypes.c_int
        libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                  ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.c_uint32, ctypes.c_int]

        src_b = os.fsencode(src)
        dst_b = os.fsencode(dst)
        n = libc.listxattr(src_b, None, 0, 0)
        if n <= 0:
            return
        names = ctypes.create_string_buffer(n)
        n = libc.listxattr(src_b, ctypes.cast(names, ctypes.c_void_p), n, 0)
        if n <= 0:
            return
        for name in names.raw[:n].split(b"\0"):
            if not name:
                continue
            vlen = libc.getxattr(src_b, name, None, 0, 0, 0)
            if vlen < 0:
                continue
            value = ctypes.create_string_buffer(vlen) if vlen else None
            if vlen:
                got = libc.getxattr(src_b, name,
                                    ctypes.cast(value, ctypes.c_void_p),
                                    vlen, 0, 0)
                if got < 0:
                    continue
                vlen = got
            libc.setxattr(dst_b, name,
                          ctypes.cast(value, ctypes.c_void_p) if value else None,
                          vlen, 0, 0)
    except Exception:
        pass


def write_atomic(path, data):
    """Replace path's contents with data, preserving extended attributes.

    Only used by --repair; plain marking appends in place and needs none of
    this. Returns error or None."""
    tmp = path + ".jpeg-mark-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        copy_xattrs(path, tmp)  # while the original still exists
        os.replace(tmp, path)
        return None
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return str(e)


def scan(path):
    """(is_jpeg, legacy_mark, tail_mark), reading only the head and tail.

    v1.0.0's mark sits within the first 64 KB (a COM segment after the APPn
    run); the current one sits in the last 4 KB. Reading ~68 KB rather than
    the whole 10-25 MB keeps this fast over a folder.

    The two windows are made disjoint, because on a file smaller than 64 KB
    they would otherwise overlap and a tail tag would be misreported as a
    v1.0.0 mid-file mark."""
    size = os.path.getsize(path)
    tail_start = max(size - 4096, 0)
    with open(path, "rb") as f:
        head = f.read(65536)
        tail = b""
        if size > len(head):
            f.seek(max(tail_start, len(head)))
            tail = f.read()
    is_jpeg = head[:2] == b"\xff\xd8"  # before any splitting: tiny files
    if tail_start < len(head):  # windows overlap: split the head buffer
        head, tail = head[:tail_start], head[tail_start:] + tail
    return is_jpeg, MARK in head, MARK in tail


def mark(path):
    """Append tail tag in place. Returns error or None."""
    try:
        is_jpeg, legacy, tail = scan(path)
    except OSError as e:
        return str(e)
    if not is_jpeg:
        return "not a JPEG (missing SOI marker)"
    if legacy:
        return "marked mid-file by v1.0.0 — run with --repair to migrate"
    if tail:
        return "already marked"
    try:
        # Single small write past EOI. A torn write leaves a partial tag after
        # the end of the image, which decoders ignore; re-running appends a
        # whole one. Preferable to losing the file's extended attributes.
        with open(path, "ab") as f:
            f.write(new_tag())
    except Exception as e:
        return str(e)
    return None


def repair(path):
    """Excise a v1.0.0 mid-file COM mark (restoring all internal offsets to
    their original byte positions) and append a fresh tail tag instead."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"\xff\xd8":
        return "not a JPEG (missing SOI marker)"
    idx = data.find(MARK, 0, 65536)
    if idx < 4:
        return None if MARK in data[-4096:] else "no v1.0.0 mark found — nothing to repair"
    seg_start = idx - 4
    if data[seg_start:seg_start + 2] != b"\xff\xfe":
        return "found marker text but not a COM segment — refusing to touch"
    seglen = struct.unpack(">H", data[seg_start + 2:seg_start + 4])[0]
    if data[seg_start + 4:seg_start + 4 + len(MARK)] != MARK:
        return "COM segment structure unexpected — refusing to touch"
    excised = data[:seg_start] + data[seg_start + 2 + seglen:]
    return write_atomic(path, excised + new_tag())


def collect_targets(args):
    targets = []
    for a in args:
        a = os.path.abspath(os.path.expanduser(a))
        if os.path.isdir(a):
            for name in sorted(os.listdir(a)):
                if os.path.splitext(name)[1].lower() in JPEG_EXTS:
                    targets.append(os.path.join(a, name))
        elif os.path.isfile(a):
            targets.append(a)
        else:
            print(f"skipped (not found): {a}")
    return targets


def main():
    args = sys.argv[1:]
    do_repair = "--repair" in args
    args = [a for a in args if a != "--repair"]
    if not args:
        print(__doc__)
        sys.exit(1)
    targets = collect_targets(args)
    if not targets:
        print("No JPEG files found.")
        sys.exit(1)
    action = repair if do_repair else mark
    ok = 0
    for path in targets:
        err = action(path)
        if err:
            print(f"  ! {os.path.basename(path)}: {err}")
        else:
            ok += 1
            print(f"  ✓ {os.path.basename(path)}")
    print(f"{'Repaired' if do_repair else 'Marked'} {ok} of {len(targets)} file(s).")


if __name__ == "__main__":
    main()
