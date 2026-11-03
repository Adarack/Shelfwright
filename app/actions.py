"""Carry out a finding's action. Every function takes (action, log) and raises on failure."""
import csv, json, os, re, shutil, time

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

def convert(a, log):
    """ABS's own m4b encoder (keeps the item, progress and its id; originals go to ABS's cache), then the full tags
    back, then Chaptarr's records moved to the new file."""
    abs_ = ABS()
    it = abs_.item(a["item"])
    files = [f for f in it["media"]["audioFiles"] if not f.get("exclude")]
    old_paths = [abs_to_local(f["metadata"]["path"]) for f in files]
    src_br = max((f.get("bitRate") or 0) for f in files) // 1000 or config.CONVERT_MAX_BITRATE
    br = min(config.CONVERT_MAX_BITRATE, max(64, round(src_br / 32) * 32))
    channels = max((f.get("channels") or 2) for f in files)
    log(f"{it['media']['metadata']['title']}: {len(files)} file(s) -> m4b at {br}k, {channels} ch")
    abs_.call("POST", f"/api/tools/item/{a['item']}/encode-m4b?bitrate={br}k&codec=aac&channels={channels}")
    end = time.time() + 6 * 3600
    while time.time() < end:
        time.sleep(15)
        mine = [t for t in abs_.tasks() if (t.get("data") or {}).get("libraryItemId") == a["item"] and t.get("action") == "encode-m4b"]
        if not mine or mine[0].get("isFinished"):
            if mine and mine[0].get("isFailed"):
                raise RuntimeError("ABS encode failed: " + str(mine[0].get("error")))
            break
    abs_.call("POST", f"/api/items/{a['item']}/scan")
    time.sleep(5)
    it = abs_.item(a["item"])
    new = [abs_to_local(f["metadata"]["path"]) for f in it["media"]["audioFiles"]]
    if len(new) != 1 or not new[0].lower().endswith(".m4b"):
        raise RuntimeError(f"after encoding ABS lists {new}")
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
