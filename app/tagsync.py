#!/usr/bin/env python3
"""Write Audiobookshelf's book metadata into the audio file tags, in place.

ABS is the source of truth for books already in the library. Its own "embed
metadata" rewrites every file through ffmpeg and drops the tags it doesn't know
(ASIN, SERIES, SUBTITLE, ...). This writes every field with mutagen instead: no
re-encode, same file (same inode), chapters and cover left alone. Only tags whose
value differs are touched, and an empty ABS field never clears a tag.

    abs_tag_sync.py plan  <absdatabase.sqlite> <library> <report.csv> [files.txt]
    abs_tag_sync.py apply <absdatabase.sqlite> <library> <report.csv> [files.txt]
    abs_tag_sync.py undo  <library> <report.csv>

<absdatabase.sqlite> is a COPY of the ABS database. <library> is where ABS's
library folder (ABS_LIB_ROOT, as the ABS container sees it) is mounted. files.txt
optionally limits the run to those audio files (paths as ABS sees them). The apply report keeps every old value, so
undo puts them back.
"""

import csv
import html
import json
import os
import re
import sqlite3
import sys

import mutagen
from mutagen.id3 import ID3, COMM, TALB, TCOM, TCON, TDRC, TIT1, TIT2, TIT3, TPE1, TPE2, TPUB, TXXX
from mutagen.mp4 import MP4, MP4FreeForm

ABS_ROOT = os.environ.get("ABS_LIB_ROOT", "/audiobooks").rstrip("/")


# === ABS data ===

def plain(text):
    """ABS descriptions can hold HTML; tags get plain text."""
    text = re.sub(r"(?i)<br\s*/?>|</p>\s*<p[^>]*>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


def load_abs(db_path):
    """{audio file path: wanted tags} from a copy of the ABS database."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    series, authors = {}, {}
    # Series in the order ABS shows them (the order they were added); its own embed sorts them A-Z
    for r in con.execute("select bs.bookId, s.name, bs.sequence from bookSeries bs "
                         "join series s on s.id = bs.seriesId order by bs.rowid"):
        series.setdefault(r["bookId"], []).append((r["name"], r["sequence"] or ""))
    for r in con.execute("select ba.bookId, a.name from bookAuthors ba join authors a on a.id = ba.authorId "
                         "order by ba.createdAt, ba.rowid"):
        authors.setdefault(r["bookId"], []).append(r["name"])

    files = {}
    for r in con.execute("select * from books"):
        b = series.get(r["id"], [])
        tags = {
            "title": r["title"] or "",
            "album": r["title"] or "",
            "artist": ", ".join(authors.get(r["id"], [])),
            "albumartist": ", ".join(authors.get(r["id"], [])),
            "narrator": ", ".join(json.loads(r["narrators"] or "[]")),
            "genre": "; ".join(json.loads(r["genres"] or "[]")),
            "year": (re.findall(r"\d{4}", r["publishedYear"] or "") or [""])[0],
            "description": plain(r["description"]),
            "grouping": "; ".join(n + (f" #{s}" if s else "") for n, s in b),
            "series": b[0][0] if b else "",
            "series-part": b[0][1] if b else "",
            "subtitle": r["subtitle"] or "",
            "asin": r["asin"] or "",
            "isbn": r["isbn"] or "",
            "publisher": r["publisher"] or "",
            "language": r["language"] or "",
        }
        # Series and subtitle follow ABS even when empty: a series or subtitle removed in ABS (or an
        # unnumbered series) must not leave an old value in the file
        tags = {k: v for k, v in tags.items() if v or k in ("series", "series-part", "grouping", "subtitle")}
        for af in json.loads(r["audioFiles"] or "[]"):
            if not af.get("exclude"):
                files[af["metadata"]["path"]] = tags
    return files


# === Tag formats ===
# Each format maps a field to how it is read, written and removed.

FF = "----:com.apple.iTunes:"
MP4_ATOMS = {"title": "©nam", "album": "©alb", "artist": "©ART", "albumartist": "aART", "narrator": "©wrt",
             "genre": "©gen", "year": "©day", "description": "desc", "grouping": "©grp"}
MP4_FREEFORM = {"series": "SERIES", "series-part": "SERIES-PART", "subtitle": "SUBTITLE", "asin": "ASIN",
                "isbn": "ISBN", "publisher": "publisher", "language": "LANGUAGE"}


class Mp4Tags:
    def __init__(self, f):
        self.f = f

    def _ff_keys(self, field):
        name = (FF + MP4_FREEFORM[field]).lower()
        return [k for k in self.f.tags.keys() if k.lower() == name]

    def get(self, field):
        if field in MP4_ATOMS:
            v = self.f.tags.get(MP4_ATOMS[field])
            return "; ".join(str(x) for x in v) if v else ""
        keys = self._ff_keys(field)
        v = self.f.tags[keys[0]] if keys else None
        return bytes(v[0]).decode("utf-8", "replace") if v else ""

    def set(self, field, value):
        self.delete(field)
        if not value:
            return
        if field in MP4_ATOMS:
            values = value.split("; ") if field == "genre" else [value]
            self.f.tags[MP4_ATOMS[field]] = values
            if field == "description":
                self.f.tags["©cmt"] = [value]
        else:
            self.f.tags[FF + MP4_FREEFORM[field]] = [MP4FreeForm(value.encode("utf-8"))]

    def delete(self, field):
        keys = [MP4_ATOMS[field]] if field in MP4_ATOMS else self._ff_keys(field)
        for k in keys:
            self.f.tags.pop(k, None)


ID3_FRAMES = {"title": TIT2, "album": TALB, "artist": TPE1, "albumartist": TPE2, "narrator": TCOM,
              "genre": TCON, "year": TDRC, "grouping": TIT1, "subtitle": TIT3, "publisher": TPUB}
ID3_TXXX = {"series": "SERIES", "series-part": "SERIES-PART", "asin": "ASIN", "isbn": "ISBN", "language": "LANGUAGE"}


class Id3Tags:
    def __init__(self, f):
        self.f = f

    def _txxx(self, field):
        """Custom TXXX frames named like the field, any letter case ("TXXX:grouping")."""
        want = f"txxx:{field}".lower()
        return [k for k in self.f.tags.keys() if k.lower() == want]

    def get(self, field):
        if field == "description":
            # skip the "ID3v1 Comment" frame mutagen builds from an old ID3v1 tag
            frames = [c for c in self.f.tags.getall("COMM") if c.desc == ""] or self.f.tags.getall("COMM")
            return str(frames[0].text[0]) if frames and frames[0].text else ""
        if field in ID3_FRAMES:
            frame = self.f.tags.get(ID3_FRAMES[field].__name__)
            value = "; ".join(str(t) for t in frame.text) if frame and frame.text else ""
            # an old custom frame of the same name ("TXXX:grouping") is read by other tools: it has to agree
            extra = {"; ".join(str(t) for t in self.f.tags[k].text) for k in self._txxx(field)}
            return value if not extra or extra == {value} else " | ".join(sorted(extra | {value}))
        # every TXXX frame of this name, any letter case; two that disagree read as a mismatch
        want = f"TXXX:{ID3_TXXX[field]}".lower()
        values = {"; ".join(str(t) for t in self.f.tags[k].text) for k in self.f.tags.keys() if k.lower() == want}
        return values.pop() if len(values) == 1 else " | ".join(sorted(values))

    def set(self, field, value):
        self.delete(field)
        if not value:
            return
        if field == "description":
            self.f.tags.add(COMM(encoding=3, lang="eng", desc="", text=value))
        elif field in ID3_FRAMES:
            text = value.split("; ") if field == "genre" else value
            self.f.tags.add(ID3_FRAMES[field](encoding=3, text=text))
        else:
            self.f.tags.add(TXXX(encoding=3, desc=ID3_TXXX[field], text=value))

    def delete(self, field):
        if field == "description":
            self.f.tags.delall("COMM")
        elif field in ID3_FRAMES:
            self.f.tags.delall(ID3_FRAMES[field].__name__)
            for key in self._txxx(field):
                self.f.tags.delall(key)
        else:
            # Any letter case: an old "TXXX:series" next to "TXXX:SERIES" is read by other tools
            want = f"TXXX:{ID3_TXXX[field]}".lower()
            for key in [k for k in self.f.tags.keys() if k.lower() == want]:
                self.f.tags.delall(key)


VORBIS = {"title": "TITLE", "album": "ALBUM", "artist": "ARTIST", "albumartist": "ALBUMARTIST",
          "narrator": "COMPOSER", "genre": "GENRE", "year": "DATE", "description": "DESCRIPTION",
          "grouping": "GROUPING", "series": "SERIES", "series-part": "SERIES-PART", "subtitle": "SUBTITLE",
          "asin": "ASIN", "isbn": "ISBN", "publisher": "PUBLISHER", "language": "LANGUAGE"}


class VorbisTags:
    def __init__(self, f):
        self.f = f

    def get(self, field):
        v = self.f.tags.get(VORBIS[field])
        return "; ".join(v) if v else ""

    def set(self, field, value):
        self.delete(field)
        if value:
            self.f.tags[VORBIS[field]] = value.split("; ") if field == "genre" else [value]

    def delete(self, field):
        if VORBIS[field] in self.f.tags:
            del self.f.tags[VORBIS[field]]


def open_tags(path):
    f = mutagen.File(path)
    if f is None:
        return None, None
    if isinstance(f, MP4):
        if f.tags is None:
            f.add_tags()
        return f, Mp4Tags(f)
    if isinstance(getattr(f, "tags", None), ID3) or path.lower().endswith(".mp3"):
        if f.tags is None:
            f.add_tags()
        return f, Id3Tags(f)
    if getattr(f, "tags", None) is not None and hasattr(f.tags, "get"):
        return f, VorbisTags(f)
    return f, None


# === Modes ===

def local_path(library, abs_path):
    return library + abs_path[len(ABS_ROOT):] if abs_path.startswith(ABS_ROOT + "/") else None


def same(field, current, wanted):
    # ABS keeps only the year; a full date in the file ("2019-06-17") already agrees with it
    if field == "year":
        return current[:4] == wanted[:4] and bool(current)
    return current == wanted


def sync(mode, db_path, library, report, only=None):
    files = load_abs(db_path)
    paths = only if only is not None else sorted(p for p in files if p.startswith(ABS_ROOT + "/"))
    counts = {}
    with open(report, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(["file", "result", "changes"])
        for abs_path in paths:
            wanted, local, changes = files.get(abs_path), local_path(library, abs_path), {}
            if wanted is None or local is None:
                result = "not in ABS"
            elif not os.path.isfile(local):
                result = "missing"
            else:
                try:
                    f, tags = open_tags(local)
                    if tags is None:
                        result = "unsupported"
                    else:
                        changes = {k: [tags.get(k), v] for k, v in wanted.items() if not same(k, tags.get(k), v)}
                        if not changes:
                            result = "ok"
                        elif mode == "plan":
                            result = "would update"
                        else:
                            for k, (_, v) in changes.items():
                                tags.set(k, v)
                            f.save()
                            result = "updated"
                except Exception as e:  # keep going; the report says what failed
                    result, changes = f"error: {e}", {}
            key = result.split(":")[0]
            counts[key] = counts.get(key, 0) + 1
            w.writerow([abs_path, result, json.dumps(changes, ensure_ascii=False) if changes else ""])
    print(json.dumps(counts))


def undo(library, report):
    counts = {}
    with open(report, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["result"] == "updated"]
    for r in rows:
        try:
            f, tags = open_tags(local_path(library, r["file"]))
            for k, (old, _) in json.loads(r["changes"]).items():
                tags.set(k, old)  # an empty old value removes the tag again
            f.save()
            result = "restored"
        except Exception as e:
            result = "error"
            print(f"{r['file']}: {e}", file=sys.stderr)
        counts[result] = counts.get(result, 0) + 1
    print(json.dumps(counts))


def main():
    a = sys.argv[1:]
    if a and a[0] in ("plan", "apply") and len(a) in (4, 5):
        only = None
        if len(a) == 5:
            with open(a[4], encoding="utf-8") as fh:
                only = list(dict.fromkeys(line.strip() for line in fh if line.strip()))
        sync(a[0], a[1], a[2], a[3], only)
    elif a and a[0] == "undo" and len(a) == 3:
        undo(a[1], a[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
