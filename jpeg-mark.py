#!/usr/bin/env python3
"""
jpeg-mark — tag JPEG files so their checksums differ from copies iCloud
Photos has previously seen (defeats iCloud's resurrection of deleted RAWs).

The tag is appended to the very end of the file, after all image data.
Image data, EXIF, and every internal structure are untouched — appending
displaces nothing. (Versions up to Rawshuck v1.0.0 instead inserted a
comment mid-file, which silently corrupted the MPF offset table Canon
JPEGs use to locate an embedded second image, and corrupted files were
silently dropped from large Apple Photos imports. Use --repair to migrate
files marked the old way: it removes the mid-file comment, restoring the
original structure exactly, and appends a fresh tail tag.)

Usage:
    python3 jpeg-mark.py /path/to/folder          # tag all JPEGs in folder
    python3 jpeg-mark.py photo1.jpg photo2.jpg    # tag specific files
    python3 jpeg-mark.py --repair /path/to/folder # migrate v1.0.0 marks
"""

import os
import struct
import sys
import uuid

JPEG_EXTS = {".jpg", ".jpeg"}
MARK = b"photo-cull:"


def new_tag():
    return f"\nphoto-cull:{uuid.uuid4()}\n".encode("ascii")


def write_atomic(path, data):
    tmp = path + ".jpeg-mark-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return None
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return str(e)


def mark(path):
    """Append tail tag. Returns error or None."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"\xff\xd8":
        return "not a JPEG (missing SOI marker)"
    if MARK in data[:65536]:
        return "marked mid-file by v1.0.0 — run with --repair to migrate"
    if MARK in data[-4096:]:
        return "already marked"
    return write_atomic(path, data + new_tag())


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
