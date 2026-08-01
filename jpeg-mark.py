#!/usr/bin/env python3
"""
jpeg-mark — insert a unique comment segment into JPEG files so their
checksums differ from copies iCloud Photos has previously seen.

Image data, EXIF, and capture dates are untouched: this inserts a standard
JPEG COM (comment) segment, which every viewer ignores. No re-encoding.

Usage:
    python3 jpeg-mark.py /path/to/folder          # marks all JPEGs in folder
    python3 jpeg-mark.py photo1.jpg photo2.jpg    # marks specific files
"""

import os
import sys
import uuid

JPEG_EXTS = {".jpg", ".jpeg"}


def mark(path):
    """Insert a unique COM segment after the APPn headers. Returns error or None."""
    with open(path, "rb") as f:
        data = f.read()

    if data[:2] != b"\xff\xd8":
        return "not a JPEG (missing SOI marker)"
    # NB: the "photo-cull:" marker predates the project's name and is kept
    # stable so files marked by any version are recognised (internal format).
    if b"photo-cull:" in data[:65536]:
        return "already marked"

    # Insertion point: after SOI and any contiguous APPn segments (JFIF/EXIF
    # metadata), so parsers that expect APP0/APP1 immediately after SOI are happy.
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF and 0xE0 <= data[i + 1] <= 0xEF:
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if seglen < 2:
            return "malformed APPn segment"
        i += 2 + seglen

    comment = f"photo-cull:{uuid.uuid4()}".encode("ascii")
    segment = b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment

    tmp = path + ".jpeg-mark-tmp"
    with open(tmp, "wb") as f:
        f.write(data[:i])
        f.write(segment)
        f.write(data[i:])
    os.replace(tmp, path)  # atomic swap; original bytes preserved on any failure
    return None


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
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    targets = collect_targets(sys.argv[1:])
    if not targets:
        print("No JPEG files found.")
        sys.exit(1)
    ok = 0
    for path in targets:
        err = mark(path)
        if err:
            print(f"  ! {os.path.basename(path)}: {err}")
        else:
            ok += 1
            print(f"  ✓ {os.path.basename(path)}")
    print(f"Marked {ok} of {len(targets)} file(s).")


if __name__ == "__main__":
    main()
