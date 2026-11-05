"""Carry out a finding's action. Every function takes (action, log) and raises on failure."""
import csv, json, os, re, shutil, subprocess, time

import config, standardize, tagsync
from clients import ABS, Chaptarr, Pg, abs_snapshot, abs_db, q, abs_to_local, local_to_chaptarr, chaptarr_to_local

tagsync.ABS_ROOT = config.ABS_LIB_ROOT
standardize.OWNER = config.OWNER
LOGS = os.path.join(config.DATA, "logs")
os.makedirs(LOGS, exist_ok=True)
DRAMA_RE = re.compile(r"\s*[\[(]\s*dramati[sz]ed(?:\s+adaptation)?\s*[\])]|\s+-\s*graphic\s*audio\s*$", re.I)


def stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _ref(s):
    return {"id": s["id"], "name": s["name"], **({"sequence": s.get("sequence") or ""} if "sequence" in s else {})}


# === tags ===

def tag_sync(a, log, only=None):
    db, done = abs_snapshot()
    try:
        report = os.path.join(LOGS, f"tag-sync-{stamp()}.csv")
        tagsync.sync("apply", db, config.LIBRARY, report, only if only is not None else a.get("files"))
        log(f"tag sync report: {report}")
    finally:
        done()


# === Audiobookshelf ===

def abs_drama(a, log):
    abs_ = ABS()
    it = abs_.item(a["item"]); m = it["media"]["metadata"]; tags = it["media"].get("tags") or []
    title = DRAMA_RE.sub("", m["title"] or "").strip() + " " + config.DRAMA
    sub = DRAMA_RE.sub("", m.get("subtitle") or "").strip(" :-")
    abs_.patch_media(a["item"], {"tags": tags if config.TAG in tags else tags + [config.TAG], "metadata": {"title": title, "subtitle": sub}})
    known = {n.lower(): (n, i) for n, i in abs_.series_by_name().items()}
    new = []
    for s in m.get("series") or []:
        name = s["name"] if "dramati" in s["name"].lower() else DRAMA_RE.sub("", s["name"]).strip() + " " + config.DRAMA
        n, sid = known.get(name.lower(), (name, None))   # an existing series (any case) is reused, never renamed
        new.append({"id": sid, "name": n, "sequence": s.get("sequence") or ""})
    if [s["name"] for s in new] != [s["name"] for s in m.get("series") or []]:
        # series that don't exist yet are created one by one, reading back each new id
        for k, s in enumerate(new):
            if s["id"] is None:
                s["id"] = "new-1"
                abs_.patch_media(a["item"], {"metadata": {"series": new[:k + 1]}})
                s["id"] = next(g["id"] for g in abs_.item(a["item"])["media"]["metadata"]["series"] if g["name"] == s["name"])
        abs_.set_list(a["item"], "series", new)
    log(f"dramatized: {title}")


def abs_delete(a, log):
    ABS().delete_item(a["item"]); log(f"removed ABS entry {a['item']} (files untouched)")


def abs_delete_many(a, log):
    for i in a["items"]:
        abs_delete({"item": i}, log)


def abs_delete_files(a, log):
    """Duplicate copies: the files go to a trash folder inside /data, then the ABS entries are removed."""
    abs_ = ABS()
    trash = os.path.join(config.DATA, "trash", stamp())
    for i in a["items"]:
        it = abs_.item(i)
        src = abs_to_local(it["path"])
        if src and os.path.exists(src):
            dst = os.path.join(trash, it["relPath"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            log(f"moved {it['relPath']} to {dst}")
        abs_.delete_item(i)
    log("deleted copies are kept in " + trash)


def abs_patch(a, log):
    ABS().patch_media(a["item"], {"metadata": a["metadata"]}); log(f"updated {a['item']}: {a['metadata']}")


def abs_merge_author(a, log):
    abs_ = ABS()
    authors = abs_.authors_by_name()
    keep = authors[a["into"]]
    for name in a["names"]:
        if name == a["into"] or name not in authors:
            continue
        old = authors[name]
        for x in abs_.call("GET", f"/api/authors/{old['id']}?include=items")["libraryItems"]:
            m = abs_.item(x["id"])["media"]["metadata"]
            new = []
            for au in m["authors"]:
                y = {"id": keep["id"], "name": keep["name"]} if au["id"] == old["id"] else {"id": au["id"], "name": au["name"]}
                if y not in new:
                    new.append(y)
            abs_.set_list(x["id"], "authors", new)
            log(f"{m['title']}: {name} -> {a['into']}")
        try:
            if not abs_.call("GET", f"/api/authors/{old['id']}?include=items")["libraryItems"]:
                abs_.call("DELETE", f"/api/authors/{old['id']}")
        except Exception:
            pass   # ABS may already have removed the empty author


def abs_rename_author(a, log):
    abs_ = ABS()
    authors = abs_.authors_by_name()
    if a["new"] in authors:
        return abs_merge_author({"into": a["new"], "names": [a["name"], a["new"]]}, log)
    abs_.call("PATCH", f"/api/authors/{authors[a['name']]['id']}", {"name": a["new"]})
    log(f"author {a['name']} -> {a['new']}")


def abs_merge_series(a, log):
    """Books move to the kept series by id; a series is never renamed onto an existing name (that crashes ABS)."""
    abs_ = ABS()
    series = abs_.series_by_name()
    keep = {"id": series[a["into"]], "name": a["into"]}
    drop = {series[n] for n in a["names"] if n != a["into"] and n in series}
    db, done = abs_snapshot()
    try:
        con = abs_db(db)
        items = [r["id"] for r in con.execute("select li.id from libraryItems li join bookSeries bs on bs.bookId = li.mediaId where bs.seriesId in (%s)" % ",".join("?" * len(drop)), list(drop))]
    finally:
        done()
    for i in items:
        m = abs_.item(i)["media"]["metadata"]
        new, seen = [], set()
        for s in m["series"]:
            x = {**keep, "sequence": s.get("sequence") or ""} if s["id"] in drop else _ref(s)
            if x["id"] not in seen:
                seen.add(x["id"]); new.append(x)
        abs_.set_list(i, "series", new)
        log(f"{m['title']}: series -> {a['into']}")


# === Chaptarr ===

def ch_remove_files(a, log):
    stmts = [f'delete from "BookFiles" where "Id" in ({",".join(map(str, a["files"]))});']
    if a.get("unmonitor") and a.get("book"):
        stmts.append(f'update "Books" set "AudiobookMonitored" = false where "Id" = {int(a["book"])};')
    Pg().run(stmts); log(f"removed {len(a['files'])} Chaptarr file record(s)" + (", book unmonitored" if a.get("unmonitor") else ""))


def ch_author_path(a, log):
    Pg().run([f'update "Authors" set "Path" = {q(a["path"])}, "AudiobookPath" = {q(a["path"])} where "Id" = {int(a["author"])};'])
    log(f"Chaptarr author {a['author']} folder -> {a['path']}")


def ch_relink_book(a, log):
    pg = Pg()
    eds = pg.rows(f'select "Id" id, "Format" format, "Monitored" monitored from "Editions" where "BookId" = {int(a["book"])} order by ("Format" = \'audiobook\') desc, "Monitored" desc, "Id"')
    if not eds:
        raise RuntimeError("book has no editions")
    if eds[0]["format"] != "audiobook":
        raise RuntimeError("target book has no audiobook edition")
    stmts = [f'update "BookFiles" set "EditionId" = {int(eds[0]["id"])} where "Id" in ({",".join(map(str, a["files"]))});']
    src = a.get("from_book")
    if src:
        # the wrong book's monitoring moves to the right one; the wrong one is unmonitored once it holds no audiobook files
        stmts.append(f'update "Books" set "AudiobookMonitored" = true where "Id" = {int(a["book"])} and '
                     f'(select "AudiobookMonitored" from "Books" where "Id" = {int(src)});')
        if a.get("same_book", True):
            # the wrong entry was the same book (e.g. under another author): unmonitor it so it isn't downloaded again.
            # A different book stays as it was: it is simply missing now.
            stmts.append(f'update "Books" set "AudiobookMonitored" = false where "Id" = {int(src)} and not exists (select 1 from "BookFiles" bf '
                         f'join "Editions" e on e."Id" = bf."EditionId" where e."BookId" = {int(src)} and bf."Path" like {q(config.CHAPTARR_LIB_ROOT + "/%")});')
    pg.run(stmts)
    log(f"relinked {len(a['files'])} file(s) to Chaptarr book {a['book']} (edition {eds[0]['id']})" + (f", monitoring moved from book {src}" if src else ""))


def ch_unlink_files(a, log):
    Pg().run([f'update "BookFiles" set "EditionId" = 0 where "Id" in ({",".join(str(int(i)) for i in a["files"])});'])
    log(f"unlinked {len(a['files'])} Chaptarr file record(s)")


def chaptarr_update_paths(moves, log):
    """moves: [(old local path, new local path)]. One transaction. A new path already held by a stale record
    (a deleted duplicate) is resolved first: the real file takes over the link if the stale one was the
    monitored/original book, then the stale record goes."""
    pg = Pg()
    rows = pg.rows('select bf."Id" id, bf."Path" path, bf."EditionId" edition, e."BookId" book, b."AudiobookMonitored" mon '
                   'from "BookFiles" bf left join "Editions" e on e."Id" = bf."EditionId" left join "Books" b on b."Id" = e."BookId"')
    by_path = {r["path"]: r for r in rows}
    upd = [(local_to_chaptarr(o), local_to_chaptarr(n)) for o, n in moves if o != n]
    olds = {o for o, n in upd}
    stmts, emptied = [], set()
    for o, n in upd:
        held = by_path.get(n)
        real = by_path.get(o)
        if held and n not in olds and real:
            if held["mon"] == "t" and (real["mon"] != "t" or int(held["book"] or 0) < int(real["book"] or 0)):
                stmts.append(f'update "BookFiles" set "EditionId" = {held["edition"]} where "Id" = {real["id"]};')
                emptied.add(real["book"])
            stmts.append(f'delete from "BookFiles" where "Id" = {held["id"]};')
            log(f"stale Chaptarr record {held['id']} at {n} removed")
    stmts += [f'update "BookFiles" set "Path" = {q(n)} where "Path" = {q(o)};' for o, n in upd]
    for b in filter(None, emptied):
        stmts.append(f'update "Books" set "AudiobookMonitored" = false where "Id" = {b} and not exists '
                     f'(select 1 from "BookFiles" bf join "Editions" e on e."Id" = bf."EditionId" where e."BookId" = {b});')
    if stmts:
        pg.run(stmts)
    log(f"Chaptarr: {len(upd)} file path(s) updated")


def abs_reconcile(snapshot, moves, log):
    """After a scan: drop entries left pointing at moved-away folders and double entries (no listening progress
    only), and put back fields ABS kept from the previous occupant of a folder (e.g. ISBN after a swap)."""
    abs_ = ABS()
    db, done = abs_snapshot()
    try:
        con = abs_db(db)
        items = [dict(r) for r in con.execute("select li.id, li.relPath, li.isMissing, li.createdAt, b.id bookId, b.audioFiles, b.isbn, b.asin, b.title "
                                              "from libraryItems li join books b on b.id = li.mediaId where li.libraryId = ?", (config.ABS_LIBRARY_ID,))]
        progress = {r[0] for r in con.execute("select distinct mediaItemId from mediaProgresses")}
    finally:
        done()
    new_of = {n: o for o, n in moves}
    by_rel = {}
    for it in items:
        by_rel.setdefault(it["relPath"], []).append(it)
    for rel, its in by_rel.items():
        its.sort(key=lambda i: i["createdAt"])
        for extra in its[1:]:
            if extra["bookId"] not in progress:
                abs_.delete_item(extra["id"]); log(f"ABS double entry removed: {rel}")
    # only folders this run moved books out of; other missing books are a separate finding that waits for approval
    left = {os.path.dirname(o)[len(config.LIBRARY) + 1:] for o, n in moves if os.path.dirname(o) != os.path.dirname(n)}
    for it in items:
        if it["isMissing"] and it["bookId"] not in progress and it["relPath"] in left:
            local = abs_to_local(config.ABS_LIB_ROOT + "/" + it["relPath"])
            if local and not os.path.exists(local):
                abs_.delete_item(it["id"]); log(f"ABS entry for a moved-away folder removed: {it['relPath']}")
    # fields carried over from the folder's previous book
    for it in items:
        if it["isMissing"]:
            continue
        for af in json.loads(it["audioFiles"] or "[]"):
            local = abs_to_local(af["metadata"]["path"])
            old = snapshot.get(new_of.get(local, local))
            if old and old["asin"] and it["asin"] and old["asin"] == it["asin"]:
                fix = {k: old[k] or "" for k in ("isbn",) if (old[k] or "") != (it[k] or "")}
                if fix:
                    abs_.patch_media(it["id"], {"metadata": fix}); log(f"{it['title']}: restored {fix}")
            break


def standardize_all(a, log):
    """Plan, apply, Chaptarr paths, ABS scan + reconcile, tag sync. Undo log kept in /data/logs."""
    t = stamp()
    plan_csv = os.path.join(LOGS, f"plan-{t}.csv"); apply_log = os.path.join(LOGS, f"apply-{t}.jsonl")
    # a["only"]: folders (relative to the library) to limit this run to; default the whole library
    only = [o.strip("/") for o in a.get("only") or []]
    files = None
    if only:
        files = [config.ABS_LIB_ROOT + os.path.join(r, n)[len(config.LIBRARY):]
                 for o in only for r, _, ns in os.walk(os.path.join(config.LIBRARY, o)) for n in ns]
    # folder names come from the file tags, so bring the tags in line with ABS first
    tag_sync({}, log, only=files)
    rows = standardize.plan(config.LIBRARY, standardize.load_universes(os.path.join(os.path.dirname(__file__), "universes.json")))
    if only:
        rows = [r for r in rows if any(r["current"] == o or r["current"].startswith(o + "/") for o in only)]
        if not rows:
            log("nothing to do in " + ", ".join(only)); return
    with open(plan_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    todo = [r for r in rows if r["action"] in ("move", "rename")]
    log(f"plan: {len(todo)} to move/rename ({plan_csv})")
    if not todo:
        return
    # what ABS knows about each audio file before the move
    db, done = abs_snapshot()
    try:
        con = abs_db(db)
        snapshot = {}
        for r in con.execute("select b.audioFiles, b.isbn, b.asin from libraryItems li join books b on b.id = li.mediaId where li.libraryId = ?", (config.ABS_LIBRARY_ID,)):
            for af in json.loads(r["audioFiles"] or "[]"):
                snapshot[abs_to_local(af["metadata"]["path"])] = {"isbn": r["isbn"], "asin": r["asin"]}
    finally:
        done()
    pg = Pg() if config.CHAPTARR_ENABLED else None
    if pg:
        pg.backup(["BookFiles", "Authors"], os.path.join(LOGS, f"chaptarr-before-apply-{t}.dump"))
    standardize.apply(config.LIBRARY, plan_csv, apply_log)
    moves = []
    with open(apply_log, encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e["op"] == "path":
                moves.append((e["old"].replace("/library", config.LIBRARY, 1), e["new"].replace("/library", config.LIBRARY, 1)))
            elif e["op"] == "error":
                log(f"FAILED {e['current']}: {e['error']}")
    log(f"moved: {len(moves)} audio files (undo log {apply_log})")
    if pg:
        chaptarr_update_paths(moves, log)
    abs_ = ABS()
    abs_.scan(); time.sleep(5); abs_.wait_idle()
    abs_reconcile(snapshot, moves, log)
    tag_sync({}, log, only=[config.ABS_LIB_ROOT + n[len(config.LIBRARY):] for _, n in moves] if only else None)


def remove_empty_dirs(a, log):
    n = 0
    for root, dirs, files in os.walk(config.LIBRARY, topdown=False):
        if root != config.LIBRARY and not os.listdir(root):
            os.rmdir(root); n += 1
    log(f"removed {n} empty folders")


def delete_backup(a, log):
    p = os.path.join(config.ABS_METADATA_DIR, "cache", "items", os.path.basename(a["dir"]))
    shutil.rmtree(p); log(f"deleted {p}")


# === conversion ===

def _ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=codec_name,profile,sample_rate,channels:format=duration", "-of", "json", path],
                         capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"ffprobe {path}: {out.stderr.strip()[:300]}")
    j = json.loads(out.stdout)
    st = (j.get("streams") or [{}])[0]
    return {"codec": st.get("codec_name"), "profile": st.get("profile"), "rate": st.get("sample_rate"),
            "channels": st.get("channels"), "duration": float(j.get("format", {}).get("duration") or 0)}


def _ffprobe_chapters(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", path], capture_output=True, text=True)
    return [{"start": float(c["start_time"]), "end": float(c["end_time"]), "title": (c.get("tags") or {}).get("title") or f"Chapter {i + 1}"}
            for i, c in enumerate(json.loads(out.stdout or "{}").get("chapters", []))]


def _ffmeta(text):
    return re.sub(r"([=;#\\\n])", r"\\\1", str(text or ""))


def _join_lossless(it, files, log):
    """Join the book's audio into one m4b with ffmpeg stream copy (no re-encode), the way ABS's own encoder swaps
    the files: originals to ABS's item cache, the new audio written into the FIRST track's file (same inode, so ABS
    keeps the item and progress), which is then renamed to <folder>.m4b."""
    m = it["media"]
    paths = [abs_to_local(f["metadata"]["path"]) for f in files]
    probes = [_ffprobe(p) for p in paths]
    keys = {(p["codec"], p["profile"], p["rate"], p["channels"]) for p in probes}
    if len(keys) != 1:
        raise RuntimeError(f"the files differ in codec/sample rate/channels ({sorted(map(str, keys))}); a lossless join needs them identical")
    folder = os.path.dirname(paths[0])
    cache = os.path.join(config.ABS_METADATA_DIR, "cache", "items", it["id"])
    os.makedirs(cache, exist_ok=True)
    target = os.path.join(folder, os.path.basename(folder) + ".m4b")
    if os.path.exists(target) and target not in paths:
        raise RuntimeError(f"{target} already exists")
    tmp = os.path.join(cache, "shelfwright-join.m4b")
    lst = os.path.join(cache, "shelfwright-files.txt")
    meta = os.path.join(cache, "shelfwright-ffmetadata.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    md = m["metadata"]
    chapters = m.get("chapters") or []
    if not chapters:   # one chapter per file
        t = 0.0
        for i, p in enumerate(probes):
            chapters.append({"start": t, "end": t + p["duration"], "title": f"Chapter {i + 1}"}); t += p["duration"]
    with open(meta, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={_ffmeta(md.get('title'))}\nalbum={_ffmeta(md.get('title'))}\n")
        f.write(f"artist={_ffmeta(md.get('authorName'))}\nalbum_artist={_ffmeta(md.get('authorName'))}\n")
        for c in chapters:
            f.write(f"\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(round(c['start'] * 1000))}\nEND={int(round(c['end'] * 1000))}\ntitle={_ffmeta(c['title'])}\n")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-i", meta]
    cover = abs_to_local(m.get("coverPath")) if m.get("coverPath") else None
    if cover and os.path.isfile(cover) and cover.lower().endswith((".jpg", ".jpeg", ".png")):
        cmd += ["-i", cover, "-map", "0:a", "-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-map_metadata", "1", "-map_chapters", "1", "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", tmp]
    log(f"joining {len(paths)} file(s) losslessly ({probes[0]['codec']}, {probes[0]['rate']} Hz, {probes[0]['channels']} ch)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("ffmpeg: " + r.stderr.strip()[-800:])
    # check before touching the originals: same codec, nothing lost in length
    out = _ffprobe(tmp)
    total = sum(p["duration"] for p in probes)
    if out["codec"] != probes[0]["codec"] or abs(out["duration"] - total) > max(2.0, total * 0.001):
        raise RuntimeError(f"joined file looks wrong: {out['codec']} {out['duration']:.1f}s vs {probes[0]['codec']} {total:.1f}s; originals untouched")
    log(f"joined: {out['duration'] / 3600:.2f} h, {os.path.getsize(tmp) / 2**20:.0f} MB")
    # originals to ABS's item cache (the first one copied, as ABS does), then the swap
    for i, p in enumerate(paths):
        dest = os.path.join(cache, os.path.basename(p))
        if i == 0:
            shutil.copy2(p, dest)
        else:
            shutil.move(p, dest)
    with open(tmp, "rb") as src, open(paths[0], "r+b") as dst:   # in place: same inode, owner and permissions
        dst.truncate(0)
        shutil.copyfileobj(src, dst, 16 * 2**20)
        dst.flush(); os.fsync(dst.fileno())
    os.rename(paths[0], target)
    for f in (tmp, lst, meta):
        os.remove(f)
    log(f"originals kept in {cache}")
    return paths, target


def abs_readd(a, log):
    """Remove an ABS item from the database only (never its files) and let a library scan add the folder again as a
    new item: clears stale entries a rescan can't. Metadata comes back from the folder's metadata.json; collection
    membership is put back. Refused if the book has listening progress."""
    abs_ = ABS()
    it = abs_.item(a["item"])
    rel = it["relPath"]
    db, done = abs_snapshot()
    try:
        con = abs_db(db)
        if con.execute("select 1 from mediaProgresses where mediaItemId = ?", (it["media"]["id"],)).fetchone():
            raise RuntimeError("the book has listening progress; not re-adding it")
    finally:
        done()
    folder = abs_to_local(it["path"])
    if not folder or not os.path.isfile(os.path.join(folder, "metadata.json")):
        raise RuntimeError(f"no metadata.json in {folder}; re-adding would lose the ABS metadata")
    colls = [c["id"] for c in abs_.call("GET", f"/api/libraries/{config.ABS_LIBRARY_ID}/collections?limit=1000").get("results", [])
             if any(b.get("id") == a["item"] for b in c.get("books", []))]
    log(f"{rel}: in {len(colls)} collection(s); removing the ABS entry (files untouched) and rescanning")
    abs_.delete_item(a["item"])
    time.sleep(3)
    abs_.scan(); time.sleep(5); abs_.wait_idle()
    db, done = abs_snapshot()
    try:
        r = abs_db(db).execute("select id from libraryItems where libraryId = ? and relPath = ?", (config.ABS_LIBRARY_ID, rel)).fetchone()
    finally:
        done()
    if not r:
        raise RuntimeError(f"ABS did not add {rel} again; run a library scan in ABS")
    new_id = r["id"]
    new = abs_.item(new_id)
    bad = [f["metadata"]["path"] for f in new["media"]["audioFiles"] if not f["metadata"]["path"].startswith(new["path"] + "/")]
    if bad:
        raise RuntimeError(f"re-added item {new_id} still lists {bad}")
    for c in colls:
        abs_.call("POST", f"/api/collections/{c}/book", {"id": new_id})
    # metadata.json still holds the old (merged) chapter list: take the chapters from the audio itself
    m = new["media"]
    if any(c["end"] > m["duration"] + 1 for c in m.get("chapters") or []):
        if len(m["audioFiles"]) == 1:
            emb = _ffprobe_chapters(abs_to_local(m["audioFiles"][0]["metadata"]["path"]))
        else:
            emb, t = [], 0.0
            for i, f in enumerate(m["audioFiles"]):
                emb.append({"start": t, "end": t + f["duration"], "title": f"Chapter {i + 1}"}); t += f["duration"]
        ch = emb or [{"start": 0, "end": m["duration"], "title": m["metadata"]["title"]}]
        abs_.call("POST", f"/api/items/{new_id}/chapters", {"chapters": [{"id": i, **c} for i, c in enumerate(ch)]})
        log(f"chapters from metadata.json ran past the end; set {len(ch)} chapter(s) from the audio")
        new = abs_.item(new_id)
    log(f"re-added as {new_id}: {len(new['media']['audioFiles'])} audio file(s), {round(new['media']['duration'] / 60)} min, "
        f"{len(new['media'].get('chapters') or [])} chapters; collections restored: {len(colls)}")


def convert(a, log):
    """One m4b per book. mode=copy: a lossless join (AAC/ALAC/E-AC-3), done here with ffmpeg stream copy.
    mode=reencode: ABS's own encoder at the source bitrate or higher (MP3, Ogg, ...). Either way the ABS item,
    its id and listening progress are kept, originals go to ABS's item cache, the full tags are written back from
    ABS, and Chaptarr's records are moved to the new file."""
    import checks
    abs_ = ABS()
    it = abs_.item(a["item"])
    files = sorted((f for f in it["media"]["audioFiles"] if not f.get("exclude")), key=lambda f: f.get("index") or 0)
    stray = [f["metadata"]["path"] for f in it["media"]["audioFiles"] if not f["metadata"]["path"].startswith(it["path"] + "/")]
    if stray:
        raise RuntimeError(f"ABS lists audio from another folder in this book ({stray}); fix that first (ABS: wrong audio file)")
    title = it["media"]["metadata"]["title"]
    codecs = {(f.get("codec") or "?").lower() for f in files}
    mode = a.get("mode") or ("copy" if len(codecs) == 1 and codecs <= checks.COPY_CODECS else "reencode")
    if mode == "copy":
        log(f"{title}: {len(files)} file(s), lossless join")
        old_paths, new_path = _join_lossless(it, files, log)
    else:
        old_paths = [abs_to_local(f["metadata"]["path"]) for f in files]
        br = checks.reencode_bitrate(files)
        channels = max((f.get("channels") or 2) for f in files)
        log(f"{title}: {len(files)} file(s), {', '.join(sorted(codecs))} -> AAC {br}k, {channels} ch with ABS's encoder")
        abs_.call("POST", f"/api/tools/item/{a['item']}/encode-m4b?bitrate={br}k&codec=aac&channels={channels}")
        end = time.time() + 6 * 3600
        while time.time() < end:
            time.sleep(15)
            mine = [t for t in abs_.tasks() if (t.get("data") or {}).get("libraryItemId") == a["item"] and t.get("action") == "encode-m4b"]
            if not mine or mine[0].get("isFinished"):
                if mine and mine[0].get("isFailed"):
                    raise RuntimeError("ABS encode failed: " + str(mine[0].get("error")))
                break
        new_path = None
    abs_.call("POST", f"/api/items/{a['item']}/scan")
    time.sleep(5)
    it = abs_.item(a["item"])
    new = [abs_to_local(f["metadata"]["path"]) for f in it["media"]["audioFiles"]]
    if len(new) != 1 or not new[0].lower().endswith(".m4b") or (new_path and new[0] != new_path):
        raise RuntimeError(f"after converting ABS lists {new}")
    log(f"new file: {new[0]}")
    tag_sync({}, log, only=[config.ABS_LIB_ROOT + new[0][len(config.LIBRARY):]])
    if config.CHAPTARR_ENABLED:
        pg = Pg()
        olds = [local_to_chaptarr(p) for p in old_paths]
        rows = pg.rows('select "Id" id, "Path" path from "BookFiles" where "Path" in (%s)' % ",".join(q(p) for p in olds))
        if rows:
            keep, rest = rows[0], rows[1:]
            size = os.path.getsize(new[0])
            stmts = [f'update "BookFiles" set "Path" = {q(local_to_chaptarr(new[0]))}, "Size" = {size}, '
                     f'"Quality" = \'{{"quality": 12, "revision": {{"version": 1, "real": 0, "isRepack": false}}}}\' where "Id" = {keep["id"]};']
            if rest:
                stmts.append(f'delete from "BookFiles" where "Id" in ({",".join(r["id"] for r in rest)});')
            pg.run(stmts); log(f"Chaptarr: {len(rows)} file record(s) -> the m4b")
