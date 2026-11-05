"""Library checks. Each yields findings: (check, key, title, summary, detail, action, safe).

An action is either {"type": ...} (one fix) or {"type": "choose", "options": [{"label", "action"}, ...]}.
Safe findings are applied automatically when AUTO_APPLY_SAFE is on; the rest wait for approval.
"""
import collections, json, os, re, unicodedata

import config
from clients import abs_to_local, chaptarr_to_local

AUDIO_EXT = {".m4b", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma", ".wav", ".mp4"}
DRAMA_RE = re.compile(r"\s*[\[(]\s*dramati[sz]ed(?:\s+adaptation)?\s*[\])]|\s+-\s*graphic\s*audio\s*$", re.I)
DRAMA_PUB = re.compile(config.DRAMA_PUBLISHERS, re.I)
UNDOTTED_OK = set(filter(None, os.environ.get("UNDOTTED_AUTHORS_OK", "ND Stevenson,RJ Barker,TR Cameron").split(",")))


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", s)


def F(check, key, title, summary, detail=None, action=None, safe=False):
    return {"check": check, "key": str(key), "title": title, "summary": summary, "detail": detail or {}, "action": action, "safe": safe}


class Context:
    """Everything the checks read, loaded once per scan."""

    def __init__(self, db, pg=None):
        self.db = db
        lib = config.ABS_LIBRARY_ID
        self.items = [dict(r) for r in db.execute(
            "select li.id, li.relPath, li.path, li.isMissing, li.createdAt, b.id bookId, b.title, b.subtitle, b.publisher, b.asin, b.isbn, "
            "b.tags, b.narrators, b.audioFiles, b.duration from libraryItems li join books b on b.id = li.mediaId where li.libraryId = ?", (lib,))]
        self.authors = collections.defaultdict(list)
        for r in db.execute("select ba.bookId, a.id, a.name from bookAuthors ba join authors a on a.id = ba.authorId order by ba.createdAt, ba.rowid"):
            self.authors[r["bookId"]].append({"id": r["id"], "name": r["name"]})
        self.series = collections.defaultdict(list)
        for r in db.execute("select bs.bookId, s.id, s.name, bs.sequence from bookSeries bs join series s on s.id = bs.seriesId order by bs.rowid"):
            self.series[r["bookId"]].append({"id": r["id"], "name": r["name"], "sequence": r["sequence"] or ""})
        self.progress = {r["mediaItemId"] for r in db.execute("select distinct mediaItemId from mediaProgresses")}
        for it in self.items:
            it["all_files"] = json.loads(it["audioFiles"] or "[]")
            it["files"] = [a for a in it["all_files"] if not a.get("exclude")]
            it["tags_list"] = json.loads(it["tags"] or "[]")
            it["authors"] = self.authors.get(it["bookId"], [])
            it["series_list"] = self.series.get(it["bookId"], [])
        self.pg = pg
        self.ch = None
        if pg:
            self.ch = {
                "files": pg.rows('select bf."Id" id, bf."Path" path, bf."Size" size, bf."EditionId" edition, e."BookId" book, b."AuthorId" author '
                                 'from "BookFiles" bf left join "Editions" e on e."Id" = bf."EditionId" left join "Books" b on b."Id" = e."BookId"'),
                "books": {r["id"]: r for r in pg.rows('select "Id" id, "Title" title, "AuthorId" author, "AudiobookMonitored" monitored from "Books"')},
                "authors": {r["id"]: r for r in pg.rows('select "Id" id, "Name" name, "Path" path, coalesce("AudiobookPath", \'\') apath from "Authors"')},
                "audio_books": {r["book"] for r in pg.rows('select distinct "BookId" book from "Editions" where "Format" = \'audiobook\'')},
            }


# === Audiobookshelf data ===

def check_drama(ctx):
    """Dramatized books: title '... [Dramatized Adaptation]', no marker in the subtitle, every series dramatized, the tag."""
    for it in ctx.items:
        if it["isMissing"]:
            continue
        text = f'{it["title"] or ""} {it["subtitle"] or ""}'
        if not (re.search(r"dramati[sz]", text, re.I) or DRAMA_PUB.search(it["publisher"] or "")):
            continue
        title = DRAMA_RE.sub("", it["title"] or "").strip() + " " + config.DRAMA
        sub = DRAMA_RE.sub("", it["subtitle"] or "").strip(" :-")
        series = [s["name"] if "dramati" in s["name"].lower() else DRAMA_RE.sub("", s["name"]).strip() + " " + config.DRAMA for s in it["series_list"]]
        changes = {}
        if title != it["title"]: changes["title"] = [it["title"], title]
        if sub != (it["subtitle"] or ""): changes["subtitle"] = [it["subtitle"], sub]
        if series != [s["name"] for s in it["series_list"]]: changes["series"] = [[s["name"] for s in it["series_list"]], series]
        if config.TAG not in it["tags_list"]: changes["tag"] = ["", config.TAG]
        if changes:
            yield F("drama", it["id"], it["title"], "dramatized formatting: " + ", ".join(changes), {"path": it["relPath"], "changes": changes},
                    {"type": "abs_drama", "item": it["id"]}, safe=True)


def check_missing(ctx):
    for it in ctx.items:
        if it["isMissing"]:
            yield F("abs_missing", it["id"], it["title"], "in ABS but the files are gone", {"path": it["relPath"]},
                    {"type": "choose", "options": [{"label": "Remove from ABS", "action": {"type": "abs_delete", "item": it["id"]}}]})


def check_double_entries(ctx):
    """Two ABS entries for one folder (happens when a scan and the watcher both add a moved book)."""
    by = collections.defaultdict(list)
    for it in ctx.items:
        by[it["relPath"]].append(it)
    for path, its in by.items():
        if len(its) > 1:
            its.sort(key=lambda i: i["createdAt"])
            extra = [i for i in its[1:] if i["bookId"] not in ctx.progress]
            if extra:
                yield F("abs_double", path, its[0]["title"], f"{len(its)} ABS entries for one folder", {"path": path, "entries": [i["id"] for i in its]},
                        {"type": "abs_delete_many", "items": [i["id"] for i in extra]}, safe=True)


def check_duplicates(ctx):
    """The same book twice: same first author + title + series position, or the same ASIN (GraphicAudio parts excepted)."""
    key = collections.defaultdict(list)
    for it in ctx.items:
        if it["isMissing"]:
            continue
        a = it["authors"][0]["name"] if it["authors"] else ""
        s = it["series_list"][0] if it["series_list"] else {"name": "", "sequence": ""}
        part = re.search(r"\((?:part\s*)?\d+ of \d+\)", it["title"] or "", re.I)
        key[(norm(a), norm(DRAMA_RE.sub("", it["title"] or "")), s["sequence"], part.group(0) if part else "", "[Dramatized" in (it["title"] or ""))].append(it)
    # the same ASIN is the same book too (parts of a split book share theirs, so the part number stays in the key)
    for it in ctx.items:
        if it["isMissing"] or not (it["asin"] or "").strip():
            continue
        part = re.search(r"\((?:part\s*)?\d+ of \d+\)", it["title"] or "", re.I)
        key[("asin", it["asin"].strip().upper(), part.group(0) if part else "")].append(it)
    done = set()
    for k, its in key.items():
        ids = frozenset(i["id"] for i in its)
        if len(ids) < 2 or ids in done:
            continue
        done.add(ids)
        its = list({i["id"]: i for i in its}.values())
        if len(its) > 1:
            opts = [{"label": f"Keep {keep['relPath']}", "action": {"type": "abs_delete_files", "items": [o["id"] for o in its if o is not keep]}} for keep in its]
            yield F("duplicate", "|".join(sorted(i["id"] for i in its)), its[0]["title"], f"{len(its)} copies",
                    {"copies": [{"path": i["relPath"], "duration_h": round((i["duration"] or 0) / 3600, 1), "files": len(i["files"]),
                                 "progress": i["bookId"] in ctx.progress} for i in its]},
                    {"type": "choose", "options": opts})


def check_author_variants(ctx):
    names = collections.defaultdict(set)
    for it in ctx.items:
        for a in it["authors"]:
            names[re.sub(r"[^a-z]", "", a["name"].lower())].add(a["name"])
    for k, v in names.items():
        if len(v) > 1:
            v = sorted(v, key=lambda n: (-n.count("."), n))
            yield F("author_variant", k, " / ".join(v), "one author spelled more ways",
                    {"spellings": v}, {"type": "choose", "options": [{"label": f"Use {n}", "action": {"type": "abs_merge_author", "into": n, "names": v}} for n in v]})
    for n in sorted({a["name"] for it in ctx.items for a in it["authors"]}):
        if n not in UNDOTTED_OK and (re.match(r"^([A-Z]{2,3}|[A-Z] [A-Z])\s+[A-Z][a-z]", n) or re.match(r"^[A-Z] [A-Z] ", n)):
            m = re.match(r"^([A-Z])\s?([A-Z])\s?([A-Z]?)\s+(.*)$", n)
            if m:
                new = ".".join(x for x in m.groups()[:3] if x) + ". " + m.group(4)
                yield F("author_initials", n, n, f"initials without dots: {new}?", {},
                        {"type": "choose", "options": [{"label": f"Rename to {new}", "action": {"type": "abs_rename_author", "name": n, "new": new}}]})


def check_series_variants(ctx):
    names = collections.defaultdict(set)
    for it in ctx.items:
        for s in it["series_list"]:
            base = re.sub(r"^(the|a)\s+|\s+(series|saga|trilogy|chronicles)$", "", s["name"].replace(config.DRAMA, "").strip(), flags=re.I)
            names[norm(base) + ("D" if config.DRAMA in s["name"] else "")].add(s["name"])
    for k, v in names.items():
        if len(v) > 1:
            v = sorted(v)
            yield F("series_variant", k, " / ".join(v), "one series spelled more ways", {"names": v},
                    {"type": "choose", "options": [{"label": f"Use {n}", "action": {"type": "abs_merge_series", "into": n, "names": v}} for n in v]})


UNABRIDGED = re.compile(r"\s*[\[(]\s*unabridged\s*[\])]", re.I)
POSITION = re.compile(r"[\s,:-]*\(?\b(books?|volume|vol|part)\s*[#.,]?\s*(\d+(?:\s*-\s*\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|i{1,3}|iv|v|vi{0,3}|ix|x)\b(\s*out of \d+)?\)?\s*$", re.I)


def check_titles(ctx):
    for it in ctx.items:
        t = it["title"] or ""
        new = UNABRIDGED.sub("", t).strip()
        if it["series_list"] and not re.search(r"\(\d+ of \d+\)", new):
            new = POSITION.sub("", new).strip() or new
        if new != t:
            yield F("title", it["id"], t, f"title → {new}", {"path": it["relPath"]},
                    {"type": "choose", "options": [{"label": f"Rename to “{new}”", "action": {"type": "abs_patch", "item": it["id"], "metadata": {"title": new}}}]})


# === Files ===

def check_tags(ctx, tag_report):
    """Files whose tags differ from ABS (from a tag-sync dry run)."""
    files = [r["file"] for r in tag_report if r["result"] == "would update"]
    if files:
        yield F("tag_sync", "all", f"{len(files)} files", "file tags differ from ABS", {"files": files[:200], "count": len(files)},
                {"type": "tag_sync", "files": files}, safe=True)


def check_naming(ctx, plan_rows, pending=()):
    """pending: folders whose file tags still differ from ABS. Their planned names come from the stale tags,
    so they are held back until the tag sync has run (the standardize fix syncs tags first anyway)."""
    pending = set(pending)
    plan_rows = [r for r in plan_rows if r["current"] not in pending]
    todo = [r for r in plan_rows if r["action"] in ("move", "rename")]
    for r in plan_rows:
        if r["action"] == "collision":
            yield F("collision", r["current"], r["current"], r["note"], {"target": r["target"]})
    if todo:
        yield F("naming", "plan", f"{len(todo)} folders", "not following the naming standard",
                {"moves": [{"from": r["current"], "to": r["target"]} for r in todo[:500]], "count": len(todo)},
                {"type": "choose", "options": [{"label": f"Move/rename {len(todo)} folders", "action": {"type": "standardize"}}]})


def check_empty_dirs(ctx):
    empty = []
    for root, dirs, files in os.walk(config.LIBRARY, topdown=False):
        if root != config.LIBRARY and not os.listdir(root):
            empty.append(root[len(config.LIBRARY) + 1:])
    if empty:
        yield F("empty_dirs", "all", f"{len(empty)} empty folders", "empty folders in the library", {"folders": empty[:300]},
                {"type": "remove_empty_dirs"}, safe=True)


COPY_CODECS = {"aac", "alac", "eac3", "ac3"}   # audio an m4b can hold as it is


def in_library(it):
    """Items in the library folder this container works on (an ABS library can have more folders)."""
    return (it["path"] or "").startswith(config.ABS_LIB_ROOT + "/")


def foreign_files(it):
    """Audio files an ABS item lists outside its own folder. ABS tracks files by inode; when a moved book's old
    inode is reused, a stale entry can stay attached to another book (wrong track, length and chapters)."""
    return [f for f in it["all_files"] if not f["metadata"]["path"].startswith((it["path"] or "") + "/")]


def check_foreign_tracks(ctx):
    for it in ctx.items:
        bad = foreign_files(it) if in_library(it) else []
        if bad:
            yield F("abs_foreign", it["id"], it["title"], f"ABS lists {len(bad)} audio file(s) from another folder in this book (wrong length/chapters)",
                    {"path": it["relPath"], "foreign": [f["metadata"]["path"] for f in bad],
                     "listening_progress": it["bookId"] in ctx.progress},
                    {"type": "choose", "options": [{"label": "Re-add the book from its folder (metadata.json, cover and collections kept)",
                                                    "action": {"type": "abs_readd", "item": it["id"]}}]})


def reencode_plan(files):
    """(kbps, channels) for re-encoding lossy audio (MP3, Ogg, ...) to AAC with as little extra loss as possible.

    Worked out per channel, from the book's highest-bitrate file: 1.5x the source (ffmpeg's built-in AAC encoder
    needs that headroom over MP3 to add no audible loss of its own), at least 48 kbps per channel, and at most
    128 kbps per channel, where the encoder is transparent and more bitrate can't add quality the source never had.
    Mono stays mono, the sample rate is kept. CONVERT_MAX_BITRATE, when set, caps the total."""
    channels = max((f.get("channels") or 2) for f in files)
    per_ch = max(((f.get("bitRate") or 0) / 1000) / max(1, f.get("channels") or channels) for f in files) or 64
    target = min(128, max(48, per_ch * 1.5))
    kbps = int(-(-target * channels // 16) * 16)   # round up to a multiple of 16
    if config.CONVERT_MAX_BITRATE:
        kbps = min(kbps, config.CONVERT_MAX_BITRATE)
    return kbps, channels


def check_conversion(ctx):
    """Books not in a single m4b. Audio an m4b can hold as it is gets joined losslessly; anything else needs a
    re-encode and is only proposed when CONVERT_REENCODE is on."""
    for it in ctx.items:
        if it["isMissing"] or not it["files"] or not in_library(it) or foreign_files(it):
            continue
        exts = {os.path.splitext(f["metadata"]["path"])[1].lower() for f in it["files"]}
        if len(it["files"]) == 1 and exts == {".m4b"}:
            continue
        codecs = {(f.get("codec") or "?").lower() for f in it["files"]}
        br = round(max((f.get("bitRate") or 0) for f in it["files"]) / 1000)
        detail = {"path": it["relPath"], "files": len(it["files"]), "formats": sorted(exts), "codecs": sorted(codecs), "bitrate_kbps": br}
        if len(codecs) == 1 and codecs <= COPY_CODECS:
            yield F("convert", it["id"], it["title"], f"{len(it['files'])} file(s), {', '.join(sorted(codecs))} ~{br} kbps: lossless join, quality unchanged",
                    detail, {"type": "choose", "options": [{"label": "Join into one m4b (lossless)", "action": {"type": "convert", "item": it["id"], "mode": "copy"}}]})
        elif config.CONVERT_REENCODE:
            to, ch = reencode_plan(it["files"])
            layout = {1: "mono", 2: "stereo"}.get(ch, f"{ch} ch")
            yield F("convert", it["id"], it["title"],
                    f"{len(it['files'])} file(s), {', '.join(sorted(codecs))} ~{br} kbps {layout}: re-encode to AAC {to} kbps {layout}",
                    {**detail, "target_kbps": to, "channels": ch},
                    {"type": "choose", "options": [{"label": f"Re-encode to m4b, AAC {to} kbps {layout}", "action": {"type": "convert", "item": it["id"], "mode": "reencode"}}]})


def check_backups(ctx):
    """Original tracks ABS kept after an m4b encode (metadata/cache/items/<id>)."""
    import time
    root = os.path.join(config.ABS_METADATA_DIR, "cache", "items")
    if not os.path.isdir(root):
        return
    for d in os.listdir(root):
        p = os.path.join(root, d)
        audio = [f for f in os.listdir(p) if os.path.splitext(f)[1].lower() in AUDIO_EXT] if os.path.isdir(p) else []
        if audio and time.time() - os.path.getmtime(p) > config.BACKUP_KEEP_DAYS * 86400:
            size = sum(os.path.getsize(os.path.join(p, f)) for f in audio) // 2**20
            yield F("backup", d, d, f"{len(audio)} original tracks kept by ABS ({size} MB)", {"dir": p},
                    {"type": "choose", "options": [{"label": "Delete the originals", "action": {"type": "delete_backup", "dir": d}}]})


# === Chaptarr ===

def check_chaptarr(ctx):
    if not ctx.ch:
        return
    ch = ctx.ch
    lib = config.CHAPTARR_LIB_ROOT + "/"
    files = [f for f in ch["files"] if f["path"].startswith(lib)]
    # file records whose file is gone, grouped per book
    gone = collections.defaultdict(list)
    for f in files:
        local = chaptarr_to_local(f["path"])
        if local and not os.path.exists(local):
            gone[f["book"] or "none:" + f["id"]].append(f)
        if not f["edition"] or not f["book"]:
            yield F("ch_unlinked", f["id"], f["path"][len(lib):], "Chaptarr file not linked to a book", {"path": f["path"]})
    for bid, fs in gone.items():
        book = ch["books"].get(bid, {})
        ids = [f["id"] for f in fs]
        mon = book.get("monitored") == "t"
        yield F("ch_stale", bid, book.get("title") or fs[0]["path"][len(lib):], f"Chaptarr still lists {len(fs)} deleted file(s)" + (" — book is monitored" if mon else ""),
                {"folder": "/".join(fs[0]["path"][len(lib):].split("/")[:-1]), "book": bid, "monitored": mon},
                {"type": "choose", "options": [
                    {"label": "Remove the records, keep monitored (re-download)", "action": {"type": "ch_remove_files", "files": ids, "book": bid, "unmonitor": False}},
                    {"label": "Remove the records and unmonitor", "action": {"type": "ch_remove_files", "files": ids, "book": bid, "unmonitor": True}}]})
    # author folders: Path/AudiobookPath must be where that author's files are (same person only)
    tops = collections.defaultdict(collections.Counter)
    for f in files:
        if f["author"]:
            tops[f["author"]][f["path"][len(lib):].split("/")[0]] += 1
    used = collections.Counter(a["apath"].rstrip("/") for a in ch["authors"].values())
    for aid, c in tops.items():
        a = ch["authors"].get(aid)
        if not a:
            continue
        top, n = c.most_common(1)[0]
        want = lib + top
        if a["path"].rstrip("/") == want and a["apath"].rstrip("/") == want:
            continue
        same = norm(re.sub(r"\b(jr|sr)\b", "", top.lower())) in (norm(a["name"]), norm(a["name"].replace(".", ""))) or \
            re.sub(r"[^a-z]", "", top.lower()).startswith(re.sub(r"[^a-z]", "", a["name"].lower())[:6])
        if same and n == sum(c.values()) and used[want] == 0:
            yield F("ch_author_path", aid, a["name"], f"Chaptarr author folder → {top}", {"path": a["path"], "apath": a["apath"], "want": want},
                    {"type": "ch_author_path", "author": aid, "path": want}, safe=True)


def check_chaptarr_links(ctx):
    """Chaptarr file linked to a book by another author (Chaptarr matches on title only)."""
    if not ctx.ch:
        return
    ch = ctx.ch
    by_file = {}
    for it in ctx.items:
        for f in it["files"]:
            by_file[f["metadata"]["path"]] = it
    by_title = collections.defaultdict(list)
    for b in ch["books"].values():
        by_title[norm(b["title"])].append(b)
    groups = collections.defaultdict(list)
    for f in ch["files"]:
        local = chaptarr_to_local(f["path"])
        if not local or not f["book"] or not os.path.exists(local):
            continue
        it = by_file.get(config.ABS_LIB_ROOT + local[len(config.LIBRARY):])
        book = ch["books"].get(f["book"])
        if not it or not book or not it["authors"]:
            continue
        ch_author = ch["authors"].get(book["author"], {}).get("name", "")
        if norm(ch_author) in {norm(a["name"]) for a in it["authors"]}:
            continue
        # only books that have an audiobook edition, monitored ones first
        better = sorted((b for b in by_title[norm(book["title"])] if b["id"] in ch["audio_books"]
                         and norm(ch["authors"].get(b["author"], {}).get("name", "")) == norm(it["authors"][0]["name"])),
                        key=lambda b: (b["monitored"] != "t", int(b["id"])))
        if better:
            groups[(f["book"], better[0]["id"], it["id"])].append(f["id"])
    for (from_book, to_book, item_id), ids in groups.items():
        it = next(i for i in ctx.items if i["id"] == item_id)
        book = ch["books"][from_book]
        wrong = ch["authors"].get(book["author"], {}).get("name", "")
        right = it["authors"][0]["name"]
        yield F("ch_wrong_author", f"{from_book}->{to_book}", book["title"], f"{len(ids)} file(s) linked to {wrong}'s book; ABS says {right}",
                {"path": it["relPath"], "from_book": from_book, "to_book": to_book},
                {"type": "choose", "options": [{"label": f"Link to {right}'s book (monitoring moves with it)", "action": {"type": "ch_relink_book", "files": ids, "book": to_book, "from_book": from_book}}]})


def _title_variants(t):
    """Normalized forms of a title: whole, and each part around ':' or ' - ', without bracketed bits or a dramatized marker."""
    t = DRAMA_RE.sub("", t or "")
    t = re.sub(r"\s*[\[(][^\])]*[\])]", "", t)
    t = re.sub(r"\bversus\b", "vs", t, flags=re.I)
    t = re.sub(r"(?<=[a-z])our\b", "or", t, flags=re.I)        # honour/honor, valour/valor
    t = re.sub(r"(?<=[a-z])ll(?=er\b)", "l", t, flags=re.I)     # traveller/traveler
    parts = [t] + re.split(r":| - ", t)
    return [v for v in (norm(re.sub(r"^\s*(the|a|an)\s+", "", p.strip(), flags=re.I)) for p in parts) if len(v) >= 4]


def _same_title(a, b):
    va, vb = _title_variants(a), _title_variants(b)
    if not va or not vb:
        return True
    return va[0] in vb or vb[0] in va or any(x == y or (len(x) >= 6 and len(y) >= 6 and (x in y or y in x)) for x in va for y in vb)


def check_chaptarr_titles(ctx):
    """Chaptarr file linked to another book by the same author (Chaptarr matches on title only). Reported only when
    an audiobook entry with the book's exact title exists: other mismatches are mostly the same work under another
    title (translations, UK/US titles, an adaptation of a book you also have)."""
    if not ctx.ch:
        return
    ch = ctx.ch
    lib = config.CHAPTARR_LIB_ROOT + "/"
    by_file = {af["metadata"]["path"]: it for it in ctx.items for af in it["files"]}
    by_author = collections.defaultdict(list)
    for b in ch["books"].values():
        by_author[b["author"]].append(b)
    groups = collections.defaultdict(list)
    for f in ch["files"]:
        if not f["path"].startswith(lib) or not f["book"]:
            continue
        it = by_file.get(config.ABS_LIB_ROOT + "/" + f["path"][len(lib):])
        book = ch["books"].get(f["book"])
        if not it or not book or not it["authors"] or _same_title(it["title"], book["title"]):
            continue
        ch_author = ch["authors"].get(book["author"], {}).get("name", "")
        if norm(ch_author) not in {norm(a["name"]) for a in it["authors"]}:
            continue   # another author's book: check_chaptarr_links reports it
        want = _title_variants(it["title"])
        exact = [b for b in by_author[book["author"]] if b["id"] != book["id"] and b["id"] in ch["audio_books"]
                 and want and (_title_variants(b["title"])[:1] == want[:1] or (_title_variants(b["title"]) or [""])[0] in want)]
        exact.sort(key=lambda b: (b["monitored"] != "t", int(b["id"])))
        if exact:
            groups[(f["book"], it["id"], exact[0]["id"])].append(f["id"])
    items = {i["id"]: i for i in ctx.items}
    for (from_book, item_id, to_book), ids in groups.items():
        it, book = items[item_id], ch["books"][from_book]
        unlink = {"label": "Unlink (the right book isn't in Chaptarr)", "action": {"type": "ch_unlink_files", "files": ids}}
        opts = [{"label": f"Link to “{ch['books'][to_book]['title']}”",
                 "action": {"type": "ch_relink_book", "files": ids, "book": to_book, "from_book": from_book, "same_book": False}}, unlink]
        yield F("ch_wrong_title", f"{from_book}:{item_id}", it["title"], f"{len(ids)} file(s) linked to Chaptarr's “{book['title']}”",
                {"path": it["relPath"], "from_book": from_book, "to_book": to_book}, {"type": "choose", "options": opts})
