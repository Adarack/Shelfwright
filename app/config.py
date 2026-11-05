"""Settings, all from environment variables (set in the Unraid template)."""
import os


def env(name, default=""):
    return os.environ.get(name, default)


# Audiobookshelf: the API for changes, a COPY of its database for reading (never open the live one)
ABS_URL = env("ABS_URL", "http://audiobookshelf").rstrip("/")
ABS_TOKEN = env("ABS_TOKEN")
ABS_LIBRARY_ID = env("ABS_LIBRARY_ID")
ABS_CONFIG_DIR = env("ABS_CONFIG_DIR", "/abs-config")          # holds absdatabase.sqlite (mounted read-only)
ABS_METADATA_DIR = env("ABS_METADATA_DIR", "/abs-metadata")    # ABS's metadata dir; m4b backups land in cache/items/<id>
ABS_LIB_ROOT = env("ABS_LIB_ROOT", "/audiobooks")               # the library path as ABS sees it

# The library as this container sees it
LIBRARY = env("LIBRARY", "/library").rstrip("/")

# Chaptarr: API plus its Postgres database
CHAPTARR_URL = env("CHAPTARR_URL", "").rstrip("/")
CHAPTARR_KEY = env("CHAPTARR_KEY")
CHAPTARR_LIB_ROOT = env("CHAPTARR_LIB_ROOT", "").rstrip("/")
PG_HOST = env("PG_HOST", "")
PG_PORT = env("PG_PORT", "5432")
PG_USER = env("PG_USER", "")
PG_PASSWORD = env("PG_PASSWORD")
PG_DB = env("PG_DB", "")
# Chaptarr checks and fixes run only when its database and library path are set
CHAPTARR_ENABLED = bool(PG_HOST and PG_USER and PG_PASSWORD and PG_DB and CHAPTARR_LIB_ROOT)

# This app
VERSION = env("APP_VERSION", "dev")
DATA = env("DATA_DIR", "/data")
PORT = int(env("PORT", "8484"))
SCAN_INTERVAL_HOURS = float(env("SCAN_INTERVAL_HOURS", "24"))
AUTO_APPLY_SAFE = env("AUTO_APPLY_SAFE", "false").lower() == "true"   # tag sync, dramatized formatting, author paths, empty folders
OWNER = (int(env("PUID", "99")), int(env("PGID", "100")))

# Conversion
# Books whose audio can go into an m4b as it is (AAC, ALAC, E-AC-3) are joined without re-encoding: no quality change.
# Other formats (MP3, Ogg, ...) need a re-encode, which always loses a little; they are only proposed when this is on.
CONVERT_REENCODE = env("CONVERT_REENCODE", "false").lower() == "true"
# Re-encodes use at least the source's bitrate (rounded up to a standard step). 0 = no cap.
CONVERT_MAX_BITRATE = int(env("CONVERT_MAX_BITRATE", "0"))
CONVERT_CONCURRENCY = int(env("CONVERT_CONCURRENCY", "1"))
BACKUP_KEEP_DAYS = int(env("BACKUP_KEEP_DAYS", "30"))          # ABS keeps the original tracks; offered for removal after this

DRAMA = "[Dramatized Adaptation]"
TAG = "Dramatized Adaptation"
# Publishers whose audiobooks are always dramatized productions
DRAMA_PUBLISHERS = env("DRAMA_PUBLISHERS", r"graphic\s*audio|soundbooth")


def problems():
    """Settings that are missing or half set, shown in the UI."""
    out = []
    if not ABS_TOKEN:
        out.append("ABS_TOKEN is empty: fixes and conversions can't reach Audiobookshelf.")
    if not ABS_LIBRARY_ID:
        out.append("ABS_LIBRARY_ID is empty.")
    pg = {"PG_HOST": PG_HOST, "PG_USER": PG_USER, "PG_PASSWORD": PG_PASSWORD, "PG_DB": PG_DB, "CHAPTARR_LIB_ROOT": CHAPTARR_LIB_ROOT}
    missing = [k for k, v in pg.items() if not v]
    if missing and len(missing) < len(pg):
        out.append("Chaptarr checks are off: " + ", ".join(missing) + " empty.")
    return out
