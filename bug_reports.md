# Bug troubleshooting log

Internal working notes.

## Silent import-drop investigation

August 2026.

#### TL;DR

Two separate phenomena were investigated while building/using Rawshuck. **Both
are now resolved.**

1. **iCloud RAW resurrection** — SOLVED, documented in README & blog. iCloud's
   server-side dedupe re-links a reimported, byte-identical JPEG to its deleted
   RAW (server retains purged assets ~30 days). Defeated by changing the JPEG's
   checksum ("marking"). Confirmed by A/B test: one-byte-different JPEG stays
   lone; identical control resurrects its RAW within minutes of import.

2. **Apple Photos silently drops files from large drag-imports** — SOLVED
   2026-08-03.

   **Mechanism:** a multi-file drag whose items are *not homogeneous with
   respect to `com.apple.quarantine` flag* is delivered to Photos as two separate
   `aevt/odoc` AppleEvents — one per quarantine class. Photos builds a separate
   `PHImportUrlSource` for each event, and the Import pane ends up showing only
   the last one instead of the union. Nothing fails, nothing times out, nothing
   is corrupt. The pane is simply displaying the final chunk.

   **Why our files were heterogeneous:** Photos stamps `com.apple.quarantine`
   (agent `Photos`) on the originals it exports. `jpeg-mark` rewrites each
   marked file via `os.replace` from a fresh temp file, which does not inherit
   xattrs — so every marked JPEG came out quarantine-free while every untouched
   JPEG and CR3 kept the flag. Hence the split always landing exactly on the
   marked/unmarked seam.

   **Fixes:**
   - **Code fix — shipped in v1.1.1.** Marking now appends in place
     (`open(path, "ab")`) instead of rewriting via a temp file, so xattrs
     survive and a culled folder stays homogeneous. `--repair` still has to
     rewrite, so it copies xattrs across explicitly. See "The code change".
   - Drag the enclosing **folder** onto Photos rather than a multi-file
     selection. One URL → one AppleEvent → one source → nothing to partition.
     Verified: 1 URL, 155 assets, first time. Still the recommended habit —
     it is robust to any source of heterogeneity, not just ours.
   - `xattr -r -d com.apple.quarantine .` on the staging folder before dragging.
     Verified: full 155 appear from a Cmd+A drag.

Along the way we found and fixed a real bug of our own (v1.0.0 marking
corrupted MPF offsets — see below). It was a perfect statistical match for
the vanishing files and turned out to be a co-traveller, not the cause.

#### Diagnosis

**Failing case** — Cmd+A on the 187-file dodgy batch (155 JPG + 32 CR3), dragged
onto Photos. Internet disconnected and iCloud sync confirmed paused, so iCloud
is not in play. Then, inspected logs from Photos in Terminal:

`log show --last 2m --predicate 'process CONTAINS "Photos" OR process CONTAINS "photolibraryd"' --info`

Output:

```
18:58:04.170  RECEIVED:(aevt,odoc) {aevt,odoc target=Dock  returnID=-8525}
18:58:04.399  [backend:Import] Creating PHImportUrlSource <0xa968d64c0>
18:58:04.429  [backend:Import] Created source for 'temp_photos' containing 94 URL(s)
18:58:04.583  [backend:Import] PHImportUrlSource <0xa968d64c0> loaded 94 assets
              [ImportUI-DataSourceManager] [primary]/[selection] addAssets:

18:58:04.850  RECEIVED:(aevt,odoc) {aevt,odoc target=Dock  returnID=-2543}   ← second event
18:58:05.104  [backend:Import] Creating PHImportUrlSource <0xa9633f100>      ← second source
18:58:05.115  [backend:Import] Created source for 'temp_photos' containing 93 URL(s)
18:58:05.519  [backend:Import] PHImportUrlSource <0xa9633f100> loaded 61 assets
              [ImportUI-DataSourceManager] [primary]/[selection] addAssets:
```

- 94 + 93 = 187 = the batch exactly.
- 93 URLs → 61 assets ⇒ that chunk holds all 32 CR3s (61 JPEG + 32 CR3, pairing
  to 61 assets). The partition is therefore **not** interleaved — it is two
  contiguous groups.
- The 94-URL chunk is exactly the 94 marked JPEGs.
- Reproduced identically on a second drag at 18:58:32 (94, then 93 → 61).
  Deterministic, not a race.
- **Zero errors, faults, or timeouts anywhere in the log.** The only `Error`
  lines are `com.apple.iCloudQuota` complaining about a cellular-data switch,
  which is unrelated.

**Working case** — same folder, same files, dragged as a *folder* rather than a
selection:

```
19:39:09.595  RECEIVED:(aevt,odoc) {aevt,odoc target=Dock  returnID=32352}
19:39:09.843  [backend:Import] Creating PHImportUrlSource <0xa9661a840>
19:39:09.856  [backend:Import] Created source for 'temp_photos' containing 1 URL(s)
19:39:09.951  [backend:Import] PHImportUrlSource <0xa9661a840> loaded 155 assets
```

One URL, one event, one source, all 155 assets. No flash, no collapse.

#### The partition rule: `com.apple.quarantine`

`xattr -l` on three specimens from the dodgy batch:

```
unmarked, paired:  com.apple.cscachefs
                   com.apple.lastuseddate#PS
                   com.apple.quarantine: 0082;6a6fd622;Photos;
unmarked, lone:    com.apple.cscachefs
                   com.apple.lastuseddate#PS
                   com.apple.quarantine: 0082;6a6fd629;Photos;
marked:            com.apple.lastuseddate#PS
```

Photos quarantines its own exports (agent string `Photos`; `0x6a6fd622` is a
Unix timestamp in early August 2026, i.e. the export). `jpeg-mark`'s
`write_atomic` builds a temp file and `os.replace`s it over the original, and a
fresh temp file inherits no xattrs — so the marked files lost the flag.

Counted across the batch: **94 clean, 61 quarantined** JPEGs; the 32 CR3s are
quarantined. 61 + 32 = 93. Those are exactly the two chunk sizes in the log.

Behavioural confirmation: `xattr -r -d com.apple.quarantine .` followed by the
same Cmd+A drag that had failed for a fortnight → all 155 photos, one event.

So the mark was never what Photos objected to. The mark's only role was to
strip a flag from 94 files and thereby split the drag into two classes. The
94/94, 61/61, 0-anomaly correlation with marking was real and causal — just
three steps removed from anything about JPEG structure.

**Confidence: high.** Predicted mechanism, predicted counts, predicted
behavioural result, all three confirmed.

Contingency table check:

```
cd "/path/to/temp_photos" || exit

for f in *.JPG; do
  if tail -c 4096 "$f" | grep -qa 'photo-cull:'; then m="marked  "; else m="unmarked"; fi
  if xattr "$f" | grep -q '^com.apple.quarantine$'; then q="quarantined"; else q="clean      "; fi
  echo "$m $q"
done | sort | uniq -c
```

**Note:** the contingency-table command used to verify this
tested for the mark with `tail -c 4096 | grep photo-cull`, which reports every
v1.0.0 file as unmarked because that mark is *mid-file*. Use
`head -c 65536 | grep -qa 'photo-cull:'` for v1.0.0 files, the tail for v1.1.0,
both for a mixed folder. The quarantine axis was unaffected and the counts
(94/61) match the independently established marked/unmarked split exactly.

#### The code change (shipped, v1.1.1)

`write_atomic` was what destroyed the xattrs. For the tail mark no rewrite is
needed at all, so `mark_jpeg` (`rawshuck.py`) and `mark` (`jpeg-mark.py`) now
do:

```python
with open(path, "ab") as f:
    f.write(new_tag())
```

Appending in place preserves the inode and every extended attribute, so a
staging folder stays homogeneous and the drag can't be partitioned. It also
turns marking from "rewrite ~10 MB per file" into "write 49 bytes" — on a
94-file batch, about a gigabyte of I/O that stops happening. Mark *detection*
was likewise changed to read only the head (64 KB) and tail (4 KB) windows
instead of slurping the whole file.

Tradeoff accepted: strict write atomicity. A torn write leaves a truncated tag
after EOI, which every decoder ignores, and re-running appends a whole one —
a better failure mode than silently losing the xattrs.

`--repair` genuinely must rewrite (it excises mid-file bytes), so `write_atomic`
now calls `copy_xattrs(path, tmp)` before `os.replace`. CPython exposes
`os.getxattr`/`os.setxattr` on Linux only, so on macOS this is ctypes into
libSystem's `listxattr`/`getxattr`/`setxattr`; it is best-effort and silently
skips kernel-protected attributes (`com.apple.macl` and friends) that no
ordinary process can set.

`web/index.html` (`markJpegBytes`) is unchanged: the File System Access API
has no notion of extended attributes, so a browser can neither preserve nor
destroy them.

**Watch out for**, if this code is touched again: the head and tail scan
windows overlap on files smaller than 64 KB, which made a tail tag report as a
v1.0.0 mid-file mark ("run with --repair", which then found nothing to repair).
`scan()` now splits the buffer so the windows stay disjoint, and captures the
SOI check before splitting — otherwise files under 4 KB lose their first two
bytes to the tail window and get rejected as "not a JPEG".

Verified after the change: inode preserved; +49 bytes exactly; decoded pixels
byte-identical; idempotent on re-run; correct dispositions for unmarked,
tail-marked, v1.0.0-marked, non-JPEG and missing files, at 1 KB, 9 KB, 66 KB
and 10 MB; `--repair` still restores MPF arithmetic exactly and leaves pixels
byte-identical; no temp files left behind on any path.

#### Key technical facts (verified, high confidence)

- **Marker string** is `photo-cull:` (predates the Rawshuck name; kept stable
  across versions so already-marked detection works on any vintage).
- **v1.0.0 mark**: JPEG COM segment (`FF FE len "photo-cull:<uuid>"`, 51 bytes
  total) inserted after the contiguous APPn run (after APP2/MPF, before DQT).
- **v1.1.0 mark**: `\nphoto-cull:<uuid>\n` (~49 bytes) appended after EOI.
  Already-marked detection scans first 65536 bytes (legacy) + last 4096 (tail).
- **Canon R10 JPEG structure** (from real files): SOI · APP1 Exif (~44 KB, incl.
  a 160×120 thumbnail) · APP1 XMP (3 KB) · APP2 MPF (512 B) · DQT/SOF0/DHT/SOS ·
  main image 6000×4000 · EOI · **a second JPEG of 1620×1080, ~480–630 KB**,
  occupying the remainder of the file. The MPF table (TIFF-structured, offsets
  relative to the MPF header) points to it. In pristine files:
  `mpf_base + entry2.offset + entry2.size == file size` EXACTLY.
- **What that second image is** — MPF attribute `0x40010002`, i.e. MPType
  `0x010002` = "Large Thumbnail (Full HD class)". It is a *screen-sized
  preview*, **not** a second full-size copy (an earlier draft of these notes
  said "full-size, ~180 KB"; both halves were wrong). Canon writes it so
  cameras, printers and OS previewers can show the shot without decoding 24 MP.
- **These files are not HDR.** Baseline 8-bit SOF0, no ISO 21496-1 gain map, no
  `hdrgm` XMP (the XMP is a single `xmp:Rating` tag), no third MPF image, no PQ
  transfer function. Canon's HDR PQ output is `.HIF` (HEIF), not JPEG. There is
  no latent HDR-ness for the MPF damage to have spoiled.
- **The v1.0.0 bug**: inserting 51 bytes between the MPF table and the images
  makes every offset in that table 51 bytes stale. Concretely, on IMG_7503:
  the table points image 2 at 9836032, where the bytes are `FF FF FF FF`; the
  real SOI is at 9836083 (+51). The declared primary size (9835920) is likewise
  51 short and excludes the primary's own EOI. Verified across the whole "dodgy
  batch": 94/94 marked files off by −51, 61/61 unmarked exact, 0 anomalies.
- **Impact of that bug in practice: negligible.** Pixel data of both images is
  untouched; only the pointers are stale. Virtually every decoder ignores MPF
  and reads SOI→EOI, which is why the affected files look fine everywhere.
  Exposure is limited to MPF-aware consumers (macOS ImageIO parses MPF) getting
  a broken *secondary* image, with fallback to the primary. **The ~94 v1.0.0-marked
  files already in the library can be left as they are.**
- **The fix**: `jpeg-mark.py --repair` excises the mid-file COM (restoring all
  offsets byte-exactly) and appends a tail tag. Verified: repaired files have
  exact MPF arithmetic (`delta == −taglen`), decoded pixels are byte-identical
  to the unrepaired file, and new-style marking leaves the original as a
  byte-exact prefix. Both idempotent.

#### Experiments run, in order (all on real Canon R10 files)

| # | Test | Result |
|---|------|--------|
| 1 | Original incident: ~200-photo back-catalogue batch ("dodgy batch", 155 JPG + 32 CR3 after culling w/ v1.0.0), Cmd+A drag | Pairs + unmarked lone JPEGs imported; all 94 marked JPEGs silently missing |
| 2 | Marked JPEGs imported alone (1, 2, then all 155 JPEGs sorted-by-type) | All fine every time |
| 3 | Careful small replication: 6 pairs + 2 filter JPEGs, cull, delete, purge, reimport | All fine |
| 4 | XMP sidecar hypothesis | Dead: folder contains zero XMPs (155 JPG + 32 CR3 = 187 exactly); export option was never ticked |
| 5 | 2×2 factorial: (unmarked, RAW-shot lone JPEG)+pair; (marked, filter-shot JPEG)+pair | Both visible → neither mark alone nor RAW-shot metadata alone triggers |
| 6 | 3-file: dodgy-batch marked JPEG + burst-adjacent kept pair | Visible → not per-file, not burst adjacency |
| 7 | Byte forensics on specimens | Found MPF corruption (−51) in marked file only |
| 8 | Whole-batch scan | 94 corrupted = exactly the 94 vanished; 61 clean = the 61 shown |
| 9 | `dodgy_fixed` replica (all repaired, fresh tail tags) full drag | STILL collapses to 61 (flash now says "94 NEW" instead of "94 already imported") → MPF corruption falsified as sole trigger |
| 10 | Fresh batch 2 (~96 files, v1.1.0 tags): first drag | Only ~30 shown (marked missing) |
| 11 | Batch 2, identical retry (×2) | ALL 85 appear both times; imported fine |
| 12 | Fresh batch 3 (79 pairs + 112 tail-marked, 270 files): full drag | Flash "112 new" → collapses to NOTHING (pairs too) |
| 13 | Batch 3 halves: A (83 photos/115 files), B (108/155) | A fine first time; B collapses like the full batch |
| 14 | B halved again: B1 (63 photos), B2 (45/70) | Both fine first time → same files pass in smaller sets |
| 15 | **Unified-log capture of a failing Cmd+A drag, internet off / iCloud paused** | **Two `odoc` events → two sources, 94 URLs then 93 URLs; pane keeps the second. No errors. Root cause found.** |
| 16 | **Same folder dragged as a folder, not a selection** | **One `odoc`, 1 URL, 155 assets, all present. FIXED.** |
| 17 | Cmd+A drag of the same 187 files with the Finder window sorted by Name, then by Size | Identical 94/93 split with identical membership both times → **partition is not positional in view order** |
| 18 | `ls -f` (raw directory order) | Marked and unmarked thoroughly interleaved (APFS hashes directory entries by filename) → **directory-order hypothesis dead** |
| 19 | `xattr -l` on marked vs unmarked specimens; counted across batch | Unmarked carry `com.apple.quarantine ...;Photos;`, marked do not. **94 clean / 61 quarantined JPEGs + 32 quarantined CR3s = the 94/93 chunks exactly** |
| 20 | `xattr -r -d com.apple.quarantine .`, then the identical Cmd+A drag | **All 155 photos, single event. Cause confirmed.** |

Falsified hypotheses: XMP sidecars; Finder selection artifact *as originally
conceived*; browser quarantine xattrs (Python version); mark alone; RAW-shot
metadata alone; mark × RAW-shot interaction; burst adjacency; MPF corruption;
iCloud/network involvement (test 15 ran offline); "second fingerprinting pass
fails/times out" (no failure exists — the pane simply overwrites itself);
"load-dependent flakiness in Photos". Rows 13–14 are consistent throughout:
smaller sets stay under whatever threshold triggers the split.

#### Remaining loose ends (all minor)

1. **Why did retries sometimes heal (rows 11, 13–14)?** A quarantine-based
   partition is deterministic and shouldn't produce retry-dependent outcomes.
   Unexplained.
2. **Row 2** (155 JPEGs dragged as a selection, reportedly fine) was never
   count-verified and is probably a false negative — a mixed-quarantine set of
   that size should have split.
3. **Does the partition generalise beyond quarantine?** Untested whether other
   xattr differences, or other LaunchServices-relevant properties, produce the
   same chunking. Not worth chasing.
4. **Feedback Assistant report** worth filing, and now easy to write: "Photos
   Import pane shows only the last source when a drag of mixed-quarantine files
   arrives as multiple `odoc` events; no error shown; user silently loses
   files." Include the two log excerpts, the xattr counts, and the
   strip-quarantine result — the whole case fits on one page.

#### Repo state relevant to all this

- `rawshuck.py` v1.1.1: in-place tail-append marking (`mark_jpeg`), windowed
  dual-location already-marked detection (`scan_jpeg_mark`). `jpeg-mark.py`:
  standalone marker (`mark`, `scan`) + `--repair` (`write_atomic` +
  `copy_xattrs`). Web `web/index.html` `markJpegBytes`: same tail format,
  unchanged. The mark *format* is still what these notes call the v1.1.0 mark
  — only the write method changed in v1.1.1.
- Local-only folders (gitignored): `diagnostics/` and `diagnosing/` (specimen
  files: IMG_7501 unmarked pair, IMG_7102 v1.1.0 tail-tagged, IMG_7503 v1.0.0
  mid-file-marked), `dodgy_batch/` (original v1.0.0-marked batch, 187 files),
  `dodgy_fixed/` (repaired replica, fresh tags, CR3s hardlinked).
- MPF-checking snippet (offsets relative to MPF TIFF header; validate via
  `base + offset₂ + size₂ == filesize` for pristine, `== filesize − taglen`
  for tail-tagged):

```python
def mpf_delta(data):  # 0 = pristine; -N = trailing N bytes after claimed end
    import struct
    i = 2
    while i < len(data) - 4 and data[i] == 0xFF and data[i+1] not in (0xDA, 0xD9):
        seglen = struct.unpack(">H", data[i+2:i+4])[0]
        if data[i+1] == 0xE2 and data[i+4:i+8] == b"MPF\0":
            base = i + 8
            e_ = "<" if data[base:base+2] == b"II" else ">"
            u16 = lambda o: struct.unpack(e_+"H", data[o:o+2])[0]
            u32 = lambda o: struct.unpack(e_+"I", data[o:o+4])[0]
            ifd = base + u32(base + 4)
            entries = n = None
            for k in range(u16(ifd)):
                e = ifd + 2 + 12*k
                if u16(e) == 0xB001: n = u32(e + 8)
                if u16(e) == 0xB002: entries = base + u32(e + 8)
            if not n or n < 2: return None
            e = entries + 16
            return (base + u32(e + 8) + u32(e + 4)) - len(data)
        i += 2 + seglen
    return None
```

#### Morals collected along the way

- Never insert into the middle of a file format you haven't fully parsed.
- Two things were true at once: the MPF bug was real, *and* it wasn't the
  culprit. Fixing it was correct and changed nothing about the symptom. Both
  facts had to be held simultaneously for a fortnight.
- But "correlation is not causation" was still the wrong lesson to draw at
  row 9. The correlation with marking (94/94, 61/61, 0 anomalies) was causal
  all along — through a chain (rewrite → lost xattr → heterogeneous drag →
  split AppleEvent → clobbered import pane) in which not one step could be
  guessed from the outside. Falsifying the *proximate* mechanism correctly
  killed the MPF theory; concluding "so the marking is exonerated" was an
  overcorrection that cost several more experiments.
- The confound was self-inflicted, and invisible to every test run on the files
  themselves — because it wasn't *in* the files. Fourteen experiments
  interrogated the bytes; the answer was in a scrap of metadata bolted to the
  outside, which `xattr -l` would have shown in one second on day one.
- Fourteen experiments of black-box behavioural inference were beaten by one
  reading of the log the software was already writing. **Check whether the
  system is telling you what it's doing before inferring it from the outside.**
- Software that must fail should fail out loud. Every hour spent on this traces
  back to a silent failure: silent dedupe re-linking, silent chunk replacement,
  silent list collapse.
