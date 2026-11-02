"""Thin clients for Audiobookshelf, Chaptarr and Chaptarr's Postgres database (stdlib + psql)."""
import csv, io, json, os, shutil, sqlite3, subprocess, tempfile, time, urllib.error, urllib.parse, urllib.request

import config


class HttpError(Exception):
    pass


def _call(base, headers, method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data, headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise HttpError(f"{method} {path}: HTTP {e.code} {e.read()[:200]!r}") from e
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"text": raw.decode(errors="replace")}


# === Audiobookshelf ===

class ABS:
    def __init__(self):
        self.h = {"Authorization": "Bearer " + config.ABS_TOKEN}

    def call(self, method, path, body=None, timeout=120):
        return _call(config.ABS_URL, self.h, method, path, body, timeout)

    def item(self, item_id):
        return self.call("GET", f"/api/items/{item_id}?expanded=1")

    def patch_media(self, item_id, body):
        return self.call("PATCH", f"/api/items/{item_id}/media", body)

    def set_list(self, item_id, field, values):
        """Authors/series one at a time: ABS orders them by link creation time, so this fixes the order."""
        for k in range(1, len(values) + 1):
            self.patch_media(item_id, {"metadata": {field: values[:k]}})

    def series_by_name(self):
        r = self.call("GET", f"/api/libraries/{config.ABS_LIBRARY_ID}/series?limit=10000")
        return {s["name"]: s["id"] for s in r.get("results", [])}

    def authors_by_name(self):
        r = self.call("GET", f"/api/libraries/{config.ABS_LIBRARY_ID}/authors?limit=10000")
        return {a["name"]: a for a in r.get("authors", [])}

    def delete_item(self, item_id):
        # never ?hard=1: database entry only, files are left alone
        return self.call("DELETE", f"/api/items/{item_id}")

    def scan(self):
        self.call("POST", f"/api/libraries/{config.ABS_LIBRARY_ID}/scan")

    def wait_idle(self, timeout=3600):
        end = time.time() + timeout
        while time.time() < end:
            tasks = self.call("GET", "/api/tasks").get("tasks", [])
            if not [t for t in tasks if not t.get("isFinished")]:
                return True
            time.sleep(10)
        return False

    def tasks(self):
        return self.call("GET", "/api/tasks").get("tasks", [])

    def issues(self):
        r = self.call("GET", f"/api/libraries/{config.ABS_LIBRARY_ID}/items?filter=issues&limit=1000&minified=1")
        return r.get("results", [])


def abs_snapshot():
    """Copy of ABS's sqlite database (with -wal/-shm) into a temp dir; returns (path, cleanup)."""
    tmp = tempfile.mkdtemp(prefix="absdb-")
    for name in os.listdir(config.ABS_CONFIG_DIR):
        if name.startswith("absdatabase.sqlite"):
            shutil.copy2(os.path.join(config.ABS_CONFIG_DIR, name), tmp)
    return os.path.join(tmp, "absdatabase.sqlite"), lambda: shutil.rmtree(tmp, ignore_errors=True)


def abs_db(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# === Chaptarr ===

class Chaptarr:
    def __init__(self):
        self.h = {"X-Api-Key": config.CHAPTARR_KEY}

    def call(self, method, path, body=None, timeout=120):
        return _call(config.CHAPTARR_URL, self.h, method, "/api/v1" + path, body, timeout)

    def up(self):
        try:
            self.call("GET", "/system/status", timeout=10)
            return True
        except Exception:
            return False

    def command(self, name, **kw):
        return self.call("POST", "/command", {"name": name, **kw})


class Pg:
    """Chaptarr's Postgres through the psql client: reads as CSV, writes as one transaction."""

    def _env(self):
        return {**os.environ, "PGPASSWORD": config.PG_PASSWORD}

    def _base(self):
        return ["psql", "-h", config.PG_HOST, "-p", config.PG_PORT, "-U", config.PG_USER, "-d", config.PG_DB, "-v", "ON_ERROR_STOP=1", "-q"]

    def rows(self, sql):
        r = subprocess.run(self._base() + ["-c", f"copy ({sql}) to stdout with csv header"], env=self._env(), capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("psql: " + r.stderr.strip()[:800])
        return list(csv.DictReader(io.StringIO(r.stdout)))

    def run(self, statements):
        """statements: list of SQL strings, applied atomically."""
        script = "begin;\n" + "\n".join(statements) + "\ncommit;\n"
        r = subprocess.run(self._base(), input=script, env=self._env(), capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr.strip()[:800])
        return r.stdout

    def backup(self, tables, dest):
        args = ["pg_dump", "-h", config.PG_HOST, "-p", config.PG_PORT, "-U", config.PG_USER, "-d", config.PG_DB, "-Fc", "-f", dest]
        for t in tables:
            args += ["-t", f'"{t}"']
        r = subprocess.run(args, env=self._env(), capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("pg_dump: " + r.stderr.strip()[:800])


def q(s):
    """SQL string literal."""
    return "'" + str(s).replace("'", "''") + "'"


# path helpers between the three views of the library
def abs_to_local(p):
    return config.LIBRARY + p[len(config.ABS_LIB_ROOT):] if p.startswith(config.ABS_LIB_ROOT + "/") else None


def chaptarr_to_local(p):
    return config.LIBRARY + p[len(config.CHAPTARR_LIB_ROOT):] if p.startswith(config.CHAPTARR_LIB_ROOT + "/") else None


def local_to_chaptarr(p):
    return config.CHAPTARR_LIB_ROOT + p[len(config.LIBRARY):]
