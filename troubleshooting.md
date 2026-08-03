# Troubleshooting log: the silent import-drop investigation

Internal working notes — Jayden Macklin-Cordes + Claude (agent), August 2026.
Written so a future session (human or agent) can pick this up without re-deriving everything.

## TL;DR

Two separate phenomena were investigated while building/using Rawshuck:

1. **iCloud RAW resurrection** — SOLVED, documented in README & blog. iCloud's
   server-side dedupe re-links a reimported, byte-identical JPEG to its deleted
   RAW (server retains purged assets ~30 days). Defeated by changing the JPEG's
   checksum ("marking"). Confirmed by A/B test: one-byte-different JPEG stays
   lone; identical control resurrects its RAW within minutes of import.

2. **Apple Photos silently drops files from large drag-imports** — root cause
   NOT fully established; behaviour characterised and mitigated. Along the way
   we found and fixed a real bug of our own (v1.0.0 marking corrupted MPF
   offsets — see below), which was initially a perfect statistical match for
   the dropped files but turned out not to be the (sole) trigger.

**Current mitigation (in README):** before clicking Import, verify the photo
count in the import window equals expectation (pairs + lone images). If short:
cancel and re-drag (often heals); if still short, split the batch in half.
Never empty the staging folder / format the card until counts match.

## Key technical facts (verified, high confidence)

- **Marker string** is `photo-cull:` (predates the Rawshuck name; kept stable
  across versions so already-marked detection works on any vintage).
- **v1.0.0 mark**: JPEG COM segment (`FF FE len "photo-cull:<uuid>"`, 51 bytes
  total) inserted after the contiguous APPn run (after APP2/MPF, before DQT).
- **v1.1.0 mark**: `\nphoto-cull:<uuid>\n` (~49 bytes) appended after EOF.
  Already-marked detection scans first 65536 bytes (legacy) + last 4096 (tail).
- **Canon R10 JPEG structure** (from real files): SOI · APP1 Exif (~41KB, incl.
  thumbnail) · APP1 XMP (3KB) · APP2 MPF (512B) · DQT/SOF0/DHT/SOS · main image
  · EOI · **a second full-size JPEG image (~180KB)** occupying the remainder of
  the file. The MPF table (TIFF-structured, offsets relative to the MPF header)
  points to that trailing image. In pristine files:
  `mpf_base + entry2.offset + entry2.size == file size` EXACTLY.
- **The v1.0.0 bug**: inserting 51 bytes between MPF and the trailing image
  makes that arithmetic short by exactly 51. Verified across the whole "dodgy
  batch": 94/94 marked files off by −51, 61/61 unmarked exact, 0 anomalies.
- **The fix**: `jpeg-mark.py --repair` excises the mid-file COM (restores all
  offsets byte-exactly) and appends a tail tag. Verified: repaired files have
  exact MPF arithmetic; new-style marking leaves the original as a byte-exact
  prefix. Both idempotent.

## The observed Photos failure

Environment: macOS Photos.app with iCloud Photos on; import via drag-and-drop
onto Photos, which opens the Import pane (the one with "Already Imported" /
"New Photos" sections). Culling done with the **Python** version (so browser
quarantine xattrs are not in play).

Symptom: on large batches, the pane briefly flashes a count equal to the
number of *marked* JPEGs (as "Already imported" for v1.0.0-marked files, as
"New" for v1.1.0-tail-tagged files — consistent with their checksum status),
then the list collapses: sometimes to only the unmarked/pair items, in one
case (batch 3) to *nothing at all, pairs included*. No error is ever shown.
"Import All" reflects the collapsed list. Dropped files are simply never
imported; they remain untouched in the folder.

## Experiments run, in order (all on real Canon R10 files)

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
| 11 | Batch 2, identical retry (×2) | ALL 85 appear both times; imported fine → **transient!** |
| 12 | Fresh batch 3 (79 pairs + 112 tail-marked, 270 files): full drag | Flash "112 new" → collapses to NOTHING (pairs too) |
| 13 | Batch 3 halves: A (83 photos/115 files), B (108/155) | A fine first time; B collapses like the full batch |
| 14 | B halved again: B1 (63 photos), B2 (45/70) | Both fine first time → **same files pass in smaller sets** |

Falsified hypotheses: XMP sidecars; Finder selection artifact (Cmd+A used;
pane counts confirm); browser quarantine xattrs (Python version); mark alone;
RAW-shot metadata alone; mark × RAW-shot interaction at small scale; burst
adjacency; MPF corruption as sole trigger (test 9). Never formally run:
`xattr -l` comparison (mooted by Python-version usage).

## Best current model (moderate confidence, not proven)

Photos' Import pane runs (at least) two passes: a fast enumeration (the flash —
dominated by files whose fingerprints match nothing, i.e. freshly-marked ones)
and a slower grouping/fingerprinting pass. On large batches (~>100 photos /
~150 files, threshold inconsistent) the second pass fails — race, timeout, or
exception — and its staged items are discarded silently rather than surfaced
as an error. Retrying with warm caches, or shrinking the batch, avoids the
failure. The v1.0.0 MPF corruption plausibly aggravated it (that batch never
healed on retry, unlike tail-tagged batches), but pristine files reproduce the
collapse, so the load-dependent component is Apple's alone.

## If this is ever resumed

1. **Capture Photos' own logs during a failing drag** (the single most
   valuable un-run diagnostic):
   `log show --last 2m --predicate 'process CONTAINS "Photos" OR process CONTAINS "photolibraryd"' --info > photos_import.log`
   run immediately after a collapse; grep for errors around the drag timestamp.
2. Test whether full-batch retry heals batch 3 / `dodgy_fixed` (retry-heal was
   only demonstrated on batch 2).
3. Controlled batch-size sweep with synthetic files to find the threshold and
   establish whether file count, byte volume, or pair count drives it.
4. File a Feedback Assistant report: "Photos import pane silently omits files
   from large imports; no error shown" — with the halves-pass/union-fails
   evidence and logs.

## Repo state relevant to all this

- `rawshuck.py` v1.1.0: tail-append marking (`mark_jpeg`), dual-location
  already-marked detection. `jpeg-mark.py`: standalone marker + `--repair`.
  Web `web/index.html` `markJpegBytes`: same tail behaviour.
- Local-only folders (gitignored): `diagnostics/` (specimen files),
  `dodgy_batch/` (original v1.0.0-marked batch, 187 files),
  `dodgy_fixed/` (repaired replica, fresh tags, CR3s hardlinked — suitable as
  a clean reimport source for the 94 library copies that still carry corrupted
  MPF internally, should that ever matter; it hasn't so far).
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

## Morals collected along the way

- Never insert into the middle of a file format you haven't fully parsed.
- A perfect statistical correlation (94/94, 61/61, 0 anomalies) can still be
  a co-traveller rather than the cause. The repaired-batch test (row 9) was
  worth running precisely because it could falsify the beautiful theory — and did.
- Software that must fail should fail out loud. Every hour spent on this
  investigation traces back to a silent failure: silent dedupe re-linking,
  silent import omission, silent list collapse.
