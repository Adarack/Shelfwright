"""Shelfwright web UI + JSON API (stdlib http.server)."""
import json, os, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config, engine, store

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKS = {
    "tag_sync": ("File tags", "Tags that differ from ABS, written from ABS"),
    "drama": ("Dramatized formatting", "Title, series and tag for dramatized books"),
    "naming": ("Folder names", "Folders that don't follow the naming standard"),
    "collision": ("Name collisions", "Two books that would land in one folder"),
    "duplicate": ("Duplicates", "The same book more than once"),
    "author_variant": ("Author spellings", "One author spelled more ways"),
    "author_initials": ("Author initials", "Initials without dots"),
    "series_variant": ("Series spellings", "One series spelled more ways"),
    "title": ("Titles", "(Unabridged) or a series position in the title"),
    "abs_missing": ("Missing in ABS", "ABS lists books whose files are gone"),
    "abs_double": ("Double ABS entries", "Two ABS entries for one folder"),
    "abs_foreign": ("Wrong audio file", "ABS lists a file from another folder in this book"),
    "convert": ("Convert to M4B", "Lossless join for AAC books; MP3/Ogg only when re-encoding is on"),
    "backup": ("Conversion backups", "Original tracks ABS kept after converting"),
    "empty_dirs": ("Empty folders", "Leftover empty folders"),
    "ch_stale": ("Chaptarr: deleted files", "Chaptarr still lists files that are gone"),
    "ch_unlinked": ("Chaptarr: unlinked", "Files not linked to a book"),
    "ch_author_path": ("Chaptarr: author folders", "Author folder isn't where the files are"),
    "ch_wrong_author": ("Chaptarr: wrong author", "File linked to another author's book"),
    "ch_wrong_title": ("Chaptarr: wrong book", "File linked to another book by the same author"),
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        path, _, query = self.path.partition("?")
        args = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path in ("/", "/index.html"):
            return self._send(200, open(os.path.join(HERE, "ui.html"), "rb").read(), "text/html; charset=utf-8")
        if path == "/api/summary":
            fs = store.findings()
            counts = {}
            for f in fs:
                c = counts.setdefault(f["check"], {"open": 0, "applied": 0, "failed": 0, "rejected": 0, "queued": 0, "running": 0, "resolved": 0})
                c[f["status"]] = c.get(f["status"], 0) + 1
            return self._send(200, {"checks": CHECKS, "counts": counts, "last_scan": store.kv_get("last_scan"),
                                    "auto_apply_safe": config.AUTO_APPLY_SAFE, "version": config.VERSION, "problems": config.problems(), "jobs": store.jobs(15)})
        if path == "/api/findings":
            return self._send(200, store.findings(args.get("status") or None, args.get("check") or None))
        m = re.fullmatch(r"/api/jobs/(\d+)", path)
        if m:
            return self._send(200, store.job(int(m.group(1))) or {})
        if path == "/api/jobs":
            return self._send(200, store.jobs(100))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.partition("?")[0]
        body = self._body()
        if path == "/api/scan":
            threading.Thread(target=engine.scan, daemon=True).start()
            return self._send(200, {"ok": True})
        m = re.fullmatch(r"/api/findings/(\d+)/(approve|reject)", path)
        if m:
            fid = int(m.group(1))
            ok = engine.approve(fid, body.get("option")) if m.group(2) == "approve" else (engine.reject(fid) or True)
            return self._send(200 if ok else 400, {"ok": bool(ok)})
        if path == "/api/findings/approve-all":
            # bulk: findings of one check that have a single fix (or option 0 when asked for)
            n = 0
            for f in store.findings("open", body.get("check")):
                a = f["action"] or {}
                if a.get("type") == "choose" and (len(a["options"]) == 1 or body.get("first_option")):
                    n += engine.approve(f["id"], 0)
                elif a.get("type") and a.get("type") != "choose":
                    n += engine.approve(f["id"])
            return self._send(200, {"queued": n})
        self._send(404, {"error": "not found"})


def main():
    store.init()
    engine.start()
    print(f"Shelfwright {config.VERSION} on :{config.PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", config.PORT), H).serve_forever()


if __name__ == "__main__":
    main()
