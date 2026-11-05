# Shelfwright

Audiobook library upkeep for [Audiobookshelf](https://www.audiobookshelf.org/), with optional checks for Chaptarr (a Readarr fork).

Shelfwright scans the library and lists problems with a proposed fix for each. Nothing changes until you approve the fix in the web UI. You can let a small set of safe fixes run on their own (`AUTO_APPLY_SAFE=true`).

Audiobookshelf is treated as the source of truth: file tags, folder names and Chaptarr's records are brought in line with it, never the other way round.

## What it checks

| Area | Checks |
|---|---|
| Audiobookshelf | dramatized-adaptation formatting, titles with "(Unabridged)" or series positions, author and series spelling variants, author initials, duplicates, missing books, double entries |
| Files | tags that differ from ABS, folders not following the naming standard (and collisions), books to convert to m4b, original tracks ABS kept after a conversion, empty folders |
| Chaptarr (optional) | file records for deleted files, unlinked files, author folders, files linked to another author's book, files linked to the wrong book by the same author |

## Folder naming

```
Author/Series/Pos Title/Pos Title.m4b      books in a series
Author/Title/Title.m4b                     standalone books
```

`app/universes.json` lists umbrella series that should be filed first (e.g. a shared universe over its sub-series).

## Converting to m4b

Books not in a single m4b are proposed for conversion, keeping the original audio quality:

- **AAC, ALAC or E-AC-3 audio** (multi-file m4b/m4a, most GraphicAudio) is **joined losslessly** with ffmpeg stream copy: no re-encode, the audio is bit-for-bit the original. Chapters and cover come from Audiobookshelf.
- **MP3, Ogg and others** need a re-encode to AAC. They are only proposed when `CONVERT_REENCODE=true`, and then use Audiobookshelf's own **Encode M4B** with a bitrate worked out per book for the least extra loss: per channel, 1.5× the source (the headroom ffmpeg's AAC encoder needs over MP3), at least 48 and at most 128 kbps per channel (where it is transparent; more can't add quality the source never had). Mono stays mono, the sample rate is kept. Examples: 64k mono MP3 → 96k mono AAC, 128k stereo → 192k stereo, 192k+ stereo → 256k. `CONVERT_MAX_BITRATE` (default 0, none) caps the total.

Either way the files are swapped the way Audiobookshelf's encoder does it (the new m4b is written into the first track's file and renamed), so the ABS item, its metadata and listening progress stay intact. The joined file is checked (codec and length) before any original is touched. Afterwards Shelfwright writes the full tags from ABS into the new file and updates Chaptarr's record. The original tracks are kept in ABS's metadata cache; after `BACKUP_KEEP_DAYS` Shelfwright offers to delete them.

## Install

Image: `ghcr.io/adarack/shelfwright:latest`. An Unraid template is in [`unraid/shelfwright.xml`](unraid/shelfwright.xml).

| Mount | Purpose |
|---|---|
| `/library` (rw) | the audiobook library |
| `/abs-config` (ro) | Audiobookshelf's config folder; `absdatabase.sqlite` is copied before it is read, never opened live |
| `/abs-metadata` (rw) | Audiobookshelf's metadata folder (conversion backups) |
| `/data` (rw) | Shelfwright's database, logs, backups and trash |

| Variable | |
|---|---|
| `ABS_URL`, `ABS_TOKEN`, `ABS_LIBRARY_ID` | Audiobookshelf URL, an admin API key, the library id |
| `ABS_LIB_ROOT` | the library path as the ABS container sees it |
| `CHAPTARR_URL`, `CHAPTARR_KEY`, `CHAPTARR_LIB_ROOT` | Chaptarr API and its library path (optional) |
| `PG_HOST`, `PG_PORT`, `PG_USER`, `PG_PASSWORD`, `PG_DB` | Chaptarr's PostgreSQL main database; Chaptarr checks are off unless all are set |
| `AUTO_APPLY_SAFE` | `true` lets safe fixes run after each scan (default `false`) |
| `SCAN_INTERVAL_HOURS` | hours between scans (default 24) |
| `CONVERT_REENCODE` | `true` also proposes MP3/Ogg books (re-encoded, small quality loss); default `false` |
| `CONVERT_MAX_BITRATE`, `BACKUP_KEEP_DAYS` | re-encode bitrate cap (default 0 = none), days to keep originals |

The web UI listens on port 8484.

## Safety

- Files are moved, never deleted: removed duplicates go to `/data/trash`.
- Every folder move writes an undo log to `/data/logs`; Chaptarr tables are dumped before they are changed.
- Audiobookshelf items are never hard-deleted.

## Versions

The version is in [`VERSION`](VERSION). Every build of `main` publishes `ghcr.io/adarack/shelfwright:latest` and `:<version>`, and sets the image's `org.opencontainers.image.version` label (which Unraid shows). Raising `VERSION` also creates a GitHub release `v<version>`. Use `:latest` to follow updates, or pin a version.

## License

MIT, see [LICENSE](LICENSE).
