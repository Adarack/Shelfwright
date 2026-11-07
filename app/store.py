"""Findings (issues + proposed fix) and jobs, kept in a small sqlite database in /data."""
import contextlib, json, os, sqlite3, threading, time

import config

_lock = threading.Lock()
_DB = os.path.join(config.DATA, "shelfwright.db")


@contextlib.contextmanager
def _con():
    """A connection for one block: committed (or rolled back) and closed at the end. sqlite3's own context manager
    only commits, which leaked a connection per call."""
    con = sqlite3.connect(_DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    os.makedirs(config.DATA, exist_ok=True)
    with _lock, _con() as c:
        c.executescript("""
        create table if not exists findings (
            id integer primary key, "check" text not null, key text not null, title text, summary text,
            detail text, action text, safe integer default 0, status text default 'open',
            result text, created real, updated real, unique("check", key));
        create table if not exists jobs (
            id integer primary key, kind text, title text, status text, started real, ended real, log text default '');
        create table if not exists kv (k text primary key, v text);
        """)
        cols = [r[1] for r in c.execute("pragma table_info(jobs)")]
        if "progress" not in cols:
            c.execute("alter table jobs add column progress real")


def upsert_finding(check, key, title, summary, detail, action, safe=False):
    """New issues are added as open; an issue that is still open gets its details refreshed;
    a rejected issue stays rejected while its proposal is unchanged."""
    now = time.time()
    a = json.dumps(action, ensure_ascii=False, sort_keys=True)
    with _lock, _con() as c:
        row = c.execute('select id, status, action from findings where "check"=? and key=?', (check, key)).fetchone()
        if row is None:
            c.execute('insert into findings ("check", key, title, summary, detail, action, safe, status, created, updated) values (?,?,?,?,?,?,?,?,?,?)',
                      (check, key, title, summary, json.dumps(detail, ensure_ascii=False), a, int(safe), "open", now, now))
        elif row["status"] == "rejected" and row["action"] == a:
            return
        else:
            # still open, failed earlier, or back again after being fixed / resolved / rejected with a different proposal
            c.execute("update findings set title=?, summary=?, detail=?, action=?, safe=?, status='open', updated=? where id=?",
                      (title, summary, json.dumps(detail, ensure_ascii=False), a, int(safe), now, row["id"]))


def close_missing(check, keys_seen):
    """Open findings of a check that the latest scan no longer reports are resolved."""
    with _lock, _con() as c:
        for r in c.execute('select id, key from findings where "check"=? and status in (\'open\', \'failed\')', (check,)).fetchall():
            if r["key"] not in keys_seen:
                c.execute("update findings set status='resolved', updated=? where id=?", (time.time(), r["id"]))


def findings(status=None, check=None):
    sql, args = "select * from findings where 1=1", []
    if status:
        sql += " and status=?"; args.append(status)
    if check:
        sql += ' and "check"=?'; args.append(check)
    with _con() as c:
        out = []
        for r in c.execute(sql + ' order by "check", title', args):
            d = dict(r); d["detail"] = json.loads(d["detail"] or "null"); d["action"] = json.loads(d["action"] or "null")
            out.append(d)
        return out


def get_finding(fid):
    r = [f for f in findings() if f["id"] == fid]
    return r[0] if r else None


def set_status(fid, status, result=None):
    with _lock, _con() as c:
        c.execute("update findings set status=?, result=coalesce(?, result), updated=? where id=?", (status, result, time.time(), fid))


def new_job(kind, title):
    with _lock, _con() as c:
        cur = c.execute("insert into jobs (kind, title, status, started) values (?,?,?,?)", (kind, title, "running", time.time()))
        return cur.lastrowid


def job_log(jid, line):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock, _con() as c:
        c.execute("update jobs set log = log || ? where id=?", (f"[{stamp}] {line}\n", jid))


def job_progress(jid, pct):
    with _lock, _con() as c:
        c.execute("update jobs set progress=? where id=?", (round(max(0.0, min(100.0, pct)), 1), jid))


def end_job(jid, status):
    with _lock, _con() as c:
        c.execute("update jobs set status=?, ended=? where id=?", (status, time.time(), jid))


def jobs(limit=50):
    with _con() as c:
        return [dict(r) for r in c.execute("select id, kind, title, status, started, ended, progress from jobs order by id desc limit ?", (limit,))]


def job(jid):
    with _con() as c:
        r = c.execute("select * from jobs where id=?", (jid,)).fetchone()
        return dict(r) if r else None


def kv_get(k, default=None):
    with _con() as c:
        r = c.execute("select v from kv where k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default


def kv_set(k, v):
    with _lock, _con() as c:
        c.execute("insert into kv (k, v) values (?, ?) on conflict(k) do update set v=excluded.v", (k, json.dumps(v)))
