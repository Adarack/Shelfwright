"""Scans (checks -> findings), auto-applying safe fixes, and running approved fixes as jobs."""
import csv, os, queue, threading, time, traceback

import actions, checks, config, standardize, store, tagsync
from clients import Pg, abs_db, abs_snapshot

ACTIONS = {
    "tag_sync": actions.tag_sync, "abs_drama": actions.abs_drama, "abs_delete": actions.abs_delete,
    "abs_delete_many": actions.abs_delete_many, "abs_delete_files": actions.abs_delete_files, "abs_patch": actions.abs_patch,
    "abs_merge_author": actions.abs_merge_author, "abs_rename_author": actions.abs_rename_author, "abs_merge_series": actions.abs_merge_series,
    "ch_remove_files": actions.ch_remove_files, "ch_author_path": actions.ch_author_path, "ch_relink_book": actions.ch_relink_book, "ch_unlink_files": actions.ch_unlink_files,
    "standardize": actions.standardize_all, "remove_empty_dirs": actions.remove_empty_dirs, "delete_backup": actions.delete_backup,
    "convert": actions.convert,
}
# Jobs that move or rewrite files run one at a time; conversions have their own lane
_work = queue.Queue()
_convert = queue.Queue()
_scan_lock = threading.Lock()
# anything that moves, rewrites or deletes library files holds this, so a conversion never runs during a move
_files_lock = threading.Lock()
FILE_ACTIONS = {"tag_sync", "abs_delete_files", "standardize", "remove_empty_dirs", "convert", "delete_backup"}


def _logger(jid):
    return lambda line: store.job_log(jid, line)


def run_action(action, log):
    fn = ACTIONS.get(action["type"])
    if not fn:
        raise RuntimeError(f"unknown action {action['type']}")
    fn(action, log)


def scan():
    """Run every check, refresh the findings, then apply safe fixes (if enabled)."""
    if not _scan_lock.acquire(blocking=False):
        return None
    jid = store.new_job("scan", "Library scan")
    log = _logger(jid)
    try:
        db, done = abs_snapshot()
        try:
            pg = Pg() if config.CHAPTARR_ENABLED else None
            try:
                ctx = checks.Context(abs_db(db), pg)
            except Exception as e:
                log(f"Chaptarr database not reachable ({e}); Chaptarr checks skipped")
                ctx = checks.Context(abs_db(db), None)
            tag_report = os.path.join(actions.LOGS, f"tag-plan-{actions.stamp()}.csv")
            with _files_lock:
                tagsync.sync("plan", db, config.LIBRARY, tag_report)
            tag_rows = list(csv.DictReader(open(tag_report, encoding="utf-8")))
        finally:
            done()
        plan_rows = standardize.plan(config.LIBRARY, standardize.load_universes(os.path.join(os.path.dirname(__file__), "universes.json")))
        groups = {
            "tag_sync": checks.check_tags(ctx, tag_rows), "drama": checks.check_drama(ctx), "abs_missing": checks.check_missing(ctx),
            "abs_double": checks.check_double_entries(ctx), "duplicate": checks.check_duplicates(ctx),
            "author": checks.check_author_variants(ctx), "series_variant": checks.check_series_variants(ctx), "title": checks.check_titles(ctx),
            "naming": checks.check_naming(ctx, plan_rows, {os.path.dirname(r["file"])[len(config.ABS_LIB_ROOT) + 1:] for r in tag_rows if r["result"] == "would update"}), "empty_dirs": checks.check_empty_dirs(ctx), "convert": checks.check_conversion(ctx),
            "backup": checks.check_backups(ctx), "chaptarr": checks.check_chaptarr(ctx), "ch_links": checks.check_chaptarr_links(ctx), "ch_titles": checks.check_chaptarr_titles(ctx),
        }
        seen = {}
        for name, gen in groups.items():
            try:
                for f in gen:
                    store.upsert_finding(f["check"], f["key"], f["title"], f["summary"], f["detail"], f["action"], f["safe"])
                    seen.setdefault(f["check"], set()).add(f["key"])
            except Exception as e:
                log(f"check {name} failed: {e}\n{traceback.format_exc()}")
        for check in {f["check"] for f in store.findings()}:
            store.close_missing(check, seen.get(check, set()))
        open_ = store.findings(status="open")
        log(f"scan done: {len(open_)} open findings")
        if config.AUTO_APPLY_SAFE:
            for f in [f for f in open_ if f["safe"]]:
                approve(f["id"])
        store.kv_set("last_scan", time.time())
        store.end_job(jid, "done")
    except Exception as e:
        log(f"scan failed: {e}\n{traceback.format_exc()}")
        store.end_job(jid, "failed")
    finally:
        _scan_lock.release()
    return jid


def approve(fid, option=None):
    """Queue a finding's fix. For a choose-action, option is the index of the chosen option."""
    f = store.get_finding(fid)
    if not f or f["status"] not in ("open", "failed"):
        return False
    action = f["action"]
    if action and action["type"] == "choose":
        if option is None or not (0 <= option < len(action["options"])):
            return False
        action = action["options"][option]["action"]
    if not action:
        return False
    store.set_status(fid, "queued")
    (_convert if action["type"] == "convert" else _work).put((fid, action))
    return True


def reject(fid):
    store.set_status(fid, "rejected")


def _worker(q):
    while True:
        fid, action = q.get()
        f = store.get_finding(fid)
        jid = store.new_job(action["type"], f"{f['check']}: {f['title']}")
        store.set_status(fid, "running")
        try:
            if action["type"] in FILE_ACTIONS:
                with _files_lock:
                    run_action(action, _logger(jid))
            else:
                run_action(action, _logger(jid))
            store.set_status(fid, "applied", f"job {jid}")
            store.end_job(jid, "done")
        except Exception as e:
            store.job_log(jid, f"FAILED: {e}\n{traceback.format_exc()}")
            store.set_status(fid, "failed", str(e)[:500])
            store.end_job(jid, "failed")


def _scheduler():
    while True:
        last = store.kv_get("last_scan", 0)
        if time.time() - last >= config.SCAN_INTERVAL_HOURS * 3600:
            scan()
        time.sleep(300)


def start():
    # fixes interrupted by a restart go back to waiting for approval
    for f in store.findings():
        if f["status"] in ("queued", "running"):
            store.set_status(f["id"], "open", "interrupted by a restart")
    threading.Thread(target=_worker, args=(_work,), daemon=True).start()
    for _ in range(max(1, config.CONVERT_CONCURRENCY)):
        threading.Thread(target=_worker, args=(_convert,), daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()
