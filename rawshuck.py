#!/usr/bin/env python3
# Rawshuck — cull RAW+JPEG shoots before import.
# Copyright (C) 2026  Jayden Macklin-Cordes
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See the LICENSE file for details.
"""

Usage:
    python3 rawshuck.py                     # native picker (folder or files)
    python3 rawshuck.py /path/to/folder     # review a whole folder
    python3 rawshuck.py a.jpg b.jpg c.cr3   # review specific files

Opens your default browser as the UI. All file operations happen locally in
this script. On macOS, deletions go to the Trash via Finder (recoverable),
and RAW/HEIC/TIFF previews are converted with the built-in `sips` tool.

The software is optimised for sorting sets of RAW+compressed images,
for example Canon's CR3 format, which saves a JPEG+RAW, although it handles
lone RAWs and other file types too. For each JPEG+RAW (or whatever compressed
image+RAW format you use), pick one of three fates:
retain both, retain compressed image (e.g. JPEG) only, or delete all together.
For unpaired images (lone RAWs, lone JPEG/HEIC/PNG/TIF etc.), either keep or delete.

When ready, click 'commit' to delete/shuck the unwanted RAWs before importing 
to Apple Photos or your photo library software of choice.

Keys: Space = keep compressed image+RAW · J = compressed image only · Delete/Backspace = trash
      arrows = navigate · U = clear · N = next undecided
"""

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "1.0.0"

# Formats browsers render natively — served as-is.
DISPLAY_EXTS_DIRECT = {"jpg", "jpeg", "png", "webp", "gif"}
# Formats converted to JPEG for display (macOS `sips`; originals untouched).
DISPLAY_EXTS_CONVERT = {"heic", "heif", "tif", "tiff"}
DISPLAY_EXTS = DISPLAY_EXTS_DIRECT | DISPLAY_EXTS_CONVERT
RAW_EXTS = {"cr3", "cr2", "crw", "dng", "arw", "nef", "nrw", "raf", "orf", "rw2", "pef", "srw"}
# When several display files share a basename, the RAW pairs with the first by this order.
DISPLAY_PRIORITY = ["jpg", "jpeg", "heic", "heif", "png", "webp", "tif", "tiff", "gif"]
MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif", "tif": "image/tiff",
    "tiff": "image/tiff", "heic": "image/heic", "heif": "image/heif",
}

SOURCE = None   # {"mode": "folder", "folder": path} | {"mode": "files", "files": [paths]}
ITEMS = []      # [{id, name, img: path|None, raw: path|None}]
KNOWN = {}      # id -> item

PREVIEW_DIR = tempfile.mkdtemp(prefix="rawshuck-previews-")
atexit.register(lambda: shutil.rmtree(PREVIEW_DIR, ignore_errors=True))


def ext_of(path):
    return os.path.splitext(path)[1][1:].lower()


# ---------------------------------------------------------------- scanning

def gather_paths():
    if SOURCE["mode"] == "folder":
        folder = SOURCE["folder"]
        return [os.path.join(folder, n) for n in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, n))]
    return [p for p in SOURCE["files"] if os.path.isfile(p)]


def scan():
    """Build ITEMS/KNOWN. An item is a reviewable photo: a display image
    (JPEG/HEIC/PNG/TIFF/…), optionally paired with a RAW of the same basename
    in the same directory — or a lone RAW with no display counterpart."""
    global ITEMS, KNOWN
    disp, raws = {}, {}
    for p in gather_paths():
        e = ext_of(p)
        key = (os.path.dirname(p), os.path.splitext(os.path.basename(p))[0].lower())
        if e in DISPLAY_EXTS:
            disp.setdefault(key, []).append(p)
        elif e in RAW_EXTS:
            raws[key] = p

    items = []
    for key, paths in disp.items():
        paths.sort(key=lambda p: DISPLAY_PRIORITY.index(ext_of(p))
                   if ext_of(p) in DISPLAY_PRIORITY else 99)
        for i, p in enumerate(paths):
            items.append({"img": p, "raw": raws.get(key) if i == 0 else None})
    for key, rp in raws.items():
        if key not in disp:
            items.append({"img": None, "raw": rp})

    items.sort(key=lambda it: os.path.basename(it["img"] or it["raw"]).lower())
    ITEMS, KNOWN = [], {}
    for i, it in enumerate(items):
        it["id"] = i
        it["name"] = os.path.basename(it["img"] or it["raw"])
        ITEMS.append(it)
        KNOWN[i] = it


def source_label():
    if SOURCE["mode"] == "folder":
        return os.path.basename(SOURCE["folder"].rstrip("/")) or SOURCE["folder"]
    return f"{len(SOURCE['files'])} selected file(s)"


def file_size(path):
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def item_json(it):
    return {
        "id": it["id"],
        "name": it["name"],
        "raw": os.path.basename(it["raw"]) if it["raw"] else None,
        "rawOnly": it["img"] is None,
        "markable": bool(it["img"]) and ext_of(it["img"]) in ("jpg", "jpeg"),
        "imgSize": file_size(it["img"]),
        "rawSize": file_size(it["raw"]),
    }


# ---------------------------------------------------------------- previews

def preview_for(item):
    """Return (path, mimetype) of a browser-displayable preview, or (None, None).
    Originals are never modified; conversions are cached in a temp dir."""
    src = item["img"] or item["raw"]
    e = ext_of(src)
    if item["img"] and e in DISPLAY_EXTS_DIRECT:
        return src, MIME[e]
    if sys.platform != "darwin":
        # Best effort off-macOS: serve the bytes and let the browser try.
        if item["img"]:
            return src, MIME.get(e, "application/octet-stream")
        return None, None  # can't render RAW without a converter
    tag = hashlib.sha1(f"{src}:{os.path.getmtime(src)}".encode()).hexdigest()[:16]
    out = os.path.join(PREVIEW_DIR, tag + ".jpg")
    if not os.path.exists(out):
        try:
            r = subprocess.run(["sips", "-s", "format", "jpeg", src, "--out", out],
                               capture_output=True, timeout=120)
            if r.returncode != 0 or not os.path.exists(out):
                return None, None
        except Exception:
            return None, None
    return out, "image/jpeg"


# ---------------------------------------------------------------- trashing

TRASH_SCRIPT = """on run argv
set fl to {}
repeat with p in argv
set end of fl to POSIX file (p as string)
end repeat
tell application "Finder" to delete fl
end run"""


def trash_files(paths):
    """Move files to Trash. Returns list of {name, error} failures."""
    if sys.platform == "darwin":
        for i in range(0, len(paths), 100):
            chunk = paths[i:i + 100]
            try:
                r = subprocess.run(["osascript", "-e", TRASH_SCRIPT, *chunk],
                                   capture_output=True, text=True, timeout=300)
                if r.returncode != 0:
                    sys.stderr.write(r.stderr)
            except Exception as e:
                sys.stderr.write(str(e) + "\n")
    else:
        # Portable fallback: move into a .rawshuck-trash subfolder next to each file.
        for p in paths:
            tdir = os.path.join(os.path.dirname(p), ".rawshuck-trash")
            os.makedirs(tdir, exist_ok=True)
            try:
                os.replace(p, os.path.join(tdir, os.path.basename(p)))
            except Exception as e:
                sys.stderr.write(f"{p}: {e}\n")
    return [{"name": os.path.basename(p), "error": "still exists after delete"}
            for p in paths if os.path.exists(p)]


# ---------------------------------------------------------------- marking

def mark_jpeg(path):
    """Insert a unique JPEG COM (comment) segment so the file's checksum
    differs from copies iCloud Photos has previously seen — otherwise iCloud's
    server-side dedupe re-pairs reimported JPEGs with their deleted RAWs.
    Image data and EXIF are untouched; no re-encoding. Returns error or None."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"\xff\xd8":
        return "not a JPEG (missing SOI marker)"
    # NB: the "photo-cull:" marker predates the project's name and is kept
    # stable so files marked by any version are recognised (internal format).
    if b"photo-cull:" in data[:65536]:
        return None  # already marked — nothing to do
    # Insertion point: after SOI and any contiguous APPn (JFIF/EXIF) segments.
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF and 0xE0 <= data[i + 1] <= 0xEF:
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if seglen < 2:
            return "malformed APPn segment"
        i += 2 + seglen
    comment = f"photo-cull:{uuid.uuid4()}".encode("ascii")
    segment = b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment
    tmp = path + ".jpeg-mark-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data[:i])
            f.write(segment)
            f.write(data[i:])
        os.replace(tmp, path)  # atomic swap
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return str(e)
    return None


# ---------------------------------------------------------------- pickers (macOS)

def _osascript(*lines, timeout=600):
    try:
        r = subprocess.run(["osascript"] + [a for l in lines for a in ("-e", l)],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def choose_source_mac():
    """Native dialog: whole folder or specific files. Returns SOURCE dict or None."""
    choice = _osascript(
        'button returned of (display dialog "Review a whole folder, or pick specific image files?" '
        'with title "Rawshuck" buttons {"Cancel", "Pick files…", "Choose folder…"} '
        'default button "Choose folder…")')
    if not choice:
        return None
    if choice.startswith("Choose folder"):
        out = _osascript('POSIX path of (choose folder with prompt "Choose your photo folder")')
        return {"mode": "folder", "folder": os.path.abspath(out)} if out else None
    out = _osascript(
        'set fl to choose file with prompt "Pick the image files to review" '
        'with multiple selections allowed\n'
        'set acc to ""\n'
        'repeat with f in fl\n'
        'set acc to acc & POSIX path of f & linefeed\n'
        'end repeat\n'
        'return acc')
    if not out:
        return None
    files = [os.path.abspath(l) for l in out.splitlines() if l.strip()]
    return {"mode": "files", "files": files} if files else None


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass  # keep the terminal quiet

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # -- GET
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                self.send_bytes(INDEX_HTML.encode(), "text/html; charset=utf-8")
            elif path == "/api/list":
                scan()
                self.send_json({
                    "source": source_label(),
                    "canSwitch": sys.platform == "darwin",
                    "items": [item_json(it) for it in ITEMS],
                })
            elif path.startswith("/img/"):
                try:
                    item = KNOWN.get(int(path[len("/img/"):]))
                except ValueError:
                    item = None
                if item is None:
                    self.send_json({"error": "unknown item"}, 404)
                    return
                pv, mime = preview_for(item)
                if pv is None:
                    self.send_json({"error": "preview unavailable"}, 415)
                    return
                with open(pv, "rb") as f:
                    self.send_bytes(f.read(), mime, cache=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- POST
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json({"error": "bad json"}, 400)
            return

        if path == "/api/commit":
            self.handle_commit(body)
        elif path == "/api/choose":
            if sys.platform != "darwin":
                self.send_json({"error": "Switching sources needs macOS. "
                                         "Restart with a path on the command line instead."})
                return
            src = choose_source_mac()
            if src is None:
                self.send_json({"cancelled": True})
            else:
                global SOURCE
                SOURCE = src
                self.send_json({"ok": True})
        elif path == "/api/quit":
            self.send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_json({"error": "not found"}, 404)

    def handle_commit(self, body):
        decisions = body.get("decisions", [])
        do_mark = bool(body.get("mark", True))

        # 1. Mark kept JPEGs first (prevents iCloud re-pairing on reimport).
        mark_failures, marked = [], 0
        skip_raw_ids = set()
        if do_mark:
            for d in decisions:
                it = KNOWN.get(d.get("id"))
                if (it and d.get("decision") == "jpeg" and it["raw"] and it["img"]
                        and ext_of(it["img"]) in ("jpg", "jpeg")):
                    err = mark_jpeg(it["img"])
                    if err:
                        mark_failures.append({"name": it["name"], "error": err})
                        # 2. Safety interlock: an unmarked JPEG + deleted RAW is
                        #    the exact setup for a silent iCloud resurrection.
                        skip_raw_ids.add(it["id"])
                    else:
                        marked += 1

        # 3. Build the deletion list from decisions.
        to_delete, kept_back = [], []
        for d in decisions:
            it = KNOWN.get(d.get("id"))
            if not it:
                continue
            dec = d.get("decision")
            if dec == "jpeg" and it["raw"]:
                if it["id"] in skip_raw_ids:
                    kept_back.append(os.path.basename(it["raw"]))
                else:
                    to_delete.append(it["raw"])
            elif dec == "trash":
                if it["img"]:
                    to_delete.append(it["img"])
                if it["raw"]:
                    to_delete.append(it["raw"])

        # 4. Trash.
        failures = trash_files(to_delete) if to_delete else []
        self.send_json({
            "requested": len(to_delete) + len(kept_back),
            "deleted": len(to_delete) - len(failures),
            "failures": failures,
            "marked": marked,
            "mark_failures": mark_failures,
            "kept_back": kept_back,
            "trash": sys.platform == "darwin",
        })


# ---------------------------------------------------------------- UI

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Rawshuck</title>
<style>
  :root {
    --bg: #16181c; --panel: #1f2228; --border: #32363e;
    --text: #e8eaed; --dim: #9aa0a8;
    --green: #4caf7d; --blue: #5b9bd5; --red: #e06055; --gray: #6b7078;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: flex; flex-direction: column; overflow: hidden;
    -webkit-user-select: none; user-select: none;
  }
  header {
    display: flex; align-items: center; gap: 14px;
    padding: 8px 14px; background: var(--panel); border-bottom: 1px solid var(--border);
    flex-wrap: wrap; min-height: 46px;
  }
  header .title { font-weight: 600; }
  header .spacer { flex: 1; }
  #sourceName { color: var(--dim); font-size: 15px; }
  #sourceName.switchable { cursor: pointer; border-bottom: 1px dotted var(--dim); }
  #sourceName.switchable:hover { color: var(--text); border-bottom-color: var(--text); }
  .counts { display: flex; gap: 12px; font-size: 15px; color: var(--dim); }
  .counts b { color: var(--text); }
  .counts .c-both b { color: var(--green); }
  .counts .c-jpeg b { color: var(--blue); }
  .counts .c-trash b { color: var(--red); }
  button {
    background: #2a2e36; color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 14px; font-size: 15px; cursor: pointer;
  }
  button:hover { background: #343943; }
  button:disabled { opacity: 0.4; cursor: default; }
  button.primary { background: var(--green); border-color: var(--green); color: #0d1512; font-weight: 600; }
  button.primary:hover { background: #5cc08e; }
  button.danger { background: var(--red); border-color: var(--red); color: #fff; font-weight: 600; }
  button.danger:hover { background: #e97b72; }
  #viewer {
    flex: 1; position: relative; overflow: hidden; background: #0c0d10;
    cursor: grab; touch-action: none;
  }
  #viewer.dragging { cursor: grabbing; }
  #photo {
    position: absolute; top: 0; left: 0; transform-origin: 0 0;
    will-change: transform; pointer-events: none;
  }
  #noPreview {
    position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; color: var(--dim); font-size: 17px; pointer-events: none;
  }
  .overlay-badge {
    position: absolute; top: 12px; left: 12px; display: flex; gap: 8px; align-items: center;
    pointer-events: none; z-index: 5;
  }
  .badge {
    padding: 4px 10px; border-radius: 5px; font-size: 14px; font-weight: 600;
    background: rgba(0,0,0,0.55); backdrop-filter: blur(4px);
  }
  .badge.decision-both { color: var(--green); border: 1px solid var(--green); }
  .badge.decision-jpeg { color: var(--blue); border: 1px solid var(--blue); }
  .badge.decision-trash { color: var(--red); border: 1px solid var(--red); }
  .badge.decision-none { color: var(--gray); border: 1px solid var(--gray); }
  .badge.rawinfo { color: var(--dim); border: 1px solid var(--border); }
  #zoomInfo {
    position: absolute; top: 12px; right: 12px; z-index: 5; pointer-events: none;
    font-size: 14px; color: var(--dim); background: rgba(0,0,0,0.55);
    padding: 4px 10px; border-radius: 5px;
  }
  footer {
    background: var(--panel); border-top: 1px solid var(--border);
    padding: 10px 14px; display: flex; flex-direction: column; gap: 8px;
  }
  .controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: center; }
  .decide { padding: 9px 18px; font-size: 16px; border-radius: 8px; border-width: 2px; }
  .decide .key {
    display: inline-block; background: rgba(255,255,255,0.12); border-radius: 4px;
    padding: 1px 7px; font-size: 12.5px; margin-left: 8px; font-family: ui-monospace, monospace;
  }
  .decide.active-both { border-color: var(--green); color: var(--green); }
  .decide.active-jpeg { border-color: var(--blue); color: var(--blue); }
  .decide.active-trash { border-color: var(--red); color: var(--red); }
  .navbtn { padding: 9px 14px; font-size: 18.5px; }
  .hint { text-align: center; font-size: 14px; color: var(--dim); }
  #infobar {
    display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
    padding: 4px 14px; background: var(--panel); border-top: 1px solid var(--border);
    font-size: 13px; color: var(--dim);
  }
  #infobar a { color: var(--dim); }
  #infobar a:hover { color: var(--text); }
  #filename { text-align: center; font-size: 15px; color: var(--dim); }
  #filename b { color: var(--text); font-weight: 500; }
  #progressbar { display: flex; height: 5px; border-radius: 3px; overflow: hidden; background: #2a2e36; }
  #progressbar div { flex: 1; min-width: 0; cursor: pointer; }
  #progressbar div.d-both { background: var(--green); }
  #progressbar div.d-jpeg { background: var(--blue); }
  #progressbar div.d-trash { background: var(--red); }
  #progressbar div.d-none { background: transparent; }
  #progressbar div.current { outline: 1px solid #fff; outline-offset: -1px; }
  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 50;
    display: flex; align-items: center; justify-content: center;
  }
  .modal {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 24px; max-width: 500px; width: 92%; max-height: 85vh; overflow-y: auto;
  }
  .modal h2 { font-size: 19.5px; margin-bottom: 12px; }
  .modal p { font-size: 16px; line-height: 1.55; color: var(--dim); margin-bottom: 10px; }
  .modal p b { color: var(--text); }
  .modal .warn { color: #e0b055; font-weight: 500; }
  .modal .good { color: var(--green); font-weight: 500; }
  .modal .btnrow { display: flex; gap: 10px; justify-content: flex-end; margin-top: 18px; }
  .hidden { display: none !important; }
</style>
</head>
<body>

<header>
  <span class="title">Rawshuck</span>
  <span id="sourceName" title="Click to review a different folder or set of files"></span>
  <span class="spacer"></span>
  <div class="counts" id="counts"></div>
  <button id="btnNextUndecided">Next undecided <span style="opacity:.6;font-size:12.5px">N</span></button>
  <button id="btnCommit" class="primary">Commit…</button>
  <button id="btnQuit" title="Stop the app">Quit</button>
</header>

<div id="viewer">
  <img id="photo" alt="">
  <div id="noPreview" class="hidden">Preview unavailable for this file</div>
  <div class="overlay-badge" id="badges">
    <span class="badge" id="decisionBadge"></span>
    <span class="badge rawinfo" id="rawBadge"></span>
  </div>
  <div id="zoomInfo"></div>
</div>

<footer id="footer">
  <div id="filename"></div>
  <div id="progressbar"></div>
  <div class="controls">
    <button class="navbtn" id="btnPrev" title="Previous (&#8592;)">&#8592;</button>
    <button class="decide" id="btnBoth"></button>
    <button class="decide" id="btnJpeg">Image only<span class="key">J</span></button>
    <button class="decide" id="btnTrash">Delete<span class="key">&#9003;</span></button>
    <button class="navbtn" id="btnNext" title="Next (&#8594;)">&#8594;</button>
  </div>
  <div class="hint">&#8592; &#8594; navigate &middot; U clear choice &middot; N next undecided &middot; scroll to zoom &middot; drag to pan &middot; double-click 100%/fit</div>
</footer>

<div id="infobar">
  <span>Usage instructions: see the <a href="https://github.com/JaydenM-C/rawshuck" target="_blank" rel="noopener">README</a>. Read more on <a href="https://macklin-cordes.com/posts/2026/08/rawshuck/" target="_blank" rel="noopener">the blog</a>.</span>
  <span>© Jayden Macklin-Cordes (<a href="https://github.com/JaydenM-C/rawshuck/blob/main/LICENSE" target="_blank" rel="noopener">GPL v3 licence</a>)</span>
</div>

<div class="modal-backdrop hidden" id="modalBackdrop">
  <div class="modal" id="modalBox"></div>
</div>

<script>
"use strict";

let photos = [];   // { id, name, raw, rawOnly, markable, decision }
let idx = 0;
let committedAny = false;
let quitting = false;

const $ = (id) => document.getElementById(id);
const imgURL = (i) => "/img/" + photos[i].id;

// ---------- load ----------
async function loadList() {
  const r = await fetch("/api/list");
  const data = await r.json();
  photos = data.items.map((p) => ({ ...p, decision: null }));
  const src = $("sourceName");
  src.textContent = "📁 " + data.source;
  src.classList.toggle("switchable", !!data.canSwitch);
  idx = 0;
  buildProgressBar();
  if (photos.length === 0) {
    showModal(`<h2>No images found</h2>
      <p>No reviewable image or RAW files here (subfolders are not scanned).
      ${data.canSwitch ? "Click the source name in the header to pick a different folder or files." :
        "Restart the app with a different path."}</p>
      <div class="btnrow"><button onclick="hideModal()">OK</button></div>`);
    updateUI();
    return;
  }
  showPhoto(0);
}

// ---------- image display, zoom & pan ----------
const viewer = $("viewer");
const img = $("photo");
let scale = 1, fitScale = 1, tx = 0, ty = 0;
let imgW = 0, imgH = 0;
let loadToken = 0;

function showPhoto(i) {
  if (!photos.length) return;
  idx = Math.max(0, Math.min(i, photos.length - 1));
  const token = ++loadToken;
  img.onload = () => {
    if (token !== loadToken) return;
    img.style.display = "";
    $("noPreview").classList.add("hidden");
    imgW = img.naturalWidth; imgH = img.naturalHeight;
    fit();
  };
  img.onerror = () => {
    if (token !== loadToken) return;
    img.style.display = "none";
    $("noPreview").classList.remove("hidden");
  };
  img.src = imgURL(idx);
  updateUI();
  for (const j of [idx + 1, idx + 2, idx - 1]) {
    if (j >= 0 && j < photos.length) { const pre = new Image(); pre.src = imgURL(j); }
  }
}

function fit() {
  const cw = viewer.clientWidth, ch = viewer.clientHeight;
  if (!imgW || !imgH || !cw || !ch) return;
  fitScale = Math.min(cw / imgW, ch / imgH);
  scale = fitScale;
  tx = (cw - imgW * scale) / 2;
  ty = (ch - imgH * scale) / 2;
  applyTransform();
}

function applyTransform() {
  img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
  $("zoomInfo").textContent = Math.abs(scale - fitScale) < 1e-6 ? "fit" : `${Math.round(scale * 100)}%`;
}

function zoomAt(px, py, newScale) {
  newScale = Math.max(fitScale * 0.5, Math.min(8, newScale));
  tx = px - (px - tx) * (newScale / scale);
  ty = py - (py - ty) * (newScale / scale);
  scale = newScale;
  applyTransform();
}

viewer.addEventListener("wheel", (e) => {
  if (!photos.length) return;
  e.preventDefault();
  const rect = viewer.getBoundingClientRect();
  zoomAt(e.clientX - rect.left, e.clientY - rect.top, scale * Math.exp(-e.deltaY * 0.0015));
}, { passive: false });

let dragging = false, lastX = 0, lastY = 0;
viewer.addEventListener("pointerdown", (e) => {
  if (!photos.length) return;
  dragging = true; lastX = e.clientX; lastY = e.clientY;
  viewer.classList.add("dragging");
  viewer.setPointerCapture(e.pointerId);
});
viewer.addEventListener("pointermove", (e) => {
  if (!dragging) return;
  tx += e.clientX - lastX; ty += e.clientY - lastY;
  lastX = e.clientX; lastY = e.clientY;
  applyTransform();
});
viewer.addEventListener("pointerup", () => { dragging = false; viewer.classList.remove("dragging"); });
viewer.addEventListener("dblclick", (e) => {
  if (!photos.length) return;
  const rect = viewer.getBoundingClientRect();
  if (scale > fitScale * 1.01) fit();
  else zoomAt(e.clientX - rect.left, e.clientY - rect.top, Math.max(1, fitScale * 2));
});
new ResizeObserver(() => { if (Math.abs(scale - fitScale) < 1e-6) fit(); }).observe(viewer);

// ---------- decisions & UI ----------
function decide(d) {
  if (!photos.length) return;
  if (d === "jpeg" && photos[idx].rawOnly) return; // no image half to keep
  photos[idx].decision = d;
  updateUI();
  if (idx < photos.length - 1) showPhoto(idx + 1);
}

function nextUndecided() {
  const n = photos.length;
  for (let step = 1; step <= n; step++) {
    const j = (idx + step) % n;
    if (photos[j].decision === null) { showPhoto(j); return; }
  }
}

function countBy() {
  const c = { both: 0, jpeg: 0, trash: 0, none: 0 };
  for (const p of photos) c[p.decision || "none"]++;
  return c;
}

function buildProgressBar() {
  const bar = $("progressbar");
  bar.innerHTML = "";
  photos.forEach((_, i) => {
    const seg = document.createElement("div");
    seg.addEventListener("click", () => showPhoto(i));
    bar.appendChild(seg);
  });
}

function updateUI() {
  const p = photos[idx];
  const c = countBy();
  $("counts").innerHTML =
    `<span class="c-both">Keep both <b>${c.both}</b></span>` +
    `<span class="c-jpeg">Image only <b>${c.jpeg}</b></span>` +
    `<span class="c-trash">Delete <b>${c.trash}</b></span>` +
    `<span>Undecided <b>${c.none}</b></span>`;
  if (!p) { $("filename").textContent = ""; return; }
  $("filename").innerHTML = `<b>${p.name}</b> · ${idx + 1} / ${photos.length}`;

  const d = p.decision || "none";
  const labels = {
    both: p.rawOnly ? "KEEP RAW" : "KEEP IMAGE+RAW",
    jpeg: "IMAGE ONLY", trash: "DELETE", none: "UNDECIDED",
  };
  const db = $("decisionBadge");
  db.textContent = labels[d];
  db.className = `badge decision-${d}`;
  $("rawBadge").textContent = p.rawOnly
    ? `RAW only (${p.raw.split(".").pop().toUpperCase()})`
    : (p.raw ? `RAW: ${p.raw.split(".").pop().toUpperCase()} ✓` : "no RAW file");

  $("btnBoth").innerHTML =
    `${p.rawOnly ? "Keep RAW" : "Keep image+RAW"}<span class="key">space</span>`;
  $("btnBoth").className  = "decide" + (d === "both"  ? " active-both"  : "");
  $("btnJpeg").className  = "decide" + (d === "jpeg"  ? " active-jpeg"  : "");
  $("btnJpeg").disabled   = !!p.rawOnly;
  $("btnTrash").className = "decide" + (d === "trash" ? " active-trash" : "");
  $("btnPrev").disabled = idx === 0;
  $("btnNext").disabled = idx === photos.length - 1;

  const segs = $("progressbar").children;
  for (let i = 0; i < segs.length; i++) {
    segs[i].className = `d-${photos[i].decision || "none"}` + (i === idx ? " current" : "");
  }
}

// ---------- modal ----------
function showModal(html) { $("modalBox").innerHTML = html; $("modalBackdrop").classList.remove("hidden"); }
function hideModal() { $("modalBackdrop").classList.add("hidden"); }

// ---------- commit ----------
function fmtBytes(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + " KB";
  return n + " B";
}

function openCommitModal() {
  const c = countBy();
  const rawDrop = photos.filter((p) => p.decision === "jpeg" && p.raw);
  const marks = rawDrop.filter((p) => p.markable);
  const unmarkable = rawDrop.length - marks.length;
  const trashPicks = photos.filter((p) => p.decision === "trash");
  const trashFiles = trashPicks
    .reduce((s, p) => s + (p.rawOnly ? 0 : 1) + (p.raw ? 1 : 0), 0);
  const totalFiles = rawDrop.length + trashFiles;
  const freedBytes = rawDrop.reduce((s, p) => s + (p.rawSize || 0), 0) +
    trashPicks.reduce((s, p) => s + (p.imgSize || 0) + (p.rawSize || 0), 0);

  if (totalFiles === 0) {
    showModal(`<h2>Nothing to delete</h2>
      <p>${c.none > 0 ? `${c.none} photo(s) are undecided and the rest are` : "All photos are"}
      marked "keep" — commit would delete no files.</p>
      <div class="btnrow"><button onclick="hideModal()">OK</button></div>`);
    return;
  }

  showModal(`
    <h2>Commit changes?</h2>
    ${c.none > 0 ? `<p class="warn">⚠ Options aren't selected for all photos — ${c.none} still undecided. Are you sure? (Undecided photos will be left untouched.)</p>` : ""}
    <p>This will delete <b>${rawDrop.length}</b> RAW file(s) from "Image only" picks,
       and <b>${trashFiles}</b> file(s) from "Delete" picks
       — <b>${totalFiles}</b> file(s) total.</p>
    <p>This will free <b>${fmtBytes(freedBytes)}</b> of disk space (once the Trash is emptied).</p>
    ${marks.length ? `
    <label style="display:flex;gap:8px;align-items:flex-start;font-size:15px;color:var(--dim);margin-bottom:10px;cursor:pointer;">
      <input type="checkbox" id="chkMark" checked style="margin-top:2px;">
      <span>Mark the <b>${marks.length}</b> kept JPEG(s) losing their RAW
        (invisible ~50-byte comment; image data untouched). Prevents iCloud
        from re-pairing them with the deleted RAW if these photos previously
        synced to iCloud. Untick only if these files have never been in your
        Photos library.</span>
    </label>` : ""}
    ${unmarkable > 0 ? `<p class="warn">⚠ ${unmarkable} kept image(s) losing their RAW
      aren't JPEGs and can't be marked — if they previously synced to iCloud,
      reimporting them may resurrect the deleted RAW.</p>` : ""}
    <p class="good">Deleted files go to the macOS Trash — recoverable until you empty it.</p>
    <div class="btnrow">
      <button onclick="hideModal()">Cancel</button>
      <button class="danger" id="btnConfirmCommit">Commit</button>
    </div>`);
  $("btnConfirmCommit").addEventListener("click", () => {
    const chk = $("chkMark");
    const decisions = photos.filter((p) => p.decision)
      .map((p) => ({ id: p.id, decision: p.decision }));
    runCommit(decisions, !chk || chk.checked, totalFiles);
  });
}

async function runCommit(decisions, markFlag, totalFiles) {
  showModal(`<h2>Committing…</h2><p>${totalFiles} file(s) to Trash.
    The first time, macOS may ask permission for the script to control Finder —
    approve it in the dialog that appears.</p>`);
  let result;
  try {
    const r = await fetch("/api/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions, mark: markFlag }),
    });
    result = await r.json();
  } catch (e) {
    showModal(`<h2>Commit failed</h2><p class="warn">${e}</p>
      <div class="btnrow"><button onclick="hideModal()">OK</button></div>`);
    return;
  }
  committedAny = true;
  const failHtml = result.failures.length
    ? `<p class="warn">⚠ ${result.failures.length} file(s) failed to delete:<br>` +
      result.failures.slice(0, 8).map((f) => `${f.name}: ${f.error}`).join("<br>") +
      (result.failures.length > 8 ? "<br>…" : "") + "</p>"
    : "";
  const markFailHtml = (result.mark_failures || []).length
    ? `<p class="warn">⚠ ${result.mark_failures.length} JPEG(s) could not be marked:<br>` +
      result.mark_failures.map((f) => `${f.name}: ${f.error}`).join("<br>") + "</p>"
    : "";
  const keptBackHtml = (result.kept_back || []).length
    ? `<p class="warn">⚠ Kept (not deleted) because their JPEG couldn't be marked
       — deleting them would risk iCloud resurrecting the pair:<br>${result.kept_back.join("<br>")}</p>`
    : "";
  showModal(`
    <h2>Done</h2>
    <p>Moved <b>${result.deleted}</b> of ${result.requested} file(s) to
       ${result.trash ? "the Trash" : "a .rawshuck-trash folder"}.
       ${result.marked ? `Marked <b>${result.marked}</b> kept JPEG(s) against iCloud re-pairing.` : ""}</p>
    ${failHtml}${markFailHtml}${keptBackHtml}
    <p>Your kept files are ready to import into Photos in one batch.</p>
    <div class="btnrow"><button class="primary" id="btnRescan">Continue</button></div>`);
  $("btnRescan").addEventListener("click", async () => { hideModal(); await loadList(); });
}

// ---------- switch source ----------
async function switchSource() {
  if (!$("sourceName").classList.contains("switchable")) return;
  if (photos.some((p) => p.decision !== null) && !committedAny) {
    if (!confirm("Switch to a different folder or files? Your current selections will be discarded.")) return;
  }
  let r;
  try { r = await (await fetch("/api/choose", { method: "POST" })).json(); }
  catch (e) { return; }
  if (r.ok) { committedAny = false; await loadList(); }
  else if (r.error) {
    showModal(`<h2>Can't switch</h2><p>${r.error}</p>
      <div class="btnrow"><button onclick="hideModal()">OK</button></div>`);
  }
}

async function quitApp() {
  quitting = true;
  try { await fetch("/api/quit", { method: "POST" }); } catch (e) {}
  showModal(`<h2>Stopped</h2><p>The app has shut down. You can close this tab.</p>`);
}

// ---------- keyboard ----------
document.addEventListener("keydown", (e) => {
  if (!$("modalBackdrop").classList.contains("hidden")) {
    if (e.key === "Escape") hideModal();
    return;
  }
  if (!photos.length) return;
  switch (e.key) {
    case "ArrowLeft":  e.preventDefault(); showPhoto(idx - 1); break;
    case "ArrowRight": e.preventDefault(); showPhoto(idx + 1); break;
    case " ":          e.preventDefault(); decide("both"); break;
    case "j": case "J": e.preventDefault(); decide("jpeg"); break;
    case "Backspace": case "Delete": e.preventDefault(); decide("trash"); break;
    case "u": case "U": e.preventDefault(); photos[idx].decision = null; updateUI(); break;
    case "n": case "N": e.preventDefault(); nextUndecided(); break;
  }
});

// ---------- wire up ----------
function wire(id, fn) {
  $(id).addEventListener("click", (e) => { fn(); e.currentTarget.blur(); });
}
wire("btnPrev", () => showPhoto(idx - 1));
wire("btnNext", () => showPhoto(idx + 1));
wire("btnBoth", () => decide("both"));
wire("btnJpeg", () => decide("jpeg"));
wire("btnTrash", () => decide("trash"));
wire("btnNextUndecided", nextUndecided);
wire("btnCommit", openCommitModal);
wire("btnQuit", quitApp);
wire("sourceName", switchSource);
$("modalBackdrop").addEventListener("click", (e) => { if (e.target === $("modalBackdrop")) hideModal(); });

window.addEventListener("beforeunload", (e) => {
  if (photos.some((p) => p.decision !== null) && !committedAny && !quitting) {
    e.preventDefault();
    e.returnValue = "";
  }
});

loadList();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- main

def main():
    global SOURCE
    args = sys.argv[1:]
    if args:
        paths = [os.path.abspath(os.path.expanduser(a)) for a in args]
        if len(paths) == 1 and os.path.isdir(paths[0]):
            SOURCE = {"mode": "folder", "folder": paths[0]}
        else:
            missing = [p for p in paths if not os.path.isfile(p)]
            if missing:
                print("Not found: " + ", ".join(missing))
                sys.exit(1)
            SOURCE = {"mode": "files", "files": paths}
    elif sys.platform == "darwin":
        print("Opening picker… (check for a dialog window)")
        SOURCE = choose_source_mac()
        if not SOURCE:
            print("Nothing chosen — exiting.")
            sys.exit(0)
    else:
        print(f"Usage: {sys.argv[0]} /path/to/folder  (or a list of image files)")
        sys.exit(1)

    scan()
    n_pairs = sum(1 for it in ITEMS if it["img"] and it["raw"])
    n_rawonly = sum(1 for it in ITEMS if not it["img"])
    print(f"Source: {source_label()}")
    print(f"Found {len(ITEMS)} photo(s): {n_pairs} image+RAW pair(s), "
          f"{n_rawonly} RAW-only, {len(ITEMS) - n_pairs - n_rawonly} image-only.")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Running at {url}  —  press Ctrl+C (or the Quit button) to stop.", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Stopped.")


if __name__ == "__main__":
    main()
