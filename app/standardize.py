#!/usr/bin/env python3
"""Rename audiobook folders and files in the library to the standard scheme.

    Series:     Author/Series[ [Dramatized Adaptation]]/Pos Title/Pos Title[ N].ext
    Standalone: Author/Title/Title[ N].ext

Everything comes from the audio file tags (Metadata Forge / beets write them):
  author   first album artist ("A, B" -> "A"), like beets' primary_author
  title    album tag, plus the subtitle unless it only repeats the series
  series   every series the tags list: SERIES / MVNM with SERIES-PART / MVIN,
           plus each entry of an ABS-style grouping "Series #N; Other #M".
           A book in a universe from universes.json (next to this script)
           goes in Author/<universe folder>/ numbered by its universe
           position (no number if it has none); otherwise the first series.
  older ABS-style files: an album tag of "Title: Subtitle" is split
Dramatized books (title says "Dramatized Adaptation", or publisher GraphicAudio)
get " [Dramatized Adaptation]" on the series folder and at the end of the title.
"(Part 1 of 2)" becomes "(1 of 2)". Characters are cleaned the same way as beets
and Chaptarr (colons removed, ? -> !, * -> -, / \\ -> +, <>"| removed).

A book is a folder holding audio files. The whole folder moves, extra files
included; files named after the old audio file are renamed with it and .cue
FILE lines are updated.

Modes:
  plan  <library> <report.csv>             write the plan only
  apply <library> <report.csv> <log.jsonl> carry out rows with action=move/rename
  undo  <log.jsonl>                        reverse a previous apply
Needs mutagen (run inside the beets image).
"""
import csv, json, os, re, sys, unicodedata
from collections import defaultdict

import mutagen

AUDIO_EXT = {".m4b", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma", ".wav"}
SKIP_DIRS = {"@eaDir", "#recycle", ".Trash"}
OWNER = (99, 100)  # nobody:users
DRAMA = "[Dramatized Adaptation]"


# === Tags ===

def first(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    if hasattr(v, "text"):  # ID3 frame
        return first(v.text)
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return str(v).strip()


def read_tags(path):
    f = mutagen.File(path)
    t = getattr(f, "tags", None) or {}
    keys = {k.lower(): k for k in t.keys()}

    def get(*names):
        for n in names:
            k = keys.get(n.lower())
            if k is not None:
                v = first(t[k])
                if v:
                    return v
        return ""

    ff = "----:com.apple.itunes:"
    return {
        "author": get("aART", "TPE2", "albumartist", "album_artist", "©ART", "TPE1", "artist"),
        "album": get("©alb", "TALB", "album"),
        "title": get("©nam", "TIT2", "title"),
        "subtitle": get(ff + "subtitle", "TXXX:SUBTITLE", "TIT3", "subtitle"),
        "series": get(ff + "series", "TXXX:SERIES", "series", "©mvn", "MVNM", "TXXX:MVNM"),
        "position": get(ff + "series-part", "TXXX:SERIES-PART", "series-part", "©mvi", "MVIN", "TXXX:MVIN"),
        "publisher": get(ff + "publisher", ff + "label", "TPUB", "publisher", "label", "organization"),
        "asin": get(ff + "asin", "TXXX:ASIN", "asin", "TXXX:AUDIBLE_ASIN"),
        "track": get("trkn", "TRCK", "tracknumber"),
        "grouping": get("©grp", "TIT1", "grouping", "TXXX:GROUPING"),
    }


# === Naming rules ===

def clean(s):
    """Same character cleanup as beets' replace map and Chaptarr."""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"[\\/]", "+", s)
    s = s.replace("?", "!").replace("*", "-").replace(":", "")
    s = re.sub(r'[<>"|]', "", s)
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(".").strip()


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", s)


DRAMA_RE = re.compile(r"\s*[\[(]\s*dramati[sz]ed( adaptation)?\s*[\])]", re.I)
# In series names also the short "[Dramatized]"
SERIES_DRAMA_RE = re.compile(r"\s*[\[(]\s*dramati[sz]ed( adaptation)?\s*[\])]", re.I)
# "(Unabridged)" says nothing; "(Abridged)" is kept because it marks a different recording
UNABRIDGED_RE = re.compile(r"\s*[\[(]\s*unabridged\s*[\])]", re.I)
PART_RE = re.compile(r"\(\s*part\s+(\d+)\s+of\s+(\d+)\s*\)", re.I)
NUM = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)"
# A series position ("Book 4", "Books 1-3", "Book Two of ...", "Volume 7"); the folder number already says it
SERIES_REPEAT_RE = re.compile(r"\b(books?|part|volume|vol|series|#)\s*[#.,]?\s*" + NUM + r"\b|#\s*\d", re.I)
# A genre blurb ("A LitRPG Adventure"); genres belong in the genre tag
GENRE_RE = re.compile(r"lit\s*rpg|game\s*lit|progression|cultivation|isekai|xianxia|wuxia|dungeon core|harem|"
                      r"system apocalypse|post-apocalyptic|apocalyptic|space opera|military (sci-fi|science fiction)|"
                      r"(epic|urban|dark|portal|high|cozy) fantasy|monster tamer|base building|\bthriller\b|\bromance\b", re.I)


def is_dramatized(tags):
    text = " ".join((tags["album"], tags["title"], tags["subtitle"]))
    return bool(re.search(r"dramati[sz]ed adaptation", text, re.I)) or "graphicaudio" in norm(tags["publisher"])


def series_core(series):
    """"The Expanse Series" -> "expanse", for spotting subtitles that repeat it."""
    s = re.sub(r"^(the|a|an)\s+", "", series.strip(), flags=re.I)
    s = re.sub(r"\s+(series|saga|trilogy|cycle|chronicles)$", "", s, flags=re.I)
    return norm(s)


# Words that make "<series> ..." a position label. Not "Novel", "Novella", "Prequel", "Short Story":
# those say what kind of book it is, so "A Cinder Spires Novella" stays
SERIES_LABEL_RE = re.compile(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|first|second|third|"
                             r"book|series|saga|trilogy|chronicles?|volume|vol|part)\b", re.I)


def keep_subtitle(sub, series_names):
    """Drop subtitle parts that are only a series position ("Discworld, Book 13") or a genre blurb
    ("A LitRPG Adventure"); keep real titles ("The Duke of Caladan") and kinds of book ("A Novel",
    "An Expanse Short Story", "A Prof Croft Prequel")."""
    if not sub or DRAMA_RE.fullmatch(" " + sub):
        return False
    if SERIES_REPEAT_RE.search(sub) or GENRE_RE.search(sub):
        return False
    for name in series_names:
        name = DRAMA_RE.sub("", name)  # "Forest Kingdom [Dramatized Adaptation]" is the series "Forest Kingdom"
        # "Forgotten Realms" out of the series "Forgotten Realms: The Cleric Quintet" or "Forgotten Realms - Sellswords"
        if norm(sub) in {norm(part) for part in re.split(r":| - ", name)}:
            return False
        core = series_core(name)
        if core and (norm(sub) in (core, norm(name)) or series_core(sub) == core or (core in norm(sub) and SERIES_LABEL_RE.search(sub))):
            return False
    return True


def book_title(tags, drama, series_names):
    title = tags["album"] or tags["title"]
    title = UNABRIDGED_RE.sub("", DRAMA_RE.sub("", title))
    title = PART_RE.sub(r"(\1 of \2)", title)
    # A title that carries its own subtitle ("Darkblade Assassin: Hero of Darkness, Book 1") is checked the same way
    title, *extra = title.split(": ")
    # "Casca: The Panzer Soldier: Casca, Book 4" -> keep "The Panzer Soldier" only
    for seg in extra + UNABRIDGED_RE.sub("", DRAMA_RE.sub("", tags["subtitle"])).split(": "):
        seg = seg.strip()
        if keep_subtitle(seg, series_names) and norm(seg) not in norm(title):
            title = f"{title} {seg}"
    if drama:
        title = f"{title} {DRAMA}"
    return clean(title)


def position(p):
    p = p.strip()
    p = re.sub(r"^0+(?=\d)", "", p)  # "01" -> "1"
    p = re.sub(r"(?<=-)0+(?=\d)", "", p)  # "1-03" -> "1-3"
    return clean(p)


def primary_author(a):
    a = a.split(", ")[0].split(";")[0]
    # No-space initials, the way Audible writes them: "R. L. King" -> "R.L. King"
    a = re.sub(r"\b([A-Z])\.\s+(?=[A-Z]\.)", r"\1.", a)
    return clean(a)


def series_entries(tags):
    """Every (series, position) the tags list: SERIES tag first, then the grouping entries."""
    entries = []
    if tags["series"]:
        entries.append((tags["series"], tags["position"]))
    for part in tags["grouping"].split(";") if tags["grouping"] else []:
        m = re.match(r"\s*(.+?)\s*(?:#\s*([\d.]+(?:\s*-\s*[\d.]+)?))?\s*$", part)
        if m and m.group(1):
            entries.append((m.group(1), (m.group(2) or "").replace(" ", "")))
    seen, out = set(), []
    for name, pos in entries:
        k = norm(name)
        if k in seen:
            # keep a position the first entry lacked
            out = [(n, p or pos) if norm(n) == k else (n, p) for n, p in out]
            continue
        seen.add(k)
        out.append((name, pos))
    return out


def load_universes(path):
    """[(folder, universe series, {member series})] from universes.json."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    return [(u["folder"], norm(u["universe"]), {norm(m) for m in u["members"]}) for u in data["universes"]]


def choose_series(entries, universes):
    """(folder series, position, note): the universe when the book is in one, else the first series."""
    names = {norm(n): (n, p) for n, p in entries}
    for folder, uni, members in universes:
        if members & names.keys():
            pos = names.get(uni, ("", ""))[1]
            return folder, pos, "" if pos else f"in universe {folder}, no universe position"
    if entries:
        return entries[0][0], entries[0][1], ""
    return "", "", ""


def fill_from_legacy(tags):
    """Subtitle from an album tag of "Title: Subtitle"."""
    if not tags["subtitle"] and ": " in tags["album"]:
        tags["album"], tags["subtitle"] = tags["album"].split(": ", 1)
    return tags


def track_no(tags, name):
    m = re.match(r"\d+", tags["track"] or "")
    return (int(m.group()) if m else 10**6, natural(name))


def natural(s):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


# === Plan ===

def find_books(library):
    for root, dirs, files in os.walk(library):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        audio = sorted((f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXT and not f.startswith(".")), key=natural)
        if audio:
            yield root, audio, dirs


def plan(library, universes):
    rows = []
    for folder, audio, subdirs in find_books(library):
        rel = os.path.relpath(folder, library)
        row = {"current": rel, "target": "", "action": "", "note": "", "author": "", "series": "",
               "position": "", "title": "", "dramatized": "", "asin": "", "files": len(audio), "renames": ""}
        rows.append(row)
        if subdirs:
            row["action"], row["note"] = "skip", "folder also has subfolders"
            continue
        try:
            tagged = [(a, read_tags(os.path.join(folder, a))) for a in audio]
        except Exception as e:
            row["action"], row["note"] = "skip", f"tag read failed: {e}"
            continue
        tags = fill_from_legacy(dict(tagged[0][1]))
        entries = series_entries(tags)
        drama = is_dramatized(tags)
        author = primary_author(tags["author"])
        title = book_title(tags, drama, [n for n, _ in entries])
        series, pos, unote = choose_series(entries, universes)
        series = clean(series)
        pos = position(pos) if series else ""
        if unote:
            row["note"] = unote
        row.update(author=author, series=series, position=pos, title=title,
                   dramatized="yes" if drama else "", asin=tags["asin"])
        if not author or not title:
            row["action"], row["note"] = "skip", "missing author or title tag"
            continue
        albums = {norm(t["album"]) for _, t in tagged}
        if len(albums) > 1:
            row["note"] = "; ".join(x for x in (row["note"], "files carry different album tags") if x)
        # Always "Series [Dramatized Adaptation]", also when the series name carries "(Dramatized Adaptation)"
        if series and (drama or SERIES_DRAMA_RE.search(series)):
            base = SERIES_DRAMA_RE.sub("", series).strip()
            series = f"{base} {DRAMA}" if base else ""
        name = f"{pos} {title}".strip()
        target = os.path.join(author, series, name) if series else os.path.join(author, name)

        # File names: "Name.ext", or "Name N.ext" for multi-file books
        ordered = [a for a, t in sorted(tagged, key=lambda x: track_no(x[1], x[0]))] if len(audio) > 1 else audio
        renames = {}
        for i, a in enumerate(ordered, 1):
            ext = os.path.splitext(a)[1].lower()
            new = f"{name}{' ' + str(i) if len(ordered) > 1 else ''}{ext}"
            if new != a:
                renames[a] = new
        row["target"] = target
        row["renames"] = json.dumps(renames, ensure_ascii=False) if renames else ""
        if target != rel:
            row["action"] = "move"
        elif renames:
            row["action"] = "rename"
        else:
            row["action"] = "ok"

    # Collisions: two books to one folder, or onto a folder that holds another book
    current = {r["current"] for r in rows}
    # Folders whose book moves elsewhere: another book can take their place once it has gone
    vacated = {r["current"] for r in rows if r["action"] == "move"}
    by_target = defaultdict(list)
    for r in rows:
        if r["action"] in ("move", "rename", "ok"):
            by_target[r["target"]].append(r)
    for target, rs in by_target.items():
        if len(rs) > 1:
            for r in rs:
                r["action"] = "collision"
                r["note"] = "same target as: " + " | ".join(x["current"] for x in rs if x is not r)
        elif rs[0]["action"] == "move" and target in vacated:
            rs[0]["note"] = "moves in after the book there moves out"
        elif rs[0]["action"] == "move" and target in current:
            rs[0]["action"] = "collision"
            rs[0]["note"] = "target folder already holds another book"
        elif rs[0]["action"] == "move" and os.path.exists(os.path.join(library, target)):
            # Folder exists without audio (leftover extra files): merge is fine
            rs[0]["note"] = "target folder exists (no audio); files merge into it"
    return rows


# === Apply / undo ===

def mkdirs(path, log):
    missing = []
    p = path
    while not os.path.isdir(p):
        missing.append(p)
        p = os.path.dirname(p)
    for d in reversed(missing):
        os.mkdir(d)
        os.chown(d, *OWNER)
        log({"op": "mkdir", "path": d})


def move(src, dst, log):
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    os.rename(src, dst)
    log({"op": "move", "src": src, "dst": dst})


def fix_cue(path, old_audio, new_audio, log):
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    new = re.sub(r'^(FILE\s+")' + re.escape(old_audio) + r'(")', lambda m: m.group(1) + new_audio + m.group(2), text, flags=re.M)
    if new != text:
        with open(path, "wb") as f:
            f.write(new.encode(enc))
        log({"op": "cue", "path": path, "old": old_audio, "new": new_audio})


def apply_row(library, r, log):
    src = os.path.join(library, r["current"])
    dst = os.path.join(library, r["target"])
    renames = json.loads(r["renames"]) if r["renames"] else {}

    # 1. Rename audio files (and extra files named after a single audio file) in place
    files = os.listdir(src)
    audio = [f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXT]
    single = len(renames) == 1 and int(r["files"]) == 1
    for old, new in renames.items():
        old_stem, new_stem = os.path.splitext(old)[0], os.path.splitext(new)[0]
        move(os.path.join(src, old), os.path.join(src, new), log)
        if single:
            for f in files:
                if f != old and f.startswith(old_stem + ".") and os.path.splitext(f)[1].lower() not in AUDIO_EXT:
                    nf = new_stem + f[len(old_stem):]
                    if not os.path.lexists(os.path.join(src, nf)):
                        move(os.path.join(src, f), os.path.join(src, nf), log)
        for f in os.listdir(src):
            if f.lower().endswith(".cue"):
                fix_cue(os.path.join(src, f), old, new, log)

    # 2. Move the folder
    if r["action"] == "move":
        if os.path.isdir(dst):  # leftover folder without audio: merge file by file
            for f in os.listdir(src):
                move(os.path.join(src, f), os.path.join(dst, f), log)
            os.rmdir(src)
            log({"op": "rmdir", "path": src})
        else:
            mkdirs(os.path.dirname(dst), log)
            move(src, dst, log)
        # 3. Remove parent folders left empty
        p = os.path.dirname(src)
        while p != library and os.path.isdir(p) and not os.listdir(p):
            os.rmdir(p)
            log({"op": "rmdir", "path": p})
            p = os.path.dirname(p)

    # 4. Old -> new path of every audio file, for updating Chaptarr afterwards
    final_dir = dst if r["action"] == "move" else src
    for a in audio:
        log({"op": "path", "old": os.path.join(src, a), "new": os.path.join(final_dir, renames.get(a, a))})


def apply(library, report, log_path):
    with open(report, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["action"] in ("move", "rename")]
    # A book moving into a folder that another book is leaving goes after that book
    ordered, pending = [], rows
    while pending:
        leaving = {r["current"] for r in pending if r["action"] == "move"}
        ready = [r for r in pending if r["target"] not in leaving or r["target"] == r["current"]] or pending
        ordered += ready
        pending = [r for r in pending if r not in ready]
    rows = ordered
    done = failed = 0
    with open(log_path, "a", encoding="utf-8") as lf:
        def log(entry):
            lf.write(json.dumps(entry, ensure_ascii=False) + "\n")
            lf.flush()
        for r in rows:
            log({"op": "book", "current": r["current"], "target": r["target"]})
            try:
                apply_row(library, r, log)
                done += 1
            except Exception as e:
                failed += 1
                log({"op": "error", "current": r["current"], "error": str(e)})
                print(f"FAILED {r['current']}: {e}", file=sys.stderr)
    print(f"applied {done}, failed {failed}")


def undo(log_path):
    with open(log_path, encoding="utf-8") as f:
        entries = [json.loads(l) for l in f if l.strip()]
    for e in reversed(entries):
        try:
            if e["op"] == "move":
                os.rename(e["dst"], e["src"])
            elif e["op"] == "mkdir":
                if os.path.isdir(e["path"]) and not os.listdir(e["path"]):
                    os.rmdir(e["path"])
            elif e["op"] == "rmdir":
                os.makedirs(e["path"], exist_ok=True)
                os.chown(e["path"], *OWNER)
            elif e["op"] == "cue":
                fix_cue(e["path"], e["new"], e["old"], lambda x: None)
        except Exception as ex:
            print(f"undo failed for {e}: {ex}", file=sys.stderr)
    print(f"undid {len(entries)} log entries")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "plan" and len(sys.argv) == 4:
        library, report = sys.argv[2].rstrip("/"), sys.argv[3]
        universes = load_universes(os.environ.get("STANDARDIZE_UNIVERSES") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "universes.json"))
        print(f"{len(universes)} universes loaded")
        rows = plan(library, universes)
        with open(report, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        counts = defaultdict(int)
        for r in rows:
            counts[r["action"]] += 1
        print(f"{len(rows)} books: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    elif mode == "apply" and len(sys.argv) == 5:
        apply(sys.argv[2].rstrip("/"), sys.argv[3], sys.argv[4])
    elif mode == "undo" and len(sys.argv) == 3:
        undo(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
