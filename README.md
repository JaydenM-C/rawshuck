# Rawshuck

Spare your hard drive! Cull RAW+JPEG shoots *before* importing into your photo library.

*To shuck: to strip away the husk and keep the kernel. Here the husk is 40&nbsp;MB.*

## The predicament

If you shoot RAW+JPEG and use Apple Photos then, like me, you may have discovered two pain points:

1. **Photos treats a RAW+JPEG pair (e.g. Canon .CR3) as one indivisible asset.** You cannot delete just the RAW half and keep the JPEG. Like, at all. Ce n'est pas possible. Import a few hundred pictures from a day or two in the field and watch your disk space float away in the breeze (not to mention your iCloud bill). Even though you'll only ever care about the RAW for a handful of keepers.
2. **iCloud resurrects deleted RAWs.** If you already have a library full of RAW+JPEGs, there's one clunky workaround: export unmodified originals to a folder (exports the RAW + JPEG components as two files), delete the unwanted RAWs, delete everything from your photo library then reimport the JPEGs + surviving RAWs. However, this fails in a weird way: iCloud's server-side deduplication recognises the reimported JPEG (byte-identical to one it has seen before) and quietly re-links it to the "deleted" RAW, which comes back from the grave a few minutes after import. See [The iCloud resurrection problem](#the-icloud-resurrection-problem) below.

Rawshuck fixes the workflow: review each photo once, decide **keep JPEG+RAW**, **JPEG only**, or **delete**, hit commit, and import the survivors. It also defeats the resurrection behaviour when sorting a back catalogue. (NB: I'm using "RAW+JPEG" here since that's what I'm used to. But Rawshuck works with other compressed image+RAW formats too, as well as lone RAWs and other unpaired images.)

This is a deliberately narrow tool. It doesn't rate, tag, edit, or organise. It answers one question per photo — *what survives?* — as fast as you can press keys.

## What it is

A single Python script with no dependencies. It runs a small local web server and uses your default browser as the display; all file operations happen in the script, on your machine. Nothing is uploaded anywhere. (Prefer zero installation? There's also a [web version](#web-version).)

- **Deletions go to the macOS Trash** (via Finder) — recoverable until you empty it.
- **Reviews RAW+JPEG pairs, lone images, and lone RAWs.** Pairing is by filename (`IMG_1234.JPG` + `IMG_1234.CR3`).
- **Formats:** JPEG, PNG, HEIC/HEIF, TIFF, WebP, GIF for display; CR3, CR2, DNG, ARW, NEF, RAF, ORF, RW2 and friends as RAWs. RAW-only and HEIC/TIFF previews are converted on the fly with macOS's built-in `sips` (originals untouched).
- **Keyboard-first:** review hundreds of photos in minutes.

## Requirements

- macOS with `python3` (if typing `python3 --version` in Terminal offers to install developer tools, accept — or `brew install python`).
- Any browser. No packages to install.
- It runs on Linux/Windows too, with reduced niceties: deletions move to a `.rawshuck-trash` folder instead of the Trash, and RAW/HEIC previews are unavailable (no `sips`).

## Usage

Clone/download this repository and navigate to its location in the Terminal (`cd ~/PATH_TO_REPO`). Then, use the following commands in the Terminal to start the programme:

```
python3 rawshuck.py                     # native picker: choose a folder or pick files
python3 rawshuck.py /path/to/folder     # review a whole folder
python3 rawshuck.py a.jpg b.jpg c.cr3   # review specific files
```

Tip: type `python3 rawshuck.py ` and drag a folder onto the Terminal window to paste its path.

Your browser opens with the first photo. For each photo choose one of three fates:

| Key | Action |
|-----|--------|
| `Space` | Keep compressed image (e.g. JPEG) + RAW |
| `J` | Keep compressed image only (RAW will be deleted) |
| `Delete` / `Backspace` | Delete entirely |
| `←` `→` | Navigate |
| `U` | Clear choice |
| `N` | Jump to next undecided |

Scroll to zoom, drag to pan, double-click toggles 100%/fit. The strip above the buttons shows every photo's status (green/blue/red) and is clickable. Click the folder name in the header to switch to a different folder or set of files.

Nothing touches disk until you press **Commit**, which shows exactly what will be deleted and asks for confirmation. After committing, the folder contains only survivors — import them into Photos **in one batch** so RAW+JPEG pairs link correctly (Photos only pairs files imported together).

## Web version

The same review flow also exists as a browser-only app in [`web/`](web/) — nothing to install, nothing uploaded anywhere (it's a static page; all file access happens locally in your browser). Differences from the Python version:

- **Chromium-only** (Chrome, Edge, Brave, Arc, Opera). It relies on the File System Access API to read and delete local files, which Safari and Firefox don't support.
- **Deletions are permanent** — browsers cannot use the Trash. Ideal for fresh-shoot culling where the SD card is your safety net; for back-catalogue tidying, the Python version's Trash-based deletes are the safer choice.
- **Previews:** JPEG/PNG/WebP/GIF display natively. Lone CR3s display via the full embedded JPEG preview, extracted client-side from the CR3 container. Other RAW formats and HEIC/TIFF show a placeholder but remain fully reviewable — sample files from other cameras are welcome via GitHub issues so more formats can be added.
- Sources: choose a folder, pick individual files, or drag-and-drop either onto the page. (Folder mode is smoothest — some browsers prompt separately for write access to individually-picked files.)

JPEG marking against [iCloud resurrection](#the-icloud-resurrection-problem) works identically in both versions.

## The iCloud resurrection problem

This deserves its own section because it is bizarre, undocumented, and confused the heck out of me.

It's an issue which applies specifically when tidying an existing Photos library, using the 'export originals > delete RAWs > delete originals from Photos and reimport' workflow described above. It doesn't apply when sorting fresh photos on an SD card, which have never been imported before.

**Symptom:** You export unmodified originals from Photos, delete the RAWs, then delete the originals from Photos (emptying Recently Deleted everywhere, and verifying they're gone from every device). You reimport your culled JPEGs, and everything initially appears to have worked as expected. Then, minutes later, you look back and every reimported photo is a RAW+JPEG pair again. The RAWs are back from the dead. It's not just a labelling issue (Photos mistakenly labelling a plain JPEG as "RAW+JPEG"), if you try re-exporting one, it'll re-export two files, including the original RAW you already deleted everywhere. It's spooky.

**Mechanism:** emptying Recently Deleted hides assets from every interface, but iCloud retains the underlying data server-side for a window (~30 days, ostensibly for data recovery/legal reasons). In other words, it's gone from your photos library, your Recently Deleted, all your devices, Trash, everything, you can't see it anywhere... But it's still silently hiding in Apple's servers somewhere. When you upload a JPEG that is *byte-identical* to the JPEG half of a pair the server still holds, iCloud's deduplication re-links your "new" photo to the old asset — RAW included — and syncs it back down to all your devices. I did a quick A/B test to confirm this: a reimported JPEG with a single byte changed stays JPEG-only; an identical control resurrects its RAW.

**The fix:** Rawshuck's commit step offers to **mark** each kept JPEG whose RAW is being deleted, by inserting a standard JPEG comment segment (`COM`) containing a unique ID. This changes the file's checksum so iCloud can't match it. The image bitstream is not re-encoded — pixels are bit-identical, EXIF and capture dates untouched, file grows by ~50 bytes. The mark is on by default; untick it for photos that have never been in your Photos library (e.g. fresh off the SD card), where there's nothing to resurrect.

Safety interlock: if a JPEG can't be marked (e.g. corrupt file), its RAW is deliberately *not* deleted, since deleting it would set up exactly the silent-resurrection scenario.

Note: non-JPEG images (HEIC, PNG, TIFF) losing their RAW can't be marked this way; the commit dialog warns you when that applies.

## Recommended workflows

**Fresh shoot (sorting prior to first import):** copy the SD card to a staging folder → cull → commit → import survivors into Photos in one batch → empty/format the card once verified. iCloud never sees the culled RAWs, so no JPEG marking is even needed.

**Back catalogue:** select photos in Photos → File → Export → Export Unmodified Originals → cull the exported folder → commit (leave marking on) → delete the originals from Photos and empty Recently Deleted → reimport the survivors in one batch.

## Safety notes

- Deleted files go to the macOS Trash, not oblivion. Still: keep your SD card (or the Photos originals) until you've verified the result.
- The script binds to `127.0.0.1` only and validates every requested file against its scan — it cannot touch files outside what you selected.
- The first commit triggers a one-time macOS prompt asking to let the script control Finder (that's how files reach the Trash). Approve it.

## Files

- `rawshuck.py` — the app. This is all you need.
- `jpeg-mark.py` — standalone version of the JPEG-marking utility, for marking files outside the cull workflow (`python3 jpeg-mark.py folder-or-files`).
- `web/index.html` — the [web version](#web-version), a single static page.

## License

GPL-3.0 — see [LICENSE](LICENSE).
