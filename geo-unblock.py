#!/usr/bin/env python3
"""geo-unblocked -- re-acquire geoblocked Apple Music tracks and import them into Music.app.

A named release is tried first, then searches are consulted strictly as
fallbacks -- YouTube, SoundCloud, Mail.Ru, YouTube Music -- stopping at the first
match good enough to import unattended.
"""
import argparse, csv, json, os, re, subprocess, sys, time, unicodedata
from pathlib import Path

ROOT = Path(__file__).parent
SCRATCH = ROOT / "work"          # staging + downloaded artwork
DEST = SCRATCH / "staging"       # files land here, then Music copies them into its own Media folder
STATE = ROOT / "data" / "state.json"
LIST = ROOT / "data" / "list.tsv"
SCRATCH.mkdir(exist_ok=True)
STATE.parent.mkdir(exist_ok=True)

# A result is accepted only if its duration lands within this many seconds of the
# length Apple reported. This is what keeps covers, remixes, live cuts and "sped
# up" edits out of the library -- title text alone is far too easy to fool.
DUR_TOLERANCE = 4

# Past the exact window the rule is asymmetric, and deliberately so: a candidate
# SHORTER than the original is missing audio and can never be the right file,
# while a LONGER one still contains the whole recording -- padded silence, a
# count-in, an extended outro. Short is therefore rejected outright; long is
# merely penalised.
DUR_WIDE = 12          # long by this much: worth measuring the audio to settle
WIDE_PENALTY = 45

# When nothing lands in the exact window, a longer take is the correct fallback:
# it holds every second the original had. Accepted only on the second pass, once
# the strict pass has come up empty, and ranked so the smallest excess wins.
DUR_LONG_MAX = 90
LONG_PENALTY_PER_SEC = 0.25

# Ceiling for a candidate with no artist evidence anywhere -- not in the
# uploader, not in the title. Below the default --min-score, so such a match is
# parked for review rather than imported. Without this, complete title
# containment plus a duration inside the window was already worth 120 on its
# own, and the artist bonuses were pure headroom that could never be
# load-bearing: an unrelated performer's song of the same name auto-imported.
NO_ARTIST_CAP = 115
# The mirror of the rule above, and it was missing for the whole project: the
# artist can be right while the title names a different song. Measured
# 2026-10-16 by comparing every `ok` row's candidate title to the wanted one --
# three of 249 imports shared NOT ONE WORD with the title they replaced, and all
# three were auto-imported on artist evidence plus a duration inside the window:
#   MARU NARA  'I GET DOWN (feat. Nvpvlm)' (ZE, 92s)  <- 'BREAK A LEG' (OMA HOODY, 90s), 122
#   $uicideBoy$ 'N****z' (Magnolia, 261s)             <- 'Now and at the Hour of Our
#                                                        Death (Feat. Bones)' (2025!), 137
#   Gin Wigmore 'Oh My' (Holy Smoke, 257s)            <- 'Hey Ho' (261s), 123
# A candidate sharing no word with the wanted title is, by definition, something
# only a person can vouch for -- so it is parked for review instead of imported,
# and an approval goes in `track_sources.json` where it outlives the run. That
# costs one human verdict on the legitimate case this catches (a TRANSLATED
# title: `Капитан Джек Воробей` is published as `Capitán Jack Sparrow`, zero
# shared tokens, and genuinely right) against three wrong songs in the library.
NO_TITLE_CAP = 115
# Words that name a different rendition, not a different release position. Used
# twice: to penalise a candidate advertising one, and to decide whether a
# qualifier the library carries may be dropped when matching titles.
RENDITION = ("cover", "remix", "live", "karaoke", "reaction", "sped up",
             "slowed", "instrumental", "8d", "8 bit", "chiptune",
             "nightcore", "lyrics video", "acoustic", "demo", "edit")

# Quality cannot be ranked from search results: SoundCloud reports no bitrate at
# all, and Mail.ru's is wrong -- it advertised 128 for a file that arrived at
# 240. The source itself is the only signal available before downloading, and it
# is a decent one. Measured over the twelve recoveries of 2026-08-20: Mail.ru
# delivered 271-424kbps, SoundCloud 160-163k. Small on purpose -- this settles a
# near-tie, it must never outweigh duration or artist evidence. Without it, "No
# Fun" imported at 96k from SoundCloud while a 240k copy of the same 134s master
# sat one rank below, against a library original of 256k.
PROVIDER_QUALITY = {"mr": 4, "sl": 3, "ytm": 3, "yt": 2, "mp": 1, "sc": 0}
# SoundCloud has no equivalent of a `- Topic` channel: an account name is
# self-declared, so "uploader matches the artist" is not provenance there -- a
# fan cover of Sex Bob-Omb's "Threshold" was posted by an account named
# `Sex Bob-omb`, scored 148 on that basis and auto-imported. What DID separate
# the real from the homemade, across all 19 SoundCloud candidates this project
# has ever ranked, is duration: every one of the 12 a human accepted sits within
# 2.19s of the library entry, and both he rejected are 2.91s and 4.41s out. A
# rip of a released recording matches to fractions of a second; a cover never
# does. Engagement was measured too and is NOT usable -- an accepted track by
# The BlueStocking has 197 plays and 3 likes, fewer than a rejected one.
SC_TIGHT = 2.5
NO_PROVENANCE_CAP = 115


# The Apple export romanises Cyrillic artists ("Aleksey Vishnya") while every
# source spells them in Cyrillic. Without folding both to one alphabet, a correct
# Russian match scores as if the artist were absent and never clears the bar.
CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


# Back-transliteration, for building SEARCH queries only -- never for matching.
# Apple romanises Cyrillic artists, and Russian-language catalogues index the
# Cyrillic, so `Raznye Ludi - Такие дела` returns four unrelated songs from
# Mail.ru while `Такие дела Разные` returns the record on the second row.
# Lossy on purpose and known to be: `ю` was written `u`, so `Ludi` comes back as
# `луди`, and a query carrying that token finds NOTHING -- search engines AND
# their terms, and one wrong word is fatal. Hence only the single longest token
# is used: the longer the word, the likelier it round-tripped intact.
# Longest Latin unit first, and where romanisation is ambiguous the MOST LIKELY
# Cyrillic letter -- `y` is both `ы` and `й`, `u` is both `у` and `ю`. Inverting
# CYR mechanically picks whichever key the dict yields and turns "Raznye" into
# "разнйе", which finds nothing. Only one guess is made: measured over the 31
# library tracks with a Cyrillic title and a romanised artist, trying up to
# eight variants instead of one found exactly one extra track (10 against 9),
# for eight times the queries.
_UNCYR = [("shch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"),
          ("sh", "ш"), ("yu", "ю"), ("ya", "я"), ("yo", "ё"), ("y", "ы"),
          ("u", "у"), ("e", "е"), ("i", "и"), ("a", "а"), ("b", "б"),
          ("v", "в"), ("g", "г"), ("d", "д"), ("z", "з"), ("k", "к"),
          ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"),
          ("r", "р"), ("s", "с"), ("t", "т"), ("f", "ф"), ("h", "х")]


def to_cyr(s):
    out, i, low = [], 0, s.lower()
    while i < len(low):
        for lat, cyr in _UNCYR:
            if low.startswith(lat, i):
                out.append(cyr); i += len(lat); break
        else:
            out.append(low[i]); i += 1
    return "".join(out)


def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(CYR.get(c, c) for c in s)
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def tokens(s):
    return set(norm(s).split())


class Refused(RuntimeError):
    """The source answered, and the answer was no.

    Kept apart from an ordinary failure because the two deserve opposite
    treatment: a dropped fragment is worth retrying, a refusal never is."""


# Errors that will not become a success by asking again. `Sign in to confirm`
# covers both walls YouTube puts up -- LOGIN_REQUIRED and the age gate.
REFUSED = re.compile(
    r"Sign in to confirm|LOGIN_REQUIRED|Private video|members[- ]only|"
    r"Video unavailable|removed by the uploader|has been terminated|"
    r"not available in your country", re.I)


# Providers that have said no this run. A refusal belongs to the source, not to
# the track, so it is counted once and consulted everywhere -- including by the
# SEARCH, which is where the time actually goes. Measured 2026-08-20: with
# YouTube behind the login wall `ytm` spent 10.4s per query to return nothing,
# because it resolves every hit with a second call the wall refuses. Four queries
# a track made that 42s of the 104s median -- 40% of a 45-minute run that added
# nothing. Dropping downloads alone left the wall-clock almost unchanged.
REFUSALS = {}
DEAD_AFTER = 3


def note_refusal(prov, err):
    """Record a refusal if that is what `err` is. True once the source is out."""
    if not REFUSED.search(err or ""):
        return False
    REFUSALS[prov] = REFUSALS.get(prov, 0) + 1
    if REFUSALS[prov] == DEAD_AFTER:
        print(f"      {prov}: отказ №{DEAD_AFTER} — исключаю источник "
              f"до конца прогона", flush=True)
    return REFUSALS[prov] >= DEAD_AFTER


def out_of_play(prov):
    return REFUSALS.get(prov, 0) >= DEAD_AFTER


PROJECT = os.environ.get("GEO_UNBLOCKED_NAME", "Geo-Unblocked")
REPLACED_PLAYLIST_NAME = f"{PROJECT} — originals"
REFETCHED_PLAYLIST_NAME = f"{PROJECT} — refetched"


def build_index(playlist=None):
    """Read the tracks to work on straight out of Music, and cache them.

    The Apple export was never the point -- the rule behind it is a track
    property, `cloud status = no longer available`, so the library can be asked
    directly and no manual export is needed. A playlist name may still be given
    to narrow the scope, but none is required and none has to exist: Music's
    scripting interface cannot create a smart playlist with rules anyway.

    Reading live also captures `favorited`, which the Apple export has no column
    for -- so the snapshot this writes is a strictly better backup than the
    export it replaces.

    Dates are emitted as numeric components rather than formatted text, because
    AppleScript renders them in the system locale and a parser keyed to one
    locale silently mangles another."""
    # NOT a `whose` filter. `every track whose cloud status is no longer available`
    # silently omits tracks that genuinely carry that status -- one such track
    # reports database ID 10319 when enumerated and resolves to a different id
    # when looked up, and the filter never returns it. Fetching the statuses in
    # bulk and walking the matching indices finds every one.
    #
    # EVERY field is bulk-read, not just the one that selects the subset. Reading
    # `name of t` per matching track looked like the cheap half of that rule, but
    # `statuses` is a snapshot while `track i of src` resolves live, and the walk
    # took ~60s on 6500 tracks -- long enough for Music to reorder underneath it.
    # The two then address different songs. Measured on 2026-08-20: one rebuild
    # wrote four playable subscription tracks into the list (Justice & Tame
    # Impala, Nicolas Snyder, Serum 114, Sevdaliza) and omitted four genuinely
    # dead ones (Bass Battle, Universal Theme, Garbage Truck, Sex Bob-Omb's
    # Threshold) -- so four tracks had no backup row at all. Sixteen bulk reads
    # are consistent with each other AND take 1.0s instead of 60.
    if playlist:
        container = f'user playlist "{playlist}"'
        gate = "true"
        statuses = ""
    else:
        container = "library playlist 1"
        statuses = "set statuses to (get cloud status of every track of src)"
        gate = '((item i of statuses) as text) is "no longer available"'
    scr = f'''
on f(v)
  try
    if v is missing value then return ""
    return v as text
  on error
    return ""
  end try
end f

on dt(d)
  try
    if d is missing value then return ""
    return ((year of d) as text) & "." & ((month of d as integer) as text) & "." & ¬
           ((day of d) as text) & "." & ((hours of d) as text) & "." & ((minutes of d) as text)
  on error
    return ""
  end try
end dt

tell application "Music"
  set src to {container}
  {statuses}
  set nms to (get name of every track of src)
  set ars to (get artist of every track of src)
  set als to (get album of every track of src)
  set gns to (get genre of every track of src)
  set yrs to (get year of every track of src)
  set drs to (get duration of every track of src)
  set pcs to (get played count of every track of src)
  set pds to (get played date of every track of src)
  set sks to (get skipped count of every track of src)
  set das to (get date added of every track of src)
  set brs to (get bit rate of every track of src)
  set cps to (get composer of every track of src)
  set fvs to (get favorited of every track of src)
  set tns to (get track number of every track of src)
  set tcs to (get track count of every track of src)
  set o to ""
  repeat with i from 1 to (count of nms)
    if {gate} then
      set o to o & my f(item i of nms) & tab & my f(item i of ars) & tab & my f(item i of als) & tab & ¬
        my f(item i of gns) & tab & my f(item i of yrs) & tab & my f(item i of drs) & tab & ¬
        my f(item i of pcs) & tab & my dt(item i of pds) & tab & ¬
        my f(item i of sks) & tab & my dt(item i of das) & tab & ¬
        my f(item i of brs) & tab & my f(item i of cps) & tab & my f(item i of fvs) & tab & ¬
        my f(item i of tns) & tab & my f(item i of tcs) & linefeed
    end if
  end repeat
  return o
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"не удалось прочитать библиотеку: {r.stderr.strip()[:200]}")

    def num(x):
        # AppleScript renders decimals in the system locale, so a comma may stand
        # in for the point. Truncate rather than round: Apple's own export does,
        # and an index built here has to score identically to one built from it.
        try:
            return str(int(float(x.replace(",", "."))))
        except Exception:
            return ""

    def when(x):
        try:
            y, mo, d, h, mi = (int(v) for v in x.split("."))
            return f"{d:02d}.{mo:02d}.{y}, {h:02d}:{mi:02d}"
        except Exception:
            return ""

    cols = ["Name", "Artist", "Album", "Genre", "Year", "Time", "Plays", "Last Played",
            "Skips", "Date Added", "Bit Rate", "Composer", "Favorited",
            "Track Number", "Track Count"]
    rows = []
    for line in r.stdout.split("\n"):
        p = line.split("\t")
        if len(p) < 15 or not p[0].strip():
            continue
        p[5] = num(p[5])          # Time
        p[7] = when(p[7])         # Last Played
        p[9] = when(p[9])         # Date Added
        rows.append(p[:15])

    # Monotonic, never truncating. Membership here is "still unavailable" -- the
    # very property a successful replacement removes -- so a plain rewrite would
    # delete the backup row of every track this tool has already fixed, which is
    # the only surviving record of its history. Keep what is no longer live.
    live = len(rows)
    if LIST.exists():
        fresh = {(r[0], r[1]) for r in rows}
        for old in csv.DictReader(LIST.open(), delimiter="\t"):
            if old.get("Name") and (old["Name"], old.get("Artist", "")) not in fresh:
                rows.append([old.get(c, "") for c in cols])

    # Deterministic order, or --start/--limit silently mean something different
    # after every rebuild. Apple's own export is sorted by artist then title, and
    # matching it keeps a slice stable across index sources.
    rows.sort(key=lambda r: (norm(r[1]), norm(r[0])))

    LIST.parent.mkdir(parents=True, exist_ok=True)
    with LIST.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(cols)
        w.writerows(rows)
    return live


CATALOG = ROOT / "data" / "catalog.tsv"
CATALOG_COLS = ["Persistent ID", "Cloud Status", "Name", "Artist", "Album", "Genre", "Year",
                "Time", "Plays", "Last Played", "Skips", "Date Added", "Bit Rate", "Composer",
                "Favorited", "Track Number", "Track Count"]


def catalog():
    """Snapshot EVERY track, not only the dead ones -- the history of a track is
    worth most before it dies.

    `build_index` reads a track at the moment Music already calls it
    unavailable, and four tracks of 2026-10-16 could not be recovered for the
    reason that nobody had kept anything about them while they were alive. This
    is the same bulk read (sixteen fields in one call, so the lists cannot drift
    apart -- see the snapshot/live-index trap) over the whole library, keyed on
    `persistent ID`, which survives the moment a track goes dead.

    Monotonic like the index: a row that disappears from the library stays, and
    counts and favourites only ever go up -- a track deleted, merged or reset by
    iCloud must not take its record with it."""
    scr = '''
on f(v)
  try
    if v is missing value then return ""
    return v as text
  on error
    return ""
  end try
end f

on dt(d)
  try
    if d is missing value then return ""
    return ((year of d) as text) & "." & ((month of d as integer) as text) & "." & \u00ac
           ((day of d) as text) & "." & ((hours of d) as text) & "." & ((minutes of d) as text)
  on error
    return ""
  end try
end dt

tell application "Music"
  set src to library playlist 1
  set pss to (get persistent ID of every track of src)
  set css to (get cloud status of every track of src)
  set nms to (get name of every track of src)
  set ars to (get artist of every track of src)
  set als to (get album of every track of src)
  set gns to (get genre of every track of src)
  set yrs to (get year of every track of src)
  set drs to (get duration of every track of src)
  set pcs to (get played count of every track of src)
  set pds to (get played date of every track of src)
  set sks to (get skipped count of every track of src)
  set das to (get date added of every track of src)
  set brs to (get bit rate of every track of src)
  set cps to (get composer of every track of src)
  set fvs to (get favorited of every track of src)
  set tns to (get track number of every track of src)
  set tcs to (get track count of every track of src)
  set o to ""
  repeat with i from 1 to (count of nms)
    set o to o & my f(item i of pss) & tab & my f(item i of css) & tab & my f(item i of nms) & tab & \u00ac
      my f(item i of ars) & tab & my f(item i of als) & tab & my f(item i of gns) & tab & \u00ac
      my f(item i of yrs) & tab & my f(item i of drs) & tab & my f(item i of pcs) & tab & \u00ac
      my dt(item i of pds) & tab & my f(item i of sks) & tab & my dt(item i of das) & tab & \u00ac
      my f(item i of brs) & tab & my f(item i of cps) & tab & my f(item i of fvs) & tab & \u00ac
      my f(item i of tns) & tab & my f(item i of tcs) & linefeed
  end repeat
  return o
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"не удалось прочитать библиотеку: {r.stderr.strip()[:200]}")

    def num(x):
        try:
            return str(int(float(x.replace(",", "."))))
        except Exception:
            return ""

    def when(x):
        try:
            y, mo, d, h, mi = (int(v) for v in x.split("."))
            return f"{d:02d}.{mo:02d}.{y}, {h:02d}:{mi:02d}"
        except Exception:
            return ""

    fresh = {}
    for line in r.stdout.split("\n"):
        p = line.split("\t")
        if len(p) < len(CATALOG_COLS) or not p[0].strip():
            continue
        p = p[:len(CATALOG_COLS)]
        p[7] = num(p[7]); p[9] = when(p[9]); p[11] = when(p[11])
        fresh[p[0]] = dict(zip(CATALOG_COLS, p))

    old = {}
    if CATALOG.exists():
        for row in csv.DictReader(CATALOG.open(), delimiter="\t"):
            if row.get("Persistent ID"):
                old[row["Persistent ID"]] = row
    for pid, prev in old.items():
        cur = fresh.get(pid)
        if cur is None:
            fresh[pid] = {**prev, "Cloud Status": "gone"}
            continue
        try:
            if int(prev.get("Plays") or 0) > int(cur.get("Plays") or 0):
                cur["Plays"] = prev["Plays"]
        except ValueError:
            pass
        if (prev.get("Favorited") or "").lower() == "true":
            cur["Favorited"] = "true"
    rows = sorted(fresh.values(), key=lambda r: (norm(r["Artist"]), norm(r["Name"])))
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG.with_suffix(".tsv.tmp")
    with tmp.open("w", newline="") as fh:
        w = csv.DictWriter(fh, CATALOG_COLS, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(CATALOG)
    live = sum(1 for r in rows if r["Cloud Status"] != "gone")
    return live, len(rows)


# Verified copies of LIVE tracks, kept outside ~/Music so Music and
# LaunchServices never index them (see "Back the library up").
ARCHIVE = Path(os.environ.get("GEO_UNBLOCKED_ARCHIVE", "~/Backups/geo-unblocked/archive")).expanduser()
ARCHIVE_INDEX = ROOT / "data" / "archive.json"
ARCHIVE_RETRY_DAYS = 14   # a track with no copy found is asked again after this


def _archive_load():
    try:
        return json.loads(ARCHIVE_INDEX.read_text())
    except Exception:
        return {}


def _archive_save(idx):
    tmp = ARCHIVE_INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=1))
    tmp.replace(ARCHIVE_INDEX)


def mirror(limit, providers, min_score, pace=3.0):
    """Keep a verified copy of tracks that still play, so that the day one dies
    it comes back from disk in seconds instead of from a hunt.

    Every copy is checked against the release's own preview while the release
    still exists -- the one moment that check is cheap. 2026-10-16 showed the
    cost of not doing it: the four tracks still dead were the ones whose
    reference nobody had kept. A track without a reference is recorded as such
    and asked again later; a copy is only archived when its audio matches.

    Most-played first, so a run cut short still protected what matters most.
    Incremental and resumable: it can run a little every night."""
    import shutil
    if not CATALOG.exists():
        catalog()
    rows = [r for r in csv.DictReader(CATALOG.open(), delimiter="\t")
            if r.get("Cloud Status") in ("subscription", "matched") and (r.get("Time") or "").isdigit()]
    rows.sort(key=lambda r: -int(r.get("Plays") or 0))
    idx, now = _archive_load(), time.time()
    todo = []
    for r in rows:
        rec = idx.get(f"{r['Artist']} :: {r['Name']}")
        if rec and rec.get("status") == "ok" and Path(rec.get("path", "")).exists():
            continue
        if rec and rec.get("status") != "ok" and now - rec.get("at", 0) < ARCHIVE_RETRY_DAYS * 86400:
            continue
        todo.append(r)
    have = sum(1 for v in idx.values() if v.get("status") == "ok")
    print(f"в архиве {have}, без копии {len(todo)}; беру {min(limit, len(todo))}", flush=True)
    stats = {}
    for i, r in enumerate(todo[:limit], 1):
        key, want = f"{r['Artist']} :: {r['Name']}", int(r["Time"])
        print(f"[{i}] {key}  ({want}s)", flush=True)
        rec = {"status": "noref", "at": time.time(), "pid": r["Persistent ID"]}
        R = reference(r, want, quiet=True)
        if R:
            rec["status"] = "nomatch"
            cands = pick(r["Name"], r["Artist"], want, providers, r.get("Album", ""), min_score)
            for sc, e, prov in cands[:3]:
                if out_of_play(prov):
                    continue
                url = e.get("url") or e.get("webpage_url")
                try:
                    f = fetch(url, str(DEST / ("mirror " + safe(key)[:80])))
                except Exception as ex:
                    note_refusal(prov, str(ex))
                    continue
                if not f:
                    continue
                v = compare_audio(R["path"], f)
                got = container_duration(f) or effective_duration(f) or 0
                # Longer is fine: the restore cuts the release out of it against
                # the same reference. Shorter is missing audio.
                if v and v["same"] and got >= want - DUR_TOLERANCE:
                    dst = (ARCHIVE / safe(r["Artist"])[:60] / safe(r.get("Album") or "Unknown")[:60]
                           / (safe(r["Name"])[:80] + Path(f).suffix))
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), dst)
                    rec = {"status": "ok", "at": time.time(), "pid": r["Persistent ID"],
                           "path": str(dst), "source": url, "prov": prov, "wave": v["wave"],
                           "env": v["env"], "dur": round(got, 2), "want": want}
                    print(f"      в архив: {prov}, волна {v['wave']:.2f}, {got:.0f}s", flush=True)
                    break
                print(f"      {prov}: " + (f"волна {v['wave']:.2f} при фоне {v['bg']:.2f}" if v else
                                           "сравнить не удалось") + f", {got:.0f}s — не беру", flush=True)
                Path(f).unlink(missing_ok=True)
        else:
            print("      эталона нет — спрошу снова позже", flush=True)
        idx = _archive_load()
        idx[f"{r['Artist']} :: {r['Name']}"] = rec
        _archive_save(idx)
        stats[rec["status"]] = stats.get(rec["status"], 0) + 1
        time.sleep(pace)
    print(f"\n-- зеркало: {stats} --")


FINGERPRINT_KBPS = 16     # see fingerprint(): accuracy measured at 8/16/32k


def _compact(ref_path):
    """Shrink a preview to the size of a fingerprint without losing what the
    comparison needs. Measured 2026-10-16 on three verified pairs: mono 16kHz
    Opus at 16kbps keeps the waveform score at 0.96-0.97 (full preview: 0.99-1.00;
    the bar is 0.85) while an unrelated track stays at 0.02 -- for ~60KB instead
    of ~1MB. 8kbps fell to 0.88 on one track, too close to the bar."""
    src = Path(ref_path)
    if src.suffix == ".opus":
        return src
    dst = src.with_suffix(".opus")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
                        "-c:a", "libopus", "-b:a", f"{FINGERPRINT_KBPS}k", str(dst)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size < 2000:
        dst.unlink(missing_ok=True)
        return src
    src.unlink(missing_ok=True)
    return dst


def fingerprint(limit, pace=1.5):
    """Keep a fingerprint of every track that still plays -- the release's own
    30-second preview, compacted -- instead of a copy of the track.

    The day a track dies, the hunt is automatic but it needs something to judge
    candidates against, and that is exactly what disappears with the track: the
    four tracks still dead on 2026-10-16 were the ones nobody had a reference for.
    Taken while the track is alive, the preview costs one store lookup and ~60KB;
    the whole 6,200-track library comes to ~370MB, against ~45GB for copies.

    Most-played first; a track with no preview found is asked again after two
    weeks. Resumable, so it can run a little every night."""
    if not CATALOG.exists():
        catalog()
    rows = [r for r in csv.DictReader(CATALOG.open(), delimiter="\t")
            if r.get("Cloud Status") in ("subscription", "matched") and (r.get("Time") or "").isdigit()]
    rows.sort(key=lambda r: -int(r.get("Plays") or 0))
    idx, now = _load_refs(), time.time()
    todo = []
    for r in rows:
        rec = idx.get(f"{r['Artist']} :: {r['Name']}") or {}
        if rec.get("path") and Path(rec["path"]).exists():
            continue
        if rec.get("missing") and now - rec.get("at", 0) < ARCHIVE_RETRY_DAYS * 86400:
            continue
        todo.append(r)
    have = sum(1 for v in idx.values() if v.get("path"))
    print(f"слепков {have}, без слепка {len(todo)}; беру {min(limit, len(todo))}", flush=True)
    got = miss = 0
    for r in todo[:limit]:
        key, want = f"{r['Artist']} :: {r['Name']}", int(r["Time"])
        R = reference(r, want, quiet=True)
        idx = _load_refs()
        if R:
            small = _compact(R["path"])
            idx[key] = {**idx.get(key, R), "path": str(small), "pid": r.get("Persistent ID")}
            got += 1
        else:
            idx[key] = {"missing": True, "at": time.time(), "pid": r.get("Persistent ID")}
            miss += 1
        REFS_INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=1))
        if (got + miss) % 50 == 0:
            print(f"  … {got + miss}: слепков {got}, без превью {miss}", flush=True)
        time.sleep(pace)
    print(f"\n-- слепки: новых {got}, без превью {miss} --")


def pick_from_archive(row):
    """A copy archived and verified while the track was alive. Ranked below a
    human-named source and above any search: its audio has already been matched
    against the release's own preview."""
    rec = _archive_load().get(f"{row['Artist']} :: {row['Name']}")
    if not rec or rec.get("status") != "ok" or not Path(rec.get("path", "")).exists():
        return None
    return (998.0, {"duration": rec.get("dur"), "title": row["Name"], "uploader": "archive",
                    "album": row.get("Album", ""), "url": rec["path"],
                    "source_url": rec.get("source"), "thumbnails": [], "trusted_art": False},
            "archive")


def snapshot_refetched():
    """Fold the live state of already-replaced tracks back into the index.

    `--refresh-index` only sees tracks Music still reports as unavailable, and a
    successful replacement is precisely what removes that status. So anything
    the user has since hearted or played on a replaced track exists ONLY in the
    library — and a clean rerun starts by deleting exactly those entries. Run
    this before anything destructive; it is what keeps a heart from vanishing.
    Raises rather than lowers, so it can never overwrite a better value."""
    if not LIST.exists():
        return 0
    out = subprocess.run(["osascript", "-e", f'''
tell application "Music"
  set o to ""
  repeat with t in (every track of user playlist "{REFETCHED_PLAYLIST}")
    set o to o & (name of t) & tab & (artist of t) & tab & (favorited of t) & tab & (played count of t) & tab & (skipped count of t) & linefeed
  end repeat
  return o
end tell'''], capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"snapshot: {out.stderr.strip()[:160]}")

    rows = list(csv.DictReader(LIST.open(), delimiter="\t"))
    by = {(r["Name"], r["Artist"]): r for r in rows}
    changed = 0
    for line in out.stdout.split("\n"):
        p = line.split("\t")
        if len(p) < 5:
            continue
        r = by.get((p[0], p[1]))
        if not r:
            continue
        if p[2] == "true" and (r.get("Favorited") or "").lower() != "true":
            r["Favorited"] = "true"; changed += 1
        for col, val in (("Plays", p[3]), ("Skips", p[4])):
            try:
                if int(val) > int(r.get(col) or 0):
                    r[col] = val; changed += 1
            except ValueError:
                pass
    if changed:
        with LIST.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), delimiter="\t")
            w.writeheader(); w.writerows(rows)
    return changed


def jload(text):
    """Parse JSON from an external command, and treat anything that is not an
    object as no answer at all.

    `null` is valid JSON. A yt-dlp run that fails and prints it parses cleanly
    into None, sails past every `except Exception` guarding these calls, and
    then raises `'NoneType' has no attribute 'get'` several lines later. It
    killed one full pass at track 185 of 316 and a second one on the next
    command. **A failure that parses is the worst kind** -- the same shape as a
    silent `try` around a write, or a script error returning zero."""
    try:
        d = json.loads(text)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def search_ytm(query, n=6):
    """YouTube Music, which indexes distributor-delivered Art Tracks that plain
    YouTube search does not surface. It also returns structured artist and album
    fields, so a release published under a translated title still matches on who
    made it -- that is how "Капитан Джек Воробей" turns up as "Capitán Jack
    Sparrow". Flat results carry no duration, so each candidate costs a second
    call; that is why this provider goes last."""
    import urllib.parse
    url = "https://music.youtube.com/search?q=" + urllib.parse.quote(query) + "#songs"
    r = subprocess.run(["yt-dlp", url, "--flat-playlist", "--print", "%(id)s",
                        "--playlist-end", str(n), "--no-warnings"],
                       capture_output=True, text=True, timeout=120)
    note_refusal("ytm", r.stderr)
    out = []
    for vid in [x.strip() for x in r.stdout.split("\n") if x.strip()][:n]:
        try:
            j = subprocess.run(["yt-dlp", f"https://music.youtube.com/watch?v={vid}",
                                "-J", "--no-warnings", "--skip-download",
                                "--socket-timeout", "20"],
                               capture_output=True, text=True, timeout=90)
            d = jload(j.stdout)
        except Exception:
            continue
        if not d.get("duration"):
            # Each hit costs a second call, and it is that call the wall refuses.
            # Left uncounted, this provider burns ten seconds a query forever.
            if note_refusal("ytm", j.stderr):
                break
            continue
        out.append({
            "duration": d["duration"],
            "title": d.get("track") or d.get("title") or "",
            # the credited artist, not the channel -- this is the field that makes
            # a translated title survive matching
            "uploader": d.get("artist") or d.get("uploader") or "",
            "album": d.get("album") or "",
            "url": f"https://music.youtube.com/watch?v={vid}",
            "thumbnails": d.get("thumbnails") or [],
        })
    return out


ALBUM_SOURCES = ROOT / "data" / "album_sources.json"
_album_cache = {}


def album_entries(album_url):
    """Every track of a release, listed once and reused for its whole album.

    Some catalogues hold a release but cannot find it: JioSaavn carries this
    project's production-music compilation at 320kbps, yet its search returns
    karaoke covers for the same query. Naming the release directly sidesteps
    search entirely -- and one entry covers all fifteen of its tracks."""
    if album_url in _album_cache:
        return _album_cache[album_url]
    r = subprocess.run(
        ["yt-dlp", album_url, "--flat-playlist", "-J", "--no-warnings",
         "--socket-timeout", "30"],
        capture_output=True, text=True, timeout=180)
    try:
        entries = jload(r.stdout).get("entries", []) or []
    except Exception:
        entries = []
    for e in entries:
        e.setdefault("uploader", e.get("artist") or "")
        # yt-dlp appends a "#__youtubedl_smuggle=..." fragment to the flat url.
        # Anything given that back chokes on it, so keep the canonical page url --
        # the extractor re-resolves the media from it just as well.
        if e.get("webpage_url"):
            e["url"] = e["webpage_url"]
        # A release we named ourselves needs no heuristic about who uploaded it:
        # its cover IS the album cover, so mark the image as trustworthy.
        if e.get("thumbnail"):
            e.setdefault("thumbnails", [{"url": e["thumbnail"]}])
        e["trusted_art"] = True
    _album_cache[album_url] = entries
    return entries


TRACK_SOURCES = ROOT / "data" / "track_sources.json"


def pick_from_track_source(row):
    """A source a human approved for this exact track, keyed "Artist :: Track".

    Scoring cannot rank everything: some matches are right for reasons only a
    person can see, and those verdicts have to outlive the run that produced
    them. Without this, a clean rerun quietly re-parks an approved track in
    review -- and if its entry has meanwhile merged, deleting to rerun loses it
    altogether. That is exactly how "Arasaka Tower" disappeared once."""
    try:
        srcs = json.loads(TRACK_SOURCES.read_text()) if TRACK_SOURCES.exists() else {}
    except Exception:
        return None
    src = srcs.get(f"{row['Artist']} :: {row['Name']}")
    # A plain URL, or {"url": ..., "trim": [start, end]} when the release is a
    # cut of what the source holds -- `Creelles Town` is the first 72.051s of a
    # 120s rip, and a URL alone could not say so, which left the one track
    # reconstructed that way outside the tool: a clean pass would have lost it.
    url, trim = (src.get("url"), src.get("trim")) if isinstance(src, dict) else (src, None)
    if not url:
        return None
    r = subprocess.run(["yt-dlp", url, "-J", "--no-warnings", "--skip-download",
                        "--socket-timeout", "30"],
                       capture_output=True, text=True, timeout=120)
    try:
        d = jload(r.stdout)
    except Exception:
        return None
    entry = {
        "duration": d.get("duration"),
        "title": d.get("track") or d.get("title") or row["Name"],
        "uploader": d.get("artist") or d.get("uploader") or "",
        "album": d.get("album") or "",
        "url": d.get("webpage_url") or url,
        "thumbnails": d.get("thumbnails") or ([{"url": d["thumbnail"]}] if d.get("thumbnail") else []),
        # vouched for by a person, so its image is as trustworthy as a named release
        "trusted_art": True,
        "trim": trim,
    }
    return (999.0, entry, "manual")


def pick_from_album(row, want_dur, min_score):
    """Match within a named release before falling back to searching the web."""
    try:
        sources = json.loads(ALBUM_SOURCES.read_text()) if ALBUM_SOURCES.exists() else {}
    except Exception:
        return None
    url = sources.get(row.get("Album") or "")
    if not url:
        return None
    best = None
    for e in album_entries(url):
        sc = score(e, row["Name"], row["Artist"], want_dur, vouched=True)
        if sc is not None and (best is None or sc > best[0]):
            best = (sc, e, "album")
    return best if best and best[0] >= min_score else None


MP3PARTY = "https://mp3party.net"
# A file mp3party is known to store: `Bass Boosted :: Pyrotex`, imported from it
# on 2026-10-16. Asked beside a candidate that comes back empty, it tells "this
# listing has no file" from "this address is refused" -- the two give the same
# 29-byte answer.
MP3PARTY_CONTROL = "https://dl2.mp3party.net/online/8982205.mp3"


def search_mp3party(query, n=6):
    """mp3party.net: a user-upload catalogue, like Mail.ru, and it held a track
    no other source did (`Bass Boosted :: Pyrotex`, 188.000s). Its search page
    carries artist, title, length and the file URL in the markup, so one request
    answers a query. Tags are whatever the uploader typed: the delivered file
    decides, as for every source."""
    import html, urllib.parse
    try:
        code, body = _http(f"{MP3PARTY}/search?q=" + urllib.parse.quote_plus(query), timeout=30)
    except Exception as ex:
        # A wall, not an empty catalogue -- say so and count it like any refusal.
        print(f"      mp: поиск не ответил ({str(ex)[:60]})", flush=True)
        REFUSALS["mp"] = REFUSALS.get("mp", 0) + 1
        return []
    text = body.decode("utf-8", "replace")
    out = []
    for blk in text.split('class="track song-item"')[1:]:
        m = re.search(r'data-js-artist-name="([^"]*)".*?data-js-song-title="([^"]*)"'
                      r'.*?data-js-url="([^"]*)"', blk, re.S)
        d = re.search(r'track__info-item">\s*(\d+):(\d\d)\s*<', blk)
        if not (m and d):
            continue
        artist, title = html.unescape(m.group(1)), html.unescape(m.group(2))
        out.append({"duration": int(d.group(1)) * 60 + int(d.group(2)),
                    # Mail.ru's shape: the credit lives in the title, and the
                    # uploader -- unknown here -- is no provenance at all.
                    "title": f"{artist} - {title}", "uploader": "",
                    "url": m.group(3), "thumbnails": []})
    return out[:n]


_MP3PARTY_CONTROL_OK = None


def _mp3party_get(url):
    """The file, or None for mp3party's 29-byte `failed to get file info: nil`.
    It serves a file only to a request carrying `Range: bytes=0-`; without it,
    or with any other range, a stored file gets the same 29 bytes."""
    code, body = _http(url, timeout=120, headers={"Range": "bytes=0-",
                                                   "Referer": MP3PARTY + "/"})
    if body.startswith(b"failed to get file info"):
        return None
    if code not in (200, 206) or len(body) < 100_000:
        raise RuntimeError(f"mp3party: HTTP {code}, {len(body)} байт")
    return body


def fetch_mp3party(url, out_base):
    global _MP3PARTY_CONTROL_OK
    raw, m4a = Path(f"{out_base}.mp3"), Path(f"{out_base}.m4a")
    for p in (raw, m4a):
        p.unlink(missing_ok=True)
    body = _mp3party_get(url)
    if body is None:
        if _MP3PARTY_CONTROL_OK is None:
            try:
                _MP3PARTY_CONTROL_OK = _mp3party_get(MP3PARTY_CONTROL) is not None
            except Exception:
                _MP3PARTY_CONTROL_OK = False
        if _MP3PARTY_CONTROL_OK:
            # The site answered about THIS listing: there is no file behind it.
            raise RuntimeError("mp3party: листинг есть, файла нет")
        # The control file is stored, and it was refused too: the site is
        # saying no to this address, not to this track. Out for the run.
        REFUSALS["mp"] = DEAD_AFTER
        raise Refused("mp3party отказывает этому адресу — даже контрольный файл не отдан")
    raw.write_bytes(body)
    # The same conversion yt-dlp's `-x --audio-format m4a` gives every other
    # mp3 source, so all imports reach Music in one container.
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-vn", "-c:a", "aac",
                    "-b:a", "256k", str(m4a)], check=True, timeout=300)
    raw.unlink(missing_ok=True)
    return m4a


SLSK = ROOT / "scripts" / "slsk.py"
# aioslsk needs Python 3.11+, and the system python3 is 3.9, so the helper runs
# in its own venv and answers in JSON. README "Soulseek" says how to make both.
SLSK_PY = Path(os.environ.get("GEO_UNBLOCKED_SLSK_PYTHON",
                              "~/.cache/geo-unblocked/slsk-venv/bin/python")).expanduser()
SLSK_CREDENTIALS = Path(os.environ.get("GEO_UNBLOCKED_SLSK_CREDENTIALS",
                                       "~/.config/geo-unblocked/soulseek")).expanduser()
# Seconds to collect answers after a query. Peers answer for minutes, but by 12s
# a query for a specific track has its useful results: measured on `Sex Bob-Omb
# Summertime`, 54 files, the first eight all free-slot FLACs of the right length.
SLSK_WAIT = 12
SLSK_GET_TIMEOUT = 180
_SLSK_READY = None


def _slsk_off(why):
    print(f"      sl: Soulseek пропускаю — {why}", flush=True)
    REFUSALS["sl"] = DEAD_AFTER


def _slsk_ready():
    """Whether Soulseek can be asked at all this run. Every reason it cannot is
    printed once and takes the provider out -- an absent setup is not "this
    track is nowhere on Soulseek"."""
    global _SLSK_READY
    if _SLSK_READY is None:
        why = None
        if not SLSK_PY.exists():
            why = f"нет окружения {SLSK_PY} (README, раздел Soulseek)"
        elif not SLSK_CREDENTIALS.exists():
            why = f"нет файла с логином {SLSK_CREDENTIALS}"
        elif subprocess.run(["pgrep", "-x", "SoulseekQt"], capture_output=True).returncode == 0:
            # One session per account: our login would throw the user's out, or
            # the reverse halfway through a download.
            why = "открыт SoulseekQt под тем же логином"
        if why:
            _slsk_off(why)
        _SLSK_READY = why is None
    return _SLSK_READY and not out_of_play("sl")


def _slsk(args, timeout):
    """The helper's JSON answer, or None once Soulseek is out for the run."""
    r = subprocess.run([str(SLSK_PY), str(SLSK), *args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode in (3, 4):
        _slsk_off(jload(r.stdout).get("error") or f"код {r.returncode}")
        return None
    if r.returncode != 0:
        raise RuntimeError(f"sl: {(r.stderr.strip().splitlines() or ['?'])[-1][:120]}")
    return json.loads(r.stdout)


def search_slsk(query, n=12):
    """Soulseek: what people keep on their own disks, mostly whole releases in
    FLAC or 320k, and it holds things every store and catalogue has dropped.
    One query costs a login and SLSK_WAIT seconds, so it is the last provider.

    Paths, not tags, carry the credit: `Artist\\Album\\18 Title.flac`. The
    artist folder goes into the title so `score()` has artist evidence; the
    album folder does not, because names like `Expanded Edition` or `Live at`
    would read as a rendition the track is not. A peer's name is no provenance
    at all, so `uploader` stays empty, as for Mail.ru."""
    if not _slsk_ready():
        return []
    try:
        res = _slsk(["search", str(SLSK_WAIT), query], SLSK_WAIT + 60)
    except Exception as ex:
        print(f"      sl: поиск не ответил ({str(ex)[:60]})", flush=True)
        REFUSALS["sl"] = REFUSALS.get("sl", 0) + 1
        return []
    out = []
    for r in res or []:
        if not r.get("dur"):
            continue
        parts = [p for p in re.split(r"[\\/]", r["file"]) if p]
        stem = Path(parts[-1]).stem
        title = f"{parts[-3]} - {stem}" if len(parts) >= 3 else stem
        out.append({"duration": r["dur"], "title": title, "uploader": "",
                    "url": f"slsk://{r['user']}/{r['file']}", "thumbnails": [],
                    "abr": r.get("bitrate")})
    return out[:n]


def fetch_slsk(url, out_base):
    import shutil, tempfile
    user, remote = url[len("slsk://"):].split("/", 1)
    m4a = Path(f"{out_base}.m4a")
    m4a.unlink(missing_ok=True)
    if not _slsk_ready():
        raise Refused("Soulseek недоступен в этом прогоне")
    tmp = Path(tempfile.mkdtemp(prefix="slsk-", dir=DEST))
    try:
        res = _slsk(["get", user, remote, str(tmp), str(SLSK_GET_TIMEOUT)], SLSK_GET_TIMEOUT + 60)
        if res is None:
            raise Refused("Soulseek недоступен в этом прогоне")
        if "path" not in res:
            # This peer: queued, offline or refusing. The next candidate is
            # usually the same file on another disk.
            raise RuntimeError(f"sl: {res.get('error')}")
        # AAC 256k like every other source, and the same as Apple's own files.
        # From FLAC it is a first-generation encode.
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", res["path"], "-vn",
                        "-c:a", "aac", "-b:a", "256k", str(m4a)], check=True, timeout=300)
        return m4a
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def search(query, provider, n=6):
    if provider == "ytm":
        return search_ytm(query, n)
    if provider == "mp":
        return search_mp3party(query, n)
    if provider == "sl":
        return search_slsk(query)
    if provider == "mr":
        # Mail.Ru has no search prefix, only a search URL -- and yt-dlp chokes on
        # raw Cyrillic in a URL (latin-1 encode error), so it must be escaped here.
        import urllib.parse
        target = "https://my.mail.ru/music/search/" + urllib.parse.quote(query)
    else:
        prefix = {"yt": "ytsearch", "sc": "scsearch"}[provider]
        target = f"{prefix}{n}:{query}"
    cmd = ["yt-dlp", target, "--flat-playlist", "-J",
           "--no-warnings", "--socket-timeout", "20"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if not out.stdout.strip():
            # An empty answer and a refused one look identical from here, and
            # only one of them means "this source has nothing".
            note_refusal(provider, out.stderr)
            return []
        entries = jload(out.stdout).get("entries", []) or []
        for e in entries:
            # yt-dlp's mailru extractor hands back an `http://` file URL, and
            # moosic.my.mail.ru serves NOTHING over plain http -- 404, with no
            # redirect to follow. Over https the same path returns the file: a
            # 320kbps mp3, right title, right duration. This was recorded in
            # CLAUDE.md for months as "Mail.Ru answers search with file URLs
            # that 404", a property of the source; it cost twelve tracks in one
            # pass and made the provider look dead. It was the scheme.
            u = e.get("url") or ""
            if u.startswith("http://moosic.my.mail.ru/"):
                e["url"] = "https://" + u[len("http://"):]
        return entries[:n]
    except Exception:
        return []


def score(entry, name, artist, want_dur, lenient=False, vouched=False, provider=None):
    """Rank a candidate. Returns None when the duration disqualifies it.

    `lenient` opens the door to noticeably longer takes. It is for the second
    pass only -- reaching for one before the exact window has been exhausted
    would swap a perfect match for a padded one."""
    dur = entry.get("duration")
    if not dur:
        return None
    diff = dur - want_dur
    if diff < -DUR_TOLERANCE:
        return None                      # shorter: audio the original had is gone
    if diff <= DUR_TOLERANCE:
        s = 100 - abs(diff) * 3
    elif diff <= DUR_WIDE:
        s = 100 - DUR_TOLERANCE * 3 - WIDE_PENALTY
    elif lenient and diff <= DUR_LONG_MAX:
        # Continues downward from the wide band instead of restarting above it.
        # Previously +13s outscored +5s, inverting the "smallest excess wins"
        # rule this fallback is built on.
        s = (100 - DUR_TOLERANCE * 3 - WIDE_PENALTY
             - (diff - DUR_WIDE) * LONG_PENALTY_PER_SEC)
    else:
        return None
    title = entry.get("title") or ""
    uploader = entry.get("uploader") or entry.get("channel") or ""

    # "<Artist> - Topic" channels are the label-delivered audio YouTube generates
    # from the same masters the stores use. Always the best available source.
    # Only when it is THIS artist's Topic channel. Unqualified, a wholly
    # unrelated song on any "… - Topic" channel scored 160 and auto-imported.
    if uploader.endswith("- Topic") and close(uploader, artist):
        s += 60
    if tokens(artist) & tokens(uploader) and provider != "sc":
        s += 25

    t = tokens(title)
    want_tokens = tokens(name)
    # A trailing qualifier the library carries and the source omits only matters
    # when it names a different RECORDING. "(Bonus Track)" is a position on a
    # release: "Sex Bob-Omb - No Fun" is the same 134s master as the library's
    # "No Fun (Bonus Track)", but scoring against the full title charged it for
    # two missing words and left it at 119 against a bar of 120 -- the only
    # track of that soundtrack this tool could not recover. "(8 Bit)" is the
    # opposite: drop it and a plain "Threshold" outscores the 8-bit arrangement
    # the library actually holds. So the rendition list decides which it is.
    base = base_title(name)
    if base != name:
        qual = norm(name)[len(norm(base)):].strip()
        if qual and not any(b in qual for b in RENDITION):
            want_tokens = tokens(base)
    s += 20 * len(t & want_tokens) / max(len(want_tokens), 1)
    if tokens(artist) & t:
        s += 10
    # Every word of the title present, not merely some. Partial overlap is weak
    # evidence -- one shared word in a long title scores the same per-token as a
    # complete match -- so completeness is worth calling out on its own.
    if want_tokens and want_tokens <= t:
        s += 12
    # A canonical track title that equals the wanted one exactly. Only structured
    # sources publish this field, and unlike a video headline it carries no
    # decoration, so equality here means the release really is called that. It
    # matters most where the export's "artist" is a compilation uploader rather
    # than a performer, leaving the title as the only honest signal.
    if entry.get("album") is not None and norm(title) == norm(name):
        s += 10

    # Not applied to a release a human named: those entries carry no uploader at
    # all, so the requirement would demote the very verdict it is meant to
    # protect. Requiring artist evidence from a source that has none is how this
    # check silently rejected a compilation it was explicitly told to trust.
    if not vouched and not (tokens(artist) & (tokens(uploader) | t)):
        s = min(s, NO_ARTIST_CAP)
    # And the converse. `want_tokens`, not `tokens(name)`: a droppable qualifier
    # has already been removed above, so "(Bonus Track)" cannot make a title look
    # unrelated here.
    if not vouched and want_tokens and not (want_tokens & t):
        s = min(s, NO_TITLE_CAP)
    # A source with nothing behind the name has to earn it on the clock instead.
    if provider == "sc" and not vouched and abs(diff) > SC_TIGHT:
        s = min(s, NO_PROVENANCE_CAP)
    s += PROVIDER_QUALITY.get(provider, 0)

    # Penalise anything advertising itself as a different rendition. The duration
    # gate catches most of these, but remasters and edits can match on length.
    # Hyphens are normalised on BOTH sides: a candidate spelled "8-bit version"
    # against a wanted "Threshold (8 Bit)" would otherwise be punished for
    # matching, and one spelled "8-bit" against a plain "Threshold" would slip
    # through. That second case is not hypothetical -- "Sex Bob-omb - Threshold
    # (8-bit version)" scored 133 and imported over the band's own recording,
    # 110s against 107s wanted, while the 8-bit arrangement sits in this very
    # library as a separate track by Brian LeBarton. `8d` was in this list and
    # `8 bit` was not.
    tl, nl = title.lower().replace("-", " "), name.lower().replace("-", " ")
    for bad in RENDITION:
        if bad in tl and bad not in nl:
            s -= 45
    return s


def pick(name, artist, want_dur, providers, album="", min_score=120, exhaustive=False):
    """Rank every candidate found, best first, consulting providers as fallbacks.

    Returns a list rather than one winner, because a chosen source can still
    fail to download: Mail.Ru's file URLs 404'd for the whole project (its API
    hands out an `http://` link its own CDN refuses -- see search()), and that
    alone lost twelve tracks in a single pass, each recorded as an error while a
    perfectly good YouTube candidate sat unused in the same result set.

    The moment any query returns a match good enough to import unattended, the
    search stops -- no further queries, no further providers. Everything after
    the first provider exists only for what it could not find, so querying them
    anyway multiplied the network round-trips per track for nothing."""
    # The Apple export transliterates Cyrillic artists ("Akvarium", "Alisa"), so an
    # artist-qualified query can miss entirely. The bare title, gated on duration,
    # is the fallback that recovers those.
    queries = [f"{artist} - {name}", f"{name} {artist}"]
    if album and album not in (name, f"{name} - Single"):
        queries.append(f"{artist} {name} {album}")
    # The same widening `base_title` already does for store lookups, which the
    # audio search never got. A catalogue indexes the recording under its own
    # name, not under the qualifier the library's release adds: Mail.ru returns
    # nothing for "Sex Bob-Omb - No Fun (Bonus Track)" and the right 134s file
    # for "Sex Bob-Omb - No Fun". Four queries, all carrying the qualifier, and
    # the provider looked as if it had nothing.
    base = base_title(name)
    if base != name:
        queries.append(f"{artist} - {base}")
    # A Cyrillic title with a romanised artist cannot reach a Cyrillic catalogue.
    if re.search(r"[а-яё]", name.lower()) and not re.search(r"[а-яё]", artist.lower()):
        tok = max(artist.split(), key=len, default="")
        if len(tok) > 3:
            queries.append(f"{name} {to_cyr(tok)}")
    queries.append(name)

    scored, seen = {}, []

    def add(e, prov, sc):
        if sc is None:
            return
        key = e.get("url") or e.get("webpage_url") or id(e)
        if key not in scored or sc > scored[key][0]:
            scored[key] = (sc, e, prov)

    for prov in providers:
        if out_of_play(prov):
            continue
        for q in queries:
            for e in search(q, prov):
                seen.append((e, prov))
                add(e, prov, score(e, name, artist, want_dur, provider=prov))
            top = max(scored.values(), key=lambda x: x[0], default=None)
            # `exhaustive` is for the second pass, after every candidate found this
            # way failed to DOWNLOAD. Stopping early conflates "a good candidate
            # exists" with "the audio is obtainable", and a source that searches
            # but cannot deliver makes those opposites: with YouTube behind
            # LOGIN_REQUIRED and Mail.ru 404ing every file URL, two full passes
            # returned 0 of 27 tracks while SoundCloud held downloadable copies
            # that were never queried, because a YouTube candidate scored 139 on
            # the first provider and ended the search.
            if not exhaustive and top and top[0] >= min_score:
                return sorted(scored.values(), key=lambda x: -x[0])

    # Nothing in the exact window. Fall back to the shortest take that is still
    # at least as long as the original -- rescoring what was already fetched, so
    # this costs no extra queries.
    top = max(scored.values(), key=lambda x: x[0], default=None)
    if top is None or top[0] < min_score:
        for e, prov in seen:
            add(e, prov, score(e, name, artist, want_dur, lenient=True, provider=prov))
    return sorted(scored.values(), key=lambda x: -x[0])


def safe(s):
    return re.sub(r'[/:\\]', "-", s).strip() or "track"


def fetch(url, out_base, attempts=3):
    """Download native AAC where possible -- no transcode, and Music plays m4a
    directly. Transient failures (throttling, a dropped connection mid-fragment)
    are common enough that a single attempt loses tracks that are perfectly fine.

    A refusal is not one of those, and must not be retried like one. On
    2026-08-20 every YouTube download in the library came back LOGIN_REQUIRED,
    and each was still attempted six times -- roughly two minutes per track
    spent re-asking a question already answered. Retries now stop the moment
    every variant has refused; anything else still gets its three passes."""
    cmd = ["yt-dlp", url, "-f", "bestaudio[ext=m4a]/bestaudio", "-x",
           "--audio-format", "m4a", "--audio-quality", "0",
           "-o", f"{out_base}.%(ext)s", "-q", "--no-warnings",
           "--no-playlist", "--socket-timeout", "30",
           "--retries", "5", "--fragment-retries", "5"]
    # A file already at this path is NOT evidence that this url was fetched.
    # yt-dlp skips an existing target and still exits 0, and the staging name is
    # keyed on artist+title, never on the url -- so a rejected download from an
    # earlier attempt would be imported and stamped with a different source.
    Path(f"{out_base}.m4a").unlink(missing_ok=True)
    if url and Path(url).is_file():
        # An archived copy: already on disk and already verified.
        import shutil
        dst = Path(f"{out_base}{Path(url).suffix}")
        shutil.copy(url, dst)
        return dst
    if "mp3party.net/" in url:
        return fetch_mp3party(url, out_base)
    if url.startswith("slsk://"):
        return fetch_slsk(url, out_base)
    # After a few hundred downloads YouTube starts answering "Sign in to confirm
    # you're not a bot" from this address, and the default client has no way
    # past it. Asking as the embedded player does, and it hands over the same
    # 130k AAC. This recovered a Topic-channel match scoring 214 -- the best
    # candidate of the whole run -- that had been recorded as a hard error.
    # Age-restricted videos are a different wall: only real cookies clear those.
    variants = [cmd]
    if "youtube.com" in url or "youtu.be" in url:
        variants.append(cmd + ["--extractor-args", "youtube:player_client=web_embedded"])
    last = None
    for i in range(attempts):
        # Only when EVERY variant refused. The embedded-player variant exists
        # precisely because it sometimes gets past a wall the default client
        # hits, so one refusal is not the source's final word.
        all_refused = True
        for variant in variants:
            try:
                subprocess.run(variant, check=True, timeout=600,
                               capture_output=True, text=True)
                f = Path(f"{out_base}.m4a")
                if f.exists():
                    return f
                all_refused = False
                last = RuntimeError("yt-dlp produced no output file")
            except Exception as ex:
                Path(f"{out_base}.m4a").unlink(missing_ok=True)
                err = (getattr(ex, "stderr", "") or "").strip() or str(ex)
                err = err.splitlines()[-1] if err.splitlines() else str(ex)
                if REFUSED.search(err):
                    last = Refused(err[:200])
                else:
                    all_refused = False
                    last = RuntimeError(err[:200])
        if all_refused:
            print(f"      отказ, повторять нечего: {str(last)[:100]}", flush=True)
            raise last
        if i + 1 < attempts:
            print(f"      retry {i+1}/{attempts-1} после ошибки: {str(last)[:80]}", flush=True)
            time.sleep(5 * (i + 1))
    raise last


def effective_duration(path):
    """Length of the actual audio, ignoring silence padded onto either end.

    Sources routinely differ from Apple's length by a few seconds purely because
    of leading or trailing silence -- a video edit fades out into quiet, a rip
    keeps the run-off. Measuring the sound itself tells a re-mastered copy of the
    same recording apart from a genuinely different cut, which the container
    duration cannot do."""
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60).stdout.strip())
    except Exception:
        return None
    try:
        err = subprocess.run(
            ["ffmpeg", "-i", str(path), "-af", "silencedetect=noise=-45dB:d=0.5",
             "-f", "null", "-"], capture_output=True, text=True, timeout=180).stderr
    except Exception:
        return dur
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    # Anchor each gap to an actual edge. `ends[0]` is merely the end of the
    # FIRST silence anywhere, so a pause mid-song used to be charged to the
    # start: a 201s file with a 1s gap at 60s reported 129s of audio and the
    # fabricated number was printed as evidence.
    lead = ends[0] if ends and ends[0] < dur / 2 and (not starts or starts[0] < 0.5) else 0.0
    tail = 0.0
    if starts and starts[-1] > dur / 2 and (len(ends) < len(starts) or ends[-1] >= dur - 0.5):
        tail = dur - starts[-1]
    return dur - lead - tail


def container_duration(path):
    try:
        return float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60).stdout.strip())
    except Exception:
        return None


def cut(path, start, end):
    """Keep [start, end] seconds of a file, without re-encoding -- AAC and MP3
    frames are each independently decodable, so a stream copy cuts to within a
    frame (~23ms) and loses nothing."""
    out = path.with_suffix(".cut" + path.suffix)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                    "-i", str(path), "-c", "copy", str(out)], check=True, timeout=180)
    out.replace(path)
    return path


class AudioMismatch(RuntimeError):
    """The file arrived, and it is not the recording the reference says it
    should be. Neither a network failure nor a refusal: the next candidate is
    the answer, and nothing about the source is wrong."""


# ---------------------------------------------------------------------------
# Reference recordings
#
# A 30-second preview of the exact master is all an audio comparison needs, and
# it is the one identifier that survives renaming: budget labels publish one
# recording as `The Imperas`, `Spirit` and `The One World Ensemble`, and only the
# audio says they are the same file. It also usually survives the track itself.
# Apple's CDN keeps serving a removed track's preview; Genius's embedded player
# and archived album pages still name it. Every route below was first walked by
# hand on 2026-10-16 -- see "A 30-second preview is a reference recording" in
# CLAUDE.md -- and is now the tool's job, not a person's.

REFS = SCRATCH / "refs"
REFS_INDEX = REFS / "index.json"


def _http(url, timeout=30, tries=3, headers=None):
    """(status, body). A 404/410 is an answer and comes back as one; anything
    else that fails every try RAISES, because a failed fetch returned as an
    empty body is exactly how "the network blinked" becomes "there is none"."""
    import gzip, urllib.request, urllib.error
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                # The Wayback Machine replays an archived gzip body verbatim.
                if body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                return r.status, body
        except urllib.error.HTTPError as ex:
            if ex.code in (404, 410):
                return ex.code, b""
            last = ex
        except Exception as ex:
            last = ex
        if i + 1 < tries:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url[:70]}: {str(last)[:80]}")


def _ref_fits(row, want, artist, title, dur):
    """Artist, title and length all agree -- the same bar `released_where` uses.

    A reference is what every later candidate is judged against, so a wrong one
    is worse than none: it would reject the right file and bless a lookalike.
    `want` is truncated (Apple's export drops the fraction), so it is compared
    against the middle of the second it names."""
    if not dur or abs(dur - (want + 0.5)) > DUR_TOLERANCE:
        return False
    if not (tokens(row["Artist"]) - _ARTIST_STOP) & (tokens(artist) - _ARTIST_STOP):
        return False
    wt = tokens(base_title(row["Name"]))
    return bool(wt) and wt <= tokens(title)


def _closest(row, want, hits):
    """Best of several fitting hits: the length inside the second the index
    names, then the same album, then the nearest length. Two different
    `Summertime` releases sit in the store at 129.840s and 130.733s; the index's
    130 can only be the second."""
    album = re.sub(r" - (Single|EP)$", "", row.get("Album") or "")
    return sorted(hits, key=lambda h: (not (want <= h["dur"] < want + 1),
                                       not (album and close(h.get("album") or "", album)),
                                       abs(h["dur"] - want - 0.5)))


def _ref_itunes(row, want):
    hits = []
    for r in itunes(f'{row["Artist"]} {base_title(row["Name"])}', "song"):
        d = (r.get("trackTimeMillis") or 0) / 1000
        if r.get("previewUrl") and _ref_fits(row, want, r.get("artistName", ""),
                                             r.get("trackName", ""), d):
            hits.append({"source": "apple", "via": "itunes", "url": r["previewUrl"],
                         "dur": d, "album": r.get("collectionName"),
                         "page": (r.get("trackViewUrl") or "").split("&uo=")[0]})
    return _closest(row, want, hits)


def _ref_deezer(row, want):
    import urllib.parse
    hits = []
    base = base_title(row["Name"])
    for q in (f'artist:"{row["Artist"]}" track:"{base}"', f'{row["Artist"]} {base}'):
        _, body = _http("https://api.deezer.com/search?limit=10&q=" + urllib.parse.quote(q))
        for x in jload(body.decode("utf-8", "replace")).get("data", []) or []:
            if x.get("preview") and _ref_fits(row, want, x["artist"]["name"], x["title"],
                                             x.get("duration")):
                # Whole seconds only, so this length must never be taken as
                # the release's exact one -- `exact` says which sources state it.
                hits.append({"source": "deezer", "via": "deezer", "url": x["preview"],
                             "dur": float(x["duration"]), "exact": False,
                             "album": (x.get("album") or {}).get("title"), "page": x.get("link")})
        if hits:
            break
    return _closest(row, want, hits)


def _ref_genius(row, want):
    """Genius keeps the Apple preview of the track it linked, in the page of its
    embedded player, and that preview outlives the track: the only reference
    ever found for `MARU NARA :: I GET DOWN`, whose album was re-delivered
    without it and whose Apple page the Wayback Machine never captured. The
    player carries title, artist and an exact length, so it is checked like any
    store record."""
    import html, urllib.parse
    _, body = _http("https://genius.com/api/search/multi?q="
                    + urllib.parse.quote(f'{row["Artist"]} {base_title(row["Name"])}'))
    ids = []
    for sec in jload(body.decode("utf-8", "replace")).get("response", {}).get("sections", []):
        if sec.get("type") != "song":
            continue
        for h in sec.get("hits", []):
            res = h.get("result", {})
            if (close(res.get("primary_artist", {}).get("name", ""), row["Artist"])
                    and tokens(base_title(row["Name"])) & tokens(res.get("title", ""))):
                ids.append(res["id"])
    hits = []
    for sid in dict.fromkeys(ids):
        _, page = _http(f"https://genius.com/songs/{sid}/apple_music_player")
        m = re.search(r'preview_track="([^"]*)"', page.decode("utf-8", "replace"))
        if not m:
            continue
        t = jload(html.unescape(m.group(1)))
        if t.get("preview_url") and _ref_fits(row, want, t.get("artist_display_name", ""),
                                              t.get("title", ""), t.get("duration")):
            hits.append({"source": "apple", "via": "genius", "url": t["preview_url"],
                         "dur": float(t["duration"]), "album": None,
                         "page": f"https://genius.com/songs/{sid}"})
    return _closest(row, want, hits)


def _apple_slug(s):
    """Apple's URL slug for a name: lowercase, every run of punctuation a single
    hyphen, non-Latin letters kept and percent-encoded the way CDX stores them."""
    import urllib.parse
    s = re.sub(r"[^\w]+", "-", unicodedata.normalize("NFKC", s).lower()).strip("-_")
    return urllib.parse.quote(s, safe="-")


def _cdx(prefix, limit=40):
    """Archived captures under one URL prefix, newest per URL first. By exact
    prefix only: domain-wide queries time out."""
    import urllib.parse
    _, body = _http("https://web.archive.org/cdx/search/cdx?matchType=prefix&output=json"
                    f"&filter=statuscode:200&fl=timestamp,original&limit={limit}"
                    "&url=" + urllib.parse.quote(prefix, safe="/:%"), timeout=60, tries=2)
    # A list, so not `jload`, which reads every non-object as "no answer" --
    # and here that turned a found capture into none. Malformed raises.
    rows = json.loads(body.decode("utf-8", "replace")) if body.strip() else []
    newest = {}
    for ts, orig in (rows[1:] if rows else []):
        newest[orig] = max(ts, newest.get(orig, ""))
    return sorted(((ts, o) for o, ts in newest.items()), reverse=True)


def _walk_json(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk_json(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_json(v)
    elif isinstance(x, str) and x[:1] in "{[" and "audio-ssl" in x:
        # The older pages keep their data as JSON strings inside JSON.
        try:
            yield from _walk_json(json.loads(x))
        except Exception:
            pass


def _apple_page_tracks(text):
    """(title, artist, seconds, preview url) for every track an archived Apple
    page lists. Two layouts have been seen: the current `serialized-server-data`
    (`title`, `artistName`, `duration` in ms, `previewUrl`) and the older
    media-api cache (`attributes` with `name`, `durationInMillis`, `previews`)."""
    out = []
    for blob in re.findall(r"<script[^>]*>(.*?)</script>", text, re.S):
        if "audio-ssl" not in blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        for d in _walk_json(data):
            if isinstance(d.get("previewUrl"), str) and d.get("title"):
                out.append((d["title"], d.get("artistName") or "", (d.get("duration") or 0) / 1000,
                            d["previewUrl"]))
            a = d.get("attributes") if isinstance(d.get("attributes"), dict) else None
            if a and a.get("name") and a.get("previews"):
                url = (a["previews"][0] or {}).get("url")
                if url:
                    out.append((a["name"], a.get("artistName") or "",
                                (a.get("durationInMillis") or 0) / 1000, url))
    return out


_STORE_CC = None


def store_cc():
    """The account storefront as the two letters Apple puts in its URLs. `STORE`
    is a numeric id, which is all the Search API needs; page URLs need the code,
    and every store result names it in its own link."""
    global _STORE_CC
    if _STORE_CC is None:
        _STORE_CC = "us"
        m = re.match(r"country=(\w\w)$", STORE)
        if m:
            _STORE_CC = m.group(1).lower()
        else:
            for r in itunes("love", "song"):
                if r.get("_own_store"):
                    m = re.search(r"apple\.com/(\w\w)/", r.get("trackViewUrl") or "")
                    if m:
                        _STORE_CC = m.group(1)
                        break
    return _STORE_CC


def _ref_wayback_apple(row, want):
    """An archived Apple album or song page names the preview of every track on
    it, and the CDN still serves those previews years after the release left
    every storefront: `405Okced :: Creelles Town`'s album page, captured
    2024-09-10, listed 21 previews and all 21 still answered. The slug is built
    from the library's own album and title; the ` - Single`/` - EP` suffix is
    dropped so the prefix matches the page with or without it."""
    album = re.sub(r" - (Single|EP)$", "", row.get("Album") or "")
    probes = []
    for cc in dict.fromkeys(["us", store_cc()]):
        if album:
            probes.append(f"music.apple.com/{cc}/album/{_apple_slug(album)}")
        probes.append(f"music.apple.com/{cc}/song/{_apple_slug(base_title(row['Name']))}")
    if album:
        probes.append(f"itunes.apple.com/us/album/{_apple_slug(album)}")
    hits = []
    for prefix in probes:
        for ts, orig in _cdx(prefix)[:3]:
            _, body = _http(f"https://web.archive.org/web/{ts}id_/{orig}", timeout=60, tries=2)
            for title, artist, dur, url in _apple_page_tracks(body.decode("utf-8", "replace")):
                # An album page credits the album artist on its tracks only
                # sometimes; the slug already pinned the release, so an empty
                # credit stands in for the row's own.
                if _ref_fits(row, want, artist or row["Artist"], title, dur):
                    hits.append({"source": "apple", "via": "wayback", "url": url, "dur": dur,
                                 "album": album, "page": f"https://web.archive.org/web/{ts}/{orig}"})
            if hits:
                return _closest(row, want, hits)
    return hits


_BRAVE, _DDG = {}, {}
SPOTIFY_LINK = r"open\.spotify\.com/(?:intl-[a-z]+/)?(?:track|album)/[A-Za-z0-9]{22}"


def brave_spotify_links(q):
    """Spotify links Brave serves for a query, memoised for the process: Brave
    rate-limits after a handful of queries, and artwork and the reference ask
    the same questions. Raises on a 429 -- a block is not a result."""
    import urllib.parse
    if q not in _BRAVE:
        r = subprocess.run(
            ["curl", "-sL", "-A", UA_BROWSER, "--max-time", "25", "-w", "\n%{http_code}",
             "https://search.brave.com/search?q=" + urllib.parse.quote(q)],
            capture_output=True, text=True, timeout=60)
        body, _, code = r.stdout.rpartition("\n")
        if code.strip() == "429" or "captcha" in body[:4000].lower():
            raise Refused("Brave ограничил запросы (429)")
        _BRAVE[q] = ["https://" + x for x in
                     sorted(set(re.findall(SPOTIFY_LINK, urllib.parse.unquote(body))))]
    return _BRAVE[q]


def ddg_spotify_links(q):
    """Second pass: DuckDuckGo image results carry the page they came from."""
    import urllib.parse
    if q not in _DDG:
        _DDG[q] = []
        r = subprocess.run(
            ["curl", "-s", "-A", UA_BROWSER, "--max-time", "20",
             "https://duckduckgo.com/?q=" + urllib.parse.quote(q) + "&iax=images&ia=images"],
            capture_output=True, text=True, timeout=40)
        mm = re.search(r'vqd="?([\d-]+)"?', r.stdout)
        if mm:
            r2 = subprocess.run(
                ["curl", "-s", "-A", UA_BROWSER, "-H", "Referer: https://duckduckgo.com/",
                 "--max-time", "20",
                 f"https://duckduckgo.com/i.js?l=us-en&o=json&q={urllib.parse.quote(q)}"
                 f"&vqd={mm.group(1)}&f=,,,&p=1"],
                capture_output=True, text=True, timeout=40)
            try:
                for x in jload(r2.stdout).get("results", []):
                    _DDG[q] += ["https://" + y for y in re.findall(
                        SPOTIFY_LINK, (x.get("url") or "") + " " + (x.get("source") or ""))]
            except Exception:
                pass
    return _DDG[q]


def _spotify_entity(kind, sid):
    """The public embed page's own data -- no account, no API. A track gives
    name, artists, length and `audioPreview`; an album gives its track list with
    the same fields."""
    _, body = _http(f"https://open.spotify.com/embed/{kind}/{sid}")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  body.decode("utf-8", "replace"), re.S)
    if not m:
        return {}
    return (jload(m.group(1)).get("props", {}).get("pageProps", {})
            .get("state", {}).get("data", {}).get("entity", {})) or {}


def _wayback_spotify_preview(sid):
    """A restricted track has no preview on Spotify today, but an archived copy
    of its page still names `p.scdn.co/mp3-preview/…`, and that URL still
    answers: a 2020 capture gave 30s of `Yartie :: Marked Bills`."""
    caps = _cdx(f"open.spotify.com/track/{sid}", limit=10) or _cdx(f"open.spotify.com/embed/track/{sid}", limit=10)
    for ts, orig in caps[:3]:
        _, body = _http(f"https://web.archive.org/web/{ts}id_/{orig}", timeout=60, tries=2)
        m = re.search(r"https://p\.scdn\.co/mp3-preview/[0-9a-f]{20,}", body.decode("utf-8", "replace"))
        if m:
            return m.group(0), f"https://web.archive.org/web/{ts}/{orig}"
    return None, None


def _ref_spotify(row, want):
    album = re.sub(r" - (Single|EP)$", "", row.get("Album") or "")
    queries = [f"{row['Artist']} {row['Name']} spotify"]
    if album and album != row["Name"]:
        queries.append(f"{row['Artist']} {album} spotify")
    links = []
    for q in queries:
        try:
            links += brave_spotify_links(q)
        except Refused as ex:
            print(f"      {ex} — Spotify-эталон искать нечем", flush=True)
            break
    for q in queries:
        links += ddg_spotify_links(q)
    hits = []
    for link in dict.fromkeys(links):
        kind, sid = link.rstrip("/").split("/")[-2:]
        e = _spotify_entity(kind, sid)
        tracks = ([{"title": e.get("name", ""), "subtitle": ", ".join(a.get("name", "") for a in e.get("artists", [])),
                    "duration": e.get("duration"), "audioPreview": (e.get("audioPreview") or {}).get("url"),
                    "uri": e.get("uri") or f"spotify:track:{sid}"}]
                  if kind == "track" else e.get("trackList") or [])
        for t in tracks:
            d = (t.get("duration") or 0) / 1000
            if not _ref_fits(row, want, t.get("subtitle", ""), t.get("title", ""), d):
                continue
            url, page = t.get("audioPreview"), f"https://open.spotify.com/track/{t['uri'].split(':')[-1]}"
            if not url:
                url, wb = _wayback_spotify_preview(t["uri"].split(":")[-1])
                page = wb or page
            if url:
                hits.append({"source": "spotify", "via": "spotify", "url": url, "dur": d,
                             "album": e.get("name") if kind == "album" else None, "page": page})
        if hits:
            break
    return _closest(row, want, hits)


# Cheapest and most authoritative first. The store and Deezer answer in a
# second; Genius in two; the Wayback Machine takes up to ten per probe; Spotify
# discovery spends Brave queries, which run out.
REF_SOURCES = (("itunes", _ref_itunes), ("deezer", _ref_deezer), ("genius", _ref_genius),
               ("wayback", _ref_wayback_apple), ("spotify", _ref_spotify))


def aliases(row, want, n=4):
    """Other artists the catalogues credit with this title at this length.

    Budget labels issue one master under several invented names, and no query
    keyed on the library's artist reaches the others: `The Imperas :: Twin Peaks
    Theme` is also `Spirit`, `The One World Ensemble` and -- to Apple's own
    matcher -- `Paul Brooks`, and 60 results deep a title search on YouTube
    surfaces none of them under the famous original. Apple's catalogue does
    list them, by title and length. These names are leads only: whatever they
    turn up is admitted by the reference or not at all. Closest length first."""
    base = base_title(row["Name"])
    wt, own = tokens(base), tokens(row["Artist"]) - _ARTIST_STOP
    found = {}

    def consider(artist, title, dur):
        if (dur and abs(dur - (want + 0.5)) <= DUR_TOLERANCE and wt and wt <= tokens(title)
                and not (own & (tokens(artist) - _ARTIST_STOP))):
            found[artist] = min(found.get(artist, 99), abs(dur - want - 0.5))

    for r in itunes(base, "song", limit=200):
        consider(r.get("artistName", ""), r.get("trackName", ""), (r.get("trackTimeMillis") or 0) / 1000)
    try:
        import urllib.parse
        _, body = _http("https://api.deezer.com/search?limit=100&q=" + urllib.parse.quote(base))
        for x in jload(body.decode("utf-8", "replace")).get("data", []) or []:
            consider(x["artist"]["name"], x["title"], x.get("duration"))
    except Exception as ex:
        print(f"      Deezer не ответил ({str(ex)[:60]}) — псевдонимы только из Apple", flush=True)
    return sorted(found, key=found.get)[:n]


def apple_window(dur):
    """Where in the release Apple's 30s preview starts, from the release length.

    Measured on `Creepypasta Land`, 2026-10-16: tracks of 79s and more preview
    from 45.0s, the 72s track from 30.0s, the 25s track from 0 -- in full. That
    is "the first of 45 and 30 that leaves room for 30 seconds", and nothing in
    between was measured, so a length under 60s other than a full preview gets
    no window rather than a guess."""
    if not dur:
        return None
    if dur <= 30.5:
        return 0.0
    for s in (45.0, 30.0):
        if s + 30 <= dur:
            return s
    return None


def _load_refs():
    try:
        return json.loads(REFS_INDEX.read_text())
    except Exception:
        return {}


def reference(row, want, quiet=False):
    """A short recording of the exact release, cached in work/refs/.

    Returns the cached record -- path, where it came from, the release's exact
    length when the source states one, and for an Apple preview where in the
    release it sits -- or None. Only a found reference is cached: a source that
    failed this run is not evidence that none exists, so a miss is asked again
    next time. Each failure is printed, never folded into "not found"."""
    key = f"{row['Artist']} :: {row['Name']}"
    idx = _load_refs()
    hit = idx.get(key)
    if hit and Path(hit.get("path", "")).exists():
        return hit
    REFS.mkdir(parents=True, exist_ok=True)
    for name, fn in REF_SOURCES:
        try:
            found = fn(row, want)
        except Exception as ex:
            print(f"      эталон: {name} не ответил ({str(ex)[:70]}) — это не отсутствие", flush=True)
            continue
        for h in found:
            ext = ".m4a" if ".m4a" in h["url"] else ".mp3"
            dst = REFS / (safe(f"{row['Artist']} - {row['Name']}")[:80] + f".{h['via']}{ext}")
            try:
                code, body = _http(h["url"], timeout=60)
            except Exception as ex:
                print(f"      эталон {h['via']}: не скачался ({str(ex)[:60]})", flush=True)
                continue
            if code != 200 or len(body) < 20000:
                continue
            dst.write_bytes(body)
            got = effective_duration(dst)
            if not got or got < 5:
                dst.unlink(missing_ok=True)
                continue
            exact = h.get("dur") if h.get("exact", True) else None
            rec = {"path": str(dst), "source": h["source"], "via": h["via"], "url": h["url"],
                   "page": h.get("page"), "dur": exact, "secs": round(got, 2),
                   "window": apple_window(exact) if h["source"] == "apple" else None}
            idx = _load_refs()
            idx[key] = rec
            REFS_INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=1))
            if not quiet:
                print(f"      эталон: {h['via']} ({rec['source']}), {got:.0f}s — {h.get('page') or h['url'][:60]}",
                      flush=True)
            return rec
    return None


# ---------------------------------------------------------------------------
# Comparing audio against the reference
#
# Two questions, two measurements, settled on 2026-10-16 (CLAUDE.md, "The RMS
# envelope is not enough"):
#   * the 200 ms RMS envelope answers "is this the same performance" -- and is
#     fooled by a pitch-shifted copy, which keeps the loudness contour intact;
#   * the 8 kHz waveform through a 3 kHz low-pass, segment by segment, answers
#     "is this the same file lineage" -- five uploads of one master agreed at
#     0.97-0.99 in every segment while the shifted copy read 0.00.
# The envelope finds WHERE the preview sits; a 1 kHz waveform pass pins that to
# the millisecond; the 8 kHz segments then decide. Each segment is also scored
# at two deliberately wrong offsets, and that background -- the calibration the
# hand-run method did with "against itself and against a known-wrong track" --
# is reported beside the score.

from itertools import accumulate as _acc
from operator import mul as _mul

WAVE_SAME = 0.85      # median segment correlation of one lineage; see CLAUDE.md
WAVE_MARGIN = 0.40    # ... and this far above the background, so a loop cannot pass


def _pcm(path, sr, lowpass):
    import array
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1",
                          "-af", f"lowpass=f={lowpass}", "-ar", str(sr), "-f", "s16le", "-"],
                         capture_output=True, timeout=600).stdout
    a = array.array("h")
    a.frombytes(raw[:len(raw) // 2 * 2])
    return a


def _scan(r, c, lo, hi):
    """Normalised cross-correlation of `r` against `c[l:l+len(r)]` for every l
    in [lo, hi]. Window statistics come from prefix sums over just the span
    searched, so a long candidate costs only the lags actually asked about."""
    import math
    R = len(r)
    lo, hi = max(lo, 0), min(hi, len(c) - R)
    if R < 2 or hi < lo:
        return []
    mr = sum(r) / R
    r0 = [v - mr for v in r]
    nr = math.sqrt(sum(map(_mul, r0, r0))) or 1e-9
    seg = c[lo:hi + R]
    P = list(_acc(seg, initial=0))
    Q = list(_acc(map(_mul, seg, seg), initial=0))
    out = []
    for l in range(hi - lo + 1):
        var = (Q[l + R] - Q[l]) - (P[l + R] - P[l]) ** 2 / R
        s = sum(map(_mul, r0, seg[l:l + R]))
        out.append((lo + l, s / (nr * math.sqrt(var)) if var > 1e-6 else 0.0))
    return out


def compare_audio(ref_path, cand_path):
    """Where the reference sits in the candidate, and whether it is the same file.

    Returns {env, lag, wave, bg, segs, same}: `lag` is the reference's start in
    the candidate in seconds; `same` is the verdict. None when either file is
    too short to say anything."""
    import math, statistics
    r8, c8 = _pcm(ref_path, 8000, 3000), _pcm(cand_path, 8000, 3000)
    if len(r8) < 8000 * 5 or len(c8) < len(r8):
        return None
    blk = 1600                                         # 200 ms at 8 kHz
    env = lambda x: [math.sqrt(sum(map(_mul, x[i:i + blk], x[i:i + blk])) / blk)
                     for i in range(0, len(x) - blk + 1, blk)]
    er, ec = env(r8), env(c8)
    scan = _scan(er, ec, 0, len(ec) - len(er))
    if not scan:
        return None
    lag_b, env_best = max(scan, key=lambda x: x[1])

    # 1 kHz waveform pass: from the envelope's +-0.2s to the millisecond.
    r1, c1 = _pcm(ref_path, 1000, 400), _pcm(cand_path, 1000, 400)
    fine = _scan(r1, c1, lag_b * 200 - 300, lag_b * 200 + 300)
    lag1 = max(fine, key=lambda x: x[1])[0] if fine else lag_b * 200

    seg, slack = 5 * 8000, 40                           # 5 s segments, +-5 ms
    segs, bgs = [], []
    for s0 in range(0, len(r8) - seg + 1, seg):
        part = r8[s0:s0 + seg]
        at = lag1 * 8 + s0
        got = _scan(part, c8, at - slack, at + slack)
        if not got:
            continue
        segs.append(max(v for _, v in got))
        for wrong in (int(0.37 * 8000), -int(0.53 * 8000)):
            b = _scan(part, c8, at + wrong - slack, at + wrong + slack)
            if b:
                bgs.append(max(v for _, v in b))
    if not segs:
        return None
    wave = statistics.median(segs)
    bg = statistics.median(bgs) if bgs else 0.0
    return {"env": round(env_best, 3), "lag": round(lag1 / 1000, 3), "wave": round(wave, 3),
            "bg": round(bg, 3), "segs": [round(x, 2) for x in segs],
            "same": wave >= WAVE_SAME and wave - bg >= WAVE_MARGIN}


# The storefront every itunes.apple.com lookup runs against.
#
# macOS's `AppleLocale` is an OS region setting and NOT the Apple Media Services
# store, which follows the Apple ID's country -- reading `en_US@rg=byzzzz` as
# "the Belarusian store" is how a whole availability survey was run against the
# wrong catalogue while the user's own share links said `/ge/`. Music does not
# expose the account store to AppleScript either. For most of this project that
# added up to "it cannot be detected", which was wrong: see
# `account_storefront()` below, which reads it off `itunesstored`.
#
# How much it actually matters, measured across the 14 tracks still dead on
# 2026-08-20: ge/by/us agreed on 11, two apparent differences were HTTP 403s
# that rechecked as real absences, and exactly one was genuine --
# `Sex Bob-Omb :: No Fun (Bonus Track)` exists, streamable and exact, in `us`
# alone. So `itunes()` still merges the account's store WITH `us`, and 135 of
# 229 covers were resolved through `us`. What the detection buys is telling the
# two apart afterwards: a record found only in `us` is news, not an action.
def account_storefront():
    """The Apple Media Services storefront, read off the machine instead of guessed.

    This file said for the whole project that the storefront "is in no readable
    preference, so it cannot be detected". That was wrong, and the mistake was
    looking in Music's own preferences: the signed-in account's storefront lives
    in `itunesstored` as a numeric id, and the iTunes Search API accepts that id
    directly as `s=` -- so nothing has to map it to a country code at all.

    Measured 2026-10-16: the id here is 143615, and `search?s=143615` answers with
    `"country": "GEO"` while `country=rs` answers `SRB` and `country=by` answers
    `BLR`. So this account is on the GEORGIAN store -- exactly what the user's own
    share links said all along, while `STORE` silently defaulted to `us`. Moving
    between countries does not change it; only the Apple ID's country does.

    Returns the ready query fragment, so a detected id and a configured country
    code are interchangeable to the caller."""
    env = os.environ.get("GEO_UNBLOCKED_STORE")
    if env:
        return f"country={env}"
    try:
        r = subprocess.run(
            ["defaults", "read", "com.apple.itunesstored",
             "AccountsNotificationPlugin.ActiveStorefrontIdentifier"],
            capture_output=True, text=True, timeout=10)
        # The value is "143615-2,42" -- the storefront id, then a platform suffix.
        m = re.match(r"\s*(\d{6})", r.stdout)
        if m:
            return f"s={m.group(1)}"
    except Exception:
        pass
    # `us` is what an omitted country has always silently meant here, and 135 of
    # 229 covers were resolved that way -- so it stays the fallback, not an error.
    return "country=us"


STORE = account_storefront()


def itunes(term, entity, country=None, limit=25):
    """Search the configured storefront AND `us`, merged.

    Not "one, then the other if empty": a storefront answering with the wrong
    record is not an empty answer, and gating the second look on emptiness hides
    exactly the case worth finding. Searching "Sex Bob-Omb No Fun" in `ge`
    returns a different Sex Bob-Omb release, so a fallback keyed on emptiness
    never fires -- while `us` alone carries the exact track, right artist, right
    length. Whether a result is the wanted one is the caller's question; this
    only has to make sure the caller is shown it.

    `limit` is 25 rather than 8 for the same reason: with 8 that exact match sat
    below the cut, and a truncated list also reads as an absence."""
    import urllib.parse, urllib.request
    out, seen = [], set()
    for c in dict.fromkeys([f"country={country}" if country else STORE, "country=us"]):
        url = (f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}"
               f"&entity={entity}&limit={limit}&{c}")
        res = []
        # Apple throttles with HTTP 403 after a burst, and an unretried 403 is
        # returned here as an empty list -- the same value that means "this
        # storefront does not carry it". Every other network path in this file
        # already retries for exactly that reason.
        for attempt in range(3):
            try:
                res = json.load(urllib.request.urlopen(url, timeout=15))["results"]
                break
            except Exception as ex:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                else:
                    # Say so. Retries do not beat a sustained block -- Apple 403s
                    # this address for the rest of a session after a burst of
                    # queries -- and silence here becomes "no cover exists".
                    print(f"      стор {c} отказал ({str(ex)[:40]}) — "
                          f"это не значит, что записи там нет", flush=True)
        for r in res:
            k = r.get("trackId") or r.get("collectionId")
            if k not in seen:
                seen.add(k)
                # Which storefront answered. Artwork does not care -- 135 of 229
                # covers came from `us` -- but AVAILABILITY does: "add it from
                # Apple Music" about a record that exists only in a store this
                # account cannot reach is advice to click something that is not
                # there. The own-store leg runs first, so a record carried by
                # both keeps `True`.
                r["_own_store"] = (c == STORE)
                out.append(r)
    return out


def base_title(name):
    """The title without a trailing bracketed qualifier -- "(ENDRIU BOOTLEG)",
    "(Radio Edit)", "(Vocal Mix)". Stores rarely carry a bootleg or edit under
    its full name, so a literal query finds nothing even when the release is
    there. Used only to widen a search; the artist and album checks still decide
    whether the result is accepted."""
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", name).strip() or name


def close(a, b):
    """Loose match -- store punctuation and casing never line up exactly."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def big(u):
    return u.replace("100x100bb", "1200x1200bb")


def itunes_art(row):
    """Resolve the real store artwork, and only accept a result that actually
    matches the artist -- taking result[0] blindly hands back an unrelated cover
    whenever the album is a soundtrack or compilation."""
    artist, album = row["Artist"], re.sub(r" - (Single|EP)$", "", row.get("Album") or "")

    # By track: the reliable path. It returns the album name too, so it can be
    # checked against the export instead of trusted.
    for r in itunes(f"{artist} {row['Name']}", "song"):
        if close(r.get("artistName", ""), artist) and close(r.get("trackName", ""), row["Name"]):
            if not album or close(r.get("collectionName", ""), album):
                return big(r["artworkUrl100"])

    alt = base_title(row["Name"])
    if alt != row["Name"]:
        for r in itunes(f"{artist} {alt}", "song"):
            if close(r.get("artistName", ""), artist) and close(r.get("trackName", ""), alt):
                if not album or close(r.get("collectionName", ""), album):
                    return big(r["artworkUrl100"])

    # By album: catches titles the song search fumbles (numeric names like "27").
    if album:
        for r in itunes(f"{artist} {album}", "album"):
            if close(r.get("artistName", ""), artist) and close(r.get("collectionName", ""), album):
                return big(r["artworkUrl100"])

    # Searching by title alone, verified only on duration, was tried and removed.
    # It is tempting for entries credited to an aggregator ("Bass Boosted" and
    # friends re-release other people's tracks), where matching on artist cannot
    # ever succeed. But common titles defeat it: "American Funeral" returns a
    # different artist's song 7s away from the wanted length, which passes any
    # tolerance loose enough to also accept the aggregator cases. Those belong in
    # data/artwork_sources.json, where a human vouches for the specific record.
    return None


ART_PX = 1200


def squarify(src, dst):
    """Force a centred square at a fixed size. Applied to EVERY source, not just
    thumbnails: the store also serves covers a few pixels off square, and Music
    renders anything non-square badly. Returns False if the image is unreadable."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={ART_PX}:{ART_PX}",
         "-q:v", "2", str(dst)],
        capture_output=True, timeout=120)
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 1000


ARTWORK_SOURCES = ROOT / "data" / "artwork_sources.json"


def override_art(row):
    """A cover named by hand for a track the automated sources all miss.

    Values are either a direct image URL or a Spotify link. Spotify is resolved
    through its public oEmbed endpoint, which needs no credentials -- the Web API
    would, and its dashboard gates that behind a Premium subscription, which is
    an absurd price for a handful of covers. oEmbed returns the 300px variant;
    the CDN id maps straight onto the 640px one."""
    try:
        srcs = json.loads(ARTWORK_SOURCES.read_text()) if ARTWORK_SOURCES.exists() else {}
    except Exception:
        return None
    url = srcs.get(f"{row['Artist']} :: {row['Name']}")
    if not url:
        return None
    if "open.spotify.com" not in url:
        return url
    # Retried: a single network hiccup here silently costs a cover that a person
    # went to the trouble of naming, and the run reports "не нашлось" as if the
    # image did not exist.
    thumb = None
    for _ in range(3):
        r = subprocess.run(
            ["curl", "-s", "--max-time", "20", "--retry", "2",
             "https://open.spotify.com/oembed?url=" + url],
            capture_output=True, text=True, timeout=60)
        try:
            thumb = jload(r.stdout).get("thumbnail_url")
        except Exception:
            thumb = None
        if thumb:
            break
        time.sleep(2)
    if not thumb:
        return None
    return thumb.replace("ab67616d00001e02", "ab67616d0000b273") if thumb else None


def library_art(row, dst):
    """Lift artwork off another copy of the same song already in the library.
    This is the best source there is -- it is literally the cover Apple was
    showing the user before the track went dark."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    scr = f'''
tell application "Music"
  repeat with t in (every track of library playlist 1 whose name is "{esc(row['Name'])}" and artist is "{esc(row['Artist'])}")
    try
      if (count of artworks of t) > 0 then
        set d to raw data of artwork 1 of t
        set f to open for access (POSIX file "{dst}") with write permission
        set eof f to 0
        write d to f
        close access f
        return "1"
      end if
    end try
  end repeat
  return "0"
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    return r.returncode == 0 and r.stdout.strip() == "1"


def released_where(row, want):
    """Two different answers with two different actions, kept apart.

    `store` -- the recording is in the user's Apple Music storefront. THAT is
    actionable: he adds it with one click and the history is carried across,
    which worked twice on 2026-08-20.

    `deezer` -- it was published, but is not in Apple Music. There is nothing to
    click: you cannot add a Deezer track to an Apple Music library. Saying
    "добавь из стриминга" here is advice to do something that does not exist,
    and this function existed for one revision printing exactly that about
    `Сатана Печёт Блины :: Sigma`, which is in no Apple storefront under any of
    four query forms. What it is still worth saying is that the record is real,
    so the title, artist and length are confirmed and a copy may surface later.

    Deezer cannot ever be an audio source: its public API serves a 30s 128kbps
    preview and yt-dlp has no extractor for it.

    Both checks are STRICT -- artist AND duration must match. Over the 11 tracks
    still dead that gives one hit and no false ones; matching on title and
    duration alone adds a single wrong lead (`The Imperas :: Twin Peaks Theme`
    answered by Dominik Hauser's own re-recording)."""
    import urllib.parse, urllib.request

    def fits(artist, title, dur):
        return (abs(dur - want) <= DUR_TOLERANCE
                and bool(tokens(row["Artist"]) & tokens(artist))
                and bool(tokens(row["Name"]) & tokens(title)))

    maybe, foreign = None, None
    for term in (f'{row["Artist"]} {base_title(row["Name"])}', base_title(row["Name"])):
        for r in itunes(term, "song"):
            if not r.get("isStreamable"):
                continue
            dur = r.get("trackTimeMillis", 0) / 1000
            if abs(dur - want) > DUR_TOLERANCE:
                continue
            want_t, got_t = tokens(base_title(row["Name"])), tokens(r.get("trackName", ""))
            if not want_t:
                continue
            link = r.get("trackViewUrl", "").split("&uo=")[0]
            if not (want_t & got_t):
                continue
            if tokens(row["Artist"]) & tokens(r.get("artistName", "")):
                if r.get("_own_store"):
                    return "store", link
                # Right record, wrong shop. Measured 2026-10-16: `Sex Bob-Omb ::
                # Summertime` is streamable at 130.733s in `us` and absent from
                # `ge` entirely, and this function reported it as `store` with a
                # `/us/` link -- an instruction the user cannot carry out, and
                # the same defect the docstring describes for Deezer. Keep it as
                # news, not as an action.
                if foreign is None:
                    foreign = ("foreign", link)
                continue
            # EVERY word of the wanted title, not merely some. Partial overlap is
            # what produced every false lead measured: "Confidential Liar (Live)"
            # answered by "Keep It Confidential" on the word `confidential`
            # alone, "Sigma (feat …)" by Epic Soul Factory's unrelated "Sigma",
            # "Can't Do It Like Me" by "Do It Like Me". The one true lead --
            # Geek Music for `FPS Allstars` -- carries the whole title and more.
            if not want_t <= got_t:
                continue
            # The artist did not match -- which is exactly what happens when the
            # library credits an aggregator and the store credits the publisher.
            # `FPS Allstars :: No Place for No Hero` is published as Geek Music,
            # and requiring the artist made this function blind to the one case
            # that prompted writing it. Reported as a lead for a person to judge,
            # never as a verdict: a title+duration match also answers `The
            # Imperas :: Twin Peaks Theme` with someone else's re-recording.
            if maybe is None:
                maybe = ("maybe", f"{r['artistName']} — {r['trackName']} :: {link}")
    # Both forms, as `reference()` does: Deezer's field syntax answered nothing
    # at all on 2026-10-21 (`artist:"Queen" track:"One Vision"` -> 0 results,
    # `artist:"SALUKI"` -> "Walking on Sunshine") while the plain query found the
    # track first. Every check below verifies the hit, so the loose form is safe.
    data = []
    for q in (f'artist:"{row["Artist"]}" track:"{base_title(row["Name"])}"',
              f'{row["Artist"]} {base_title(row["Name"])}'):
        try:
            data += json.load(urllib.request.urlopen(
                f"https://api.deezer.com/search?q={urllib.parse.quote(q)}&limit=10",
                timeout=15)).get("data", [])
        except Exception:
            pass
    for t in data:
        if fits(t["artist"]["name"], t["title"], t.get("duration", 0)):
            return "deezer", t.get("link")
    # A confirmed Apple record in another storefront says more than a Deezer
    # listing and more than an aggregator guess, so it is preferred to `maybe`
    # -- but it is still not something to click.
    if foreign:
        return foreign
    # Last, because a guess must never shadow a fact: for `Сатана Печёт Блины ::
    # Sigma` the store offers an unrelated "Sigma" by Epic Soul Factory while
    # Deezer confirms the real release, and returning the guess first hid it.
    return maybe if maybe else (None, None)


def deezer_art(row):
    """Second store, checked the same way as iTunes. Catches releases missing from
    Apple -- compilations and small labels especially."""
    import urllib.parse, urllib.request
    artist, album = row["Artist"], re.sub(r" - (Single|EP)$", "", row.get("Album") or "")
    res = []
    # The field syntax alone silently stopped answering (see `released_where`).
    for q in dict.fromkeys([f'artist:"{artist}" track:"{row["Name"]}"',
                            f'artist:"{artist}" track:"{base_title(row["Name"])}"',
                            f'{artist} {row["Name"]}', f'{artist} {base_title(row["Name"])}']):
        try:
            res += json.load(urllib.request.urlopen(
                f"https://api.deezer.com/search?q={urllib.parse.quote(q)}&limit=8",
                timeout=15)).get("data", [])
        except Exception:
            pass
    for x in res:
        if not close(x.get("artist", {}).get("name", ""), artist):
            continue
        if album and not close(x.get("album", {}).get("title", ""), album):
            continue
        cov = x.get("album", {}).get("cover_xl") or x.get("album", {}).get("cover_big")
        if cov:
            return cov
    return None


GENIUS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def genius_art(row):
    """Genius covers releases the stores dropped or never carried -- Russian rap
    and small labels especially. It exposes no album, so the match is verified on
    artist and title only; that is weaker than the store checks, which is why it
    sits below them in the chain. Its public endpoint refuses non-browser
    requests, hence the User-Agent."""
    import urllib.parse
    term = urllib.parse.quote(f"{row['Artist']} {row['Name']}")
    try:
        r = subprocess.run(
            ["curl", "-sL", "-A", GENIUS_UA, "--max-time", "20",
             f"https://genius.com/api/search/multi?q={term}"],
            capture_output=True, text=True, timeout=40)
        data = jload(r.stdout)
    except Exception:
        return None
    for sec in data.get("response", {}).get("sections", []):
        if sec.get("type") != "song":
            continue
        for hit in sec.get("hits", []):
            res = hit.get("result", {})
            if not close(res.get("primary_artist", {}).get("name", ""), row["Artist"]):
                continue
            if not close(res.get("title", ""), row["Name"]):
                continue
            for key in ("song_art_image_url", "header_image_url"):
                if res.get(key):
                    return res[key]
    return None


SPOTIFY_CREDS = ROOT / "data" / "spotify.json"
_spotify_token = {}


def spotify_token():
    """Client-credentials token, or None when no credentials are configured.

    Spotify's unofficial web-player token endpoints are blocked and their use is
    expressly against the Developer Terms, so this goes through the documented
    flow. Credentials come from the environment or a gitignored file -- they are
    the user's own app credentials and never belong in the repository."""
    if _spotify_token.get("value"):
        return _spotify_token["value"]
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not (cid and secret) and SPOTIFY_CREDS.exists():
        try:
            c = json.loads(SPOTIFY_CREDS.read_text())
            cid, secret = c.get("client_id"), c.get("client_secret")
        except Exception:
            return None
    if not (cid and secret):
        return None
    r = subprocess.run(
        ["curl", "-s", "--max-time", "20", "-X", "POST",
         "https://accounts.spotify.com/api/token",
         "-u", f"{cid}:{secret}",
         "-d", "grant_type=client_credentials"],
        capture_output=True, text=True, timeout=40)
    try:
        _spotify_token["value"] = jload(r.stdout)["access_token"]
    except Exception:
        return None
    return _spotify_token["value"]


def spotify_art(row):
    """Last artwork source. Spotify carries releases the others drop entirely,
    but exposes no reliable album for a search hit, so like Genius it is checked
    on artist and title only -- hence its place at the end of the chain."""
    tok = spotify_token()
    if not tok:
        return None
    import urllib.parse
    q = urllib.parse.quote(f"track:{row['Name']} artist:{row['Artist']}")
    r = subprocess.run(
        ["curl", "-s", "--max-time", "20", "-H", f"Authorization: Bearer {tok}",
         f"https://api.spotify.com/v1/search?q={q}&type=track&limit=8"],
        capture_output=True, text=True, timeout=40)
    try:
        items = jload(r.stdout)["tracks"]["items"]
    except Exception:
        return None
    for it in items:
        names = " ".join(a.get("name", "") for a in it.get("artists", []))
        if not close(names, row["Artist"]) or not close(it.get("name", ""), row["Name"]):
            continue
        images = (it.get("album") or {}).get("images") or []
        if images:
            return max(images, key=lambda i: i.get("width") or 0)["url"]
    return None


UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# Hosts that serve release artwork. A square image from one of these is a cover;
# a square image from anywhere else is a photograph someone chose. Note the
# Spotify prefix: `ab67616d` is album art, while `ab676161` is an artist portrait
# and would put a face where a sleeve belongs.
ART_CDNS = (
    "i.scdn.co/image/ab67616d",          # Spotify, album art only
    "f4.bcbits.com/img", "f1.bcbits.com/img",  # Bandcamp
    "images.genius.com",
    "mzstatic.com/image",                 # Apple
    "cdn-images.dzcdn.net/images/cover",  # Deezer
    "c.saavncdn.com",                     # JioSaavn
    "avatars.yandex.net/get-music-content",
)


def image_search_art(row, hint=""):
    """Last resort: a picture search, filtered down to things that can only be
    covers. Three conditions together, because any one alone is too weak -- the
    image must be square, it must come from a host that serves release artwork,
    and the result's caption must contain every word of the track title. That is
    what separates the sleeve from the band photo and the video thumbnail sitting
    next to it in the same results."""
    import urllib.parse

    def query(q):
        r = subprocess.run(
            ["curl", "-s", "-A", UA_BROWSER, "--max-time", "20",
             "https://duckduckgo.com/?q=" + urllib.parse.quote(q) + "&iax=images&ia=images"],
            capture_output=True, text=True, timeout=40)
        m = re.search(r'vqd="?([\d-]+)"?', r.stdout)
        if not m:
            return []
        r2 = subprocess.run(
            ["curl", "-s", "-A", UA_BROWSER, "-H", "Referer: https://duckduckgo.com/",
             "--max-time", "20",
             f"https://duckduckgo.com/i.js?l=us-en&o=json&q={urllib.parse.quote(q)}"
             f"&vqd={m.group(1)}&f=,,,&p=1"],
            capture_output=True, text=True, timeout=40)
        try:
            return jload(r2.stdout).get("results", [])
        except Exception:
            return []

    # The export romanises Russian artists, and a romanised query simply does not
    # reach a cover captioned in Cyrillic. The source's own title usually carries
    # the native spelling, so it is worth a query of its own.
    queries = [f"{row['Artist']} {row['Name']} album cover"]
    if hint and tokens(hint) - tokens(f"{row['Artist']} {row['Name']}"):
        queries.append(f"{hint} album cover")
    results = []
    for q in queries:
        results.extend(query(q))

    want, who = tokens(row["Name"]), tokens(row["Artist"])
    best = None
    for x in results:
        url, w, h = x.get("image") or "", x.get("width") or 0, x.get("height") or 0
        cap = tokens(x.get("title") or "")
        if not w or abs(w - h) > 2 or w < 500:
            continue
        if not any(c in url for c in ART_CDNS):
            continue
        if want and not want <= cap:
            continue
        # The artist has to be in the caption too. Title alone is not enough:
        # searching "Лодка" returns a square, correctly-sized Apple cover for a
        # different artist's song of the same name, and it looks entirely
        # convincing until someone opens it.
        if who and not who & cap:
            continue
        if best is None or w > best[0]:
            best = (w, url)
    return best[1] if best else None


def spotify_via_search(row):
    """Find the track on Spotify without an account, and verify what was found.

    Spotify is the only catalogue that still has some of these releases: a track
    is "no longer available" precisely because Apple removed it, so no Apple
    lookup can ever return it, and Deezer, Genius, JioSaavn and MusicBrainz do
    not carry the long tail either.

    Its API is behind Premium and oEmbed needs a track link, so the link has to
    be discovered. Brave is the one search engine that serves those links in
    plain HTML -- DuckDuckGo (html and lite), Bing (even with its base64
    redirects decoded), Mojeek, Marginalia, Startpage, Ecosia and Yandex all
    return none. Image results are kept as a second pass, since they carry the
    page they came from.

    Every candidate is resolved through oEmbed and accepted only when Spotify's
    OWN title matches. Nothing here trusts a caption or a search ranking."""
    import urllib.parse
    album = re.sub(r" - (Single|EP)$", "", row.get("Album") or "")
    queries = [f"{row['Artist']} {row['Name']} spotify"]
    if album and album != row["Name"]:
        queries.append(f"{row['Artist']} {album} spotify")

    want, seen = norm(row["Name"]), set()

    def check(links):
        for link in links:
            if link in seen:
                continue
            seen.add(link)
            o = subprocess.run(
                ["curl", "-s", "--max-time", "15",
                 "https://open.spotify.com/oembed?url=https://" + link.lstrip("htps:/")
                 if not link.startswith("http") else
                 "https://open.spotify.com/oembed?url=" + link],
                capture_output=True, text=True, timeout=30)
            try:
                d = jload(o.stdout)
            except Exception:
                continue
            if norm(d.get("title") or "") != want:
                continue
            thumb = d.get("thumbnail_url")
            if thumb:
                return thumb.replace("ab67616d00001e02", "ab67616d0000b273")
        return None

    # Brave first, DuckDuckGo images second -- both memoised and shared with
    # the reference search, which asks the same two questions of the same
    # rate-limited engine.
    for q in queries:
        try:
            links = brave_spotify_links(q)
        except Refused:
            # A CAPTCHA page has no links, so an unchecked 429 turns "we were
            # blocked" into "there is no cover" -- say so instead.
            print("      Brave ограничил запросы (429) — ссылку найти нечем", flush=True)
            break
        got = check([x for x in links if "/track/" in x])
        if got:
            return got
    for q in queries:
        got = check([x for x in ddg_spotify_links(q) if "/track/" in x])
        if got:
            return got
    return None


def artist_uploaded(uploader, row):
    """True when the upload comes from the artist themselves -- their own channel,
    or someone credited in the track title (a `feat.` collaborator). Their page
    image is the release art; a random re-uploader's is whatever they picked."""
    if not uploader:
        return False
    if uploader.endswith("- Topic"):
        return True
    return close(uploader, row["Artist"]) or bool(tokens(uploader) & tokens(row["Name"]))


def fetch_image(url, dst, tries=3):
    """Download an image, retrying. Every artwork source ends here, and a single
    network blip used to cost a cover silently -- the run reported "не нашлось",
    indistinguishable from an image that does not exist. One track lost the very
    cover a person had named by hand, twice."""
    for i in range(tries):
        dst.unlink(missing_ok=True)
        subprocess.run(["curl", "-sL", "--max-time", "30", "--retry", "2",
                        "-o", str(dst), url], timeout=90)
        if dst.exists() and dst.stat().st_size > 2000:
            return True
        if i + 1 < tries:
            time.sleep(2)
    dst.unlink(missing_ok=True)
    return False


def get_art(row, thumb_url, uploader="", trusted_thumb=False, hint=""):
    art = SCRATCH / "cover.jpg"
    raw = SCRATCH / "art_raw"
    for p in (art, raw):
        p.unlink(missing_ok=True)

    # 0. A cover named by hand. A deliberate human choice outranks every guess.
    named = override_art(row)
    if named:
        if fetch_image(named, raw) and squarify(raw, art):
            raw.unlink(missing_ok=True)
            return art, "named"
        raw.unlink(missing_ok=True)

    # 1. Another copy already in the library -- the cover Apple itself showed.
    if library_art(row, str(raw)) and raw.stat().st_size > 2000 and squarify(raw, art):
        raw.unlink(missing_ok=True)
        return art, "library"
    raw.unlink(missing_ok=True)

    # 2-4. Stores first (artist+album verified), then Genius (artist+title only).
    for url, label in ((itunes_art(row), "store"), (deezer_art(row), "deezer"),
                       (genius_art(row), "genius"), (spotify_art(row), "spotify"),
                       (spotify_via_search(row), "spotify-found"),
                       (image_search_art(row, hint), "image-search")):
        if not url:
            continue
        if fetch_image(url, raw) and squarify(raw, art):
            raw.unlink(missing_ok=True)
            return art, label
        raw.unlink(missing_ok=True)

    # 5. Last resort: the source page image, but only when the artist posted it
    # themselves. A random re-uploader's thumbnail is a picture they chose, and
    # embedding it would overwrite correct art. No artwork beats wrong artwork.
    if thumb_url and (trusted_thumb or artist_uploaded(uploader, row)):
        if fetch_image(thumb_url, raw, tries=2) and squarify(raw, art):
            raw.unlink(missing_ok=True)
            return art, "source-thumb"
        raw.unlink(missing_ok=True)
    return None, "none"


def tag(path, row, thumb_url, src_url="", uploader="", trusted_thumb=False, hint=""):
    art, art_src = get_art(row, thumb_url, uploader, trusted_thumb, hint)
    if art is None:
        art = SCRATCH / "__missing__"

    tmp = path.with_suffix(".tagged.m4a")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path)]
    if art.exists() and art.stat().st_size > 1000:
        cmd += ["-i", str(art), "-map", "0:a", "-map", "1:v",
                "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-c:a", "copy"]
    for k, v in [("title", row["Name"]), ("artist", row["Artist"]),
                 ("album", row["Album"]), ("genre", row["Genre"]),
                 ("date", row["Year"]), ("album_artist", row["Artist"]),
                 ("composer", row.get("Composer")),
                 ("comment", build_comment(row, src_url))]:
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    if row.get("Track Number"):
        cmd += ["-metadata", f"track={row['Track Number']}/{row.get('Track Count') or ''}".rstrip("/")]
    cmd += [str(tmp)]
    subprocess.run(cmd, check=True, timeout=180)
    tmp.replace(path)
    return art_src


_LIVE_INDEX = None
# Words too common to count as artist evidence: "The Decemberists" and "The
# Smiths" share `the`, and that is not the same act.
_ARTIST_STOP = {"the", "and", "feat", "ft", "with", "x"}


def _live_index():
    """Every playable store copy in the library, keyed for fuzzy lookup.

    Read ONCE per process, in one bulk call -- name, artist and cloud status
    together, so the three lists cannot drift apart (see the snapshot/live-index
    trap in CLAUDE.md). Our own imports never become `subscription` or
    `purchased`, and turn `matched` only asynchronously, so a run that imports
    does not invalidate what this answers."""
    global _LIVE_INDEX
    if _LIVE_INDEX is not None:
        return _LIVE_INDEX
    scr = '''
tell application "Music"
  set src to library playlist 1
  set nms to (get name of every track of src)
  set ars to (get artist of every track of src)
  set cs to (get cloud status of every track of src)
  set o to ""
  repeat with i from 1 to (count of nms)
    set c to ((item i of cs) as text)
    if c is "subscription" or c is "matched" or c is "purchased" then
      set o to o & (item i of nms) & tab & (item i of ars) & linefeed
    end if
  end repeat
  return o
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        # Loudly, and without caching the failure: "nothing is live" is the
        # answer that makes the tool import a duplicate.
        raise RuntimeError(f"не удалось прочитать живые копии: {r.stderr.strip()[:160]}")
    idx = {}
    for line in r.stdout.split("\n"):
        p = line.split("\t")
        if len(p) == 2 and p[0].strip():
            idx.setdefault(norm(p[0]), []).append(tokens(p[1]) - _ARTIST_STOP)
    _LIVE_INDEX = idx
    return idx


def already_live(name, artist):
    """A geoblocked entry whose song also exists as a playable subscription copy
    needs no replacement -- it was already swapped by hand at some point and the
    dead entry just never got cleaned up. Downloading over it would add a worse
    128k file next to a 256k one that already works.

    It used to ask Music for `name is X and artist is Y`, exact, through a
    `whose` filter -- and Apple re-credits artists. On 2026-10-16 two index rows
    said `АИГЕЛ` while the live copies had become `AIGEL`; the exact match
    failed and the tool imported a duplicate of each, the very thing this
    function exists to prevent. The title is now compared through `norm()` and
    the artist by shared tokens, both of which fold Cyrillic to Latin exactly as
    the scorer does, so the two can no longer disagree about whether a song is
    the same one."""
    want = tokens(artist) - _ARTIST_STOP
    return any(want & have for have in _live_index().get(norm(name), []))


def fold_into_index(name, artist, fav, plays):
    """Raise the index row from a copy that is about to stop existing.

    Raise-only, like every other write in this project: a re-run must converge,
    and a stale zero must never overwrite a real number."""
    if not LIST.exists():
        return
    rows = list(csv.reader(LIST.open(encoding="utf-8"), delimiter="\t"))
    if not rows:
        return
    h = rows[0]
    try:
        ni, ai = h.index("Name"), h.index("Artist")
        fi, pi = h.index("Favorited"), h.index("Plays")
    except ValueError:
        return
    changed = False
    for r in rows[1:]:
        if len(r) <= max(ni, ai, fi, pi) or r[ni] != name or r[ai] != artist:
            continue
        if fav and r[fi].strip().lower() != "true":
            r[fi] = "true"; changed = True
        try:
            if int(plays) > int(r[pi] or 0):
                r[pi] = str(int(plays)); changed = True
        except ValueError:
            pass
    if changed:
        with LIST.open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh, delimiter="\t").writerows(rows)


def _ids_alive(ids):
    """How many of these persistent IDs the library still holds -- read in bulk,
    so the answer comes from the library and not from the loop that deleted."""
    r = subprocess.run(["osascript", "-e",
                        'tell application "Music" to get persistent ID of every track of library playlist 1'],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"не удалось перечитать библиотеку: {r.stderr.strip()[:160]}")
    have = {x.strip() for x in r.stdout.split(",")}
    return sum(1 for i in ids if i in have)


def purge_ours(name, artist):
    """Music matches a re-imported file to the existing entry and KEEPS the old
    one -- so re-running silently changes nothing unless the previous import is
    removed first. Only our own imports are touched: a file track carrying our
    marker (or the bare URL comment older runs wrote), or one sitting in the
    refetched playlist. The user's own local files have neither, and streaming
    originals are not file tracks at all.

    It used to `delete t` inside `repeat with t in (every file track … whose …)`
    -- both traps this file lists, in a destructive function: `whose`
    under-returns, and deleting while iterating invalidates the specifiers the
    loop holds. On 2026-10-16 it returned "1" for `$uicideBoy$ :: N****z` while
    the file it meant was still there at the same persistent ID. Now: one bulk
    read picks the targets by persistent ID, a second script deletes exactly
    those IDs, and the count returned is what a fresh read says is gone."""
    scr = f'''
on g(v)
  try
    if v is missing value then return ""
    return v as text
  on error
    return ""
  end try
end g
tell application "Music"
  set src to library playlist 1
  set ps to (get persistent ID of every track of src)
  set nms to (get name of every track of src)
  set ars to (get artist of every track of src)
  set ks to (get class of every track of src)
  set cms to (get comment of every track of src)
  set fvs to (get favorited of every track of src)
  set pcs to (get played count of every track of src)
  set ours to {{}}
  try
    set ours to (get persistent ID of every track of user playlist "{REFETCHED_PLAYLIST}")
  end try
  set o to ""
  repeat with i from 1 to (count of ps)
    if ((item i of ks) as text) is "file track" then
      set c to my g(item i of cms)
      set p to (paragraph 1 of (c & return))
      set o to o & (item i of ps) & tab & (item i of nms) & tab & (item i of ars) & tab & ¬
        ((item i of ps) is in ours) & tab & p & tab & ((item i of fvs) as text) & tab & ¬
        ((item i of pcs) as text) & linefeed
    end if
  end repeat
  return o
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"не удалось прочитать библиотеку: {r.stderr.strip()[:160]}")
    targets, fav, plays = [], False, 0
    marks = (MARK,) + tuple(OLD_MARKS)
    for line in r.stdout.split("\n"):
        p = line.split("\t")
        if len(p) < 7 or p[1] != name or p[2] != artist:
            continue
        if not (p[4].startswith(marks) or p[3].strip() == "true"):
            continue
        targets.append(p[0])
        fav = fav or p[5].strip() == "true"
        try:
            plays = max(plays, int(p[6]))
        except ValueError:
            pass
    if not targets:
        return "0"
    # Whatever these files carried that the index does not, the index gets FIRST
    # -- it is the only record that survives the delete. A heart added to an
    # import after the last --snapshot lives nowhere else, and this function is
    # what destroys it: `No Fun (Bonus Track)` was purged and re-imported three
    # times in one session and lost its favourite on the first pass, silently,
    # because the index still said false and apply_export_history is set-only.
    fold_into_index(name, artist, fav, str(plays))
    ids = ", ".join(f'"{t}"' for t in targets)
    d = subprocess.run(["osascript", "-e", f'''
tell application "Music"
  repeat with pid in {{{ids}}}
    delete (first track of library playlist 1 whose persistent ID is (pid as text))
  end repeat
end tell'''], capture_output=True, text=True, timeout=300)
    left = _ids_alive(targets)
    if left:
        raise RuntimeError(f"purge_ours: {left} из {len(targets)} наших копий не удалились "
                           f"({d.stderr.strip()[:120]})")
    # The dead original was parked because a replacement stood beside it. With
    # the replacement gone it is just a dead track again, and the geoblocked
    # list already shows it -- left parked, `$uicideBoy$ :: N****z` sat in the
    # originals playlist for a day with nothing to be the original of. If the
    # caller re-imports next, park_original files it again.
    unpark_original(name, artist)
    return str(len(targets))


def unpark_original(name, artist):
    """Take the dead entries for this song out of the originals playlist --
    out of the playlist only; the library keeps them. Selected and removed by
    persistent ID; a fresh read afterwards must find none, or it raises."""
    read = f'''
tell application "Music"
  set pl to user playlist "{REPLACED_PLAYLIST}"
  -- `every track` of an EMPTY playlist is -1728, not an empty list
  if (count of tracks of pl) is 0 then return ""
  set ps to (get persistent ID of every track of pl)
  set nms to (get name of every track of pl)
  set ars to (get artist of every track of pl)
  set o to ""
  repeat with i from 1 to (count of ps)
    set o to o & (item i of ps) & tab & (item i of nms) & tab & (item i of ars) & linefeed
  end repeat
  return o
end tell'''
    def parked():
        r = subprocess.run(["osascript", "-e", read], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"не удалось прочитать {REPLACED_PLAYLIST}: {r.stderr.strip()[:160]}")
        return [p[0] for p in (l.split("\t") for l in r.stdout.split("\n"))
                if len(p) >= 3 and p[1] == name and p[2] == artist]
    ids = parked()
    if not ids:
        return 0
    lst = ", ".join(f'"{i}"' for i in ids)
    subprocess.run(["osascript", "-e", f'''
tell application "Music"
  set pl to user playlist "{REPLACED_PLAYLIST}"
  repeat with pid in {{{lst}}}
    delete (first track of pl whose persistent ID is (pid as text))
  end repeat
end tell'''], capture_output=True, text=True, timeout=120)
    left = parked()
    if left:
        raise RuntimeError(f"unpark_original: {len(left)} из {len(ids)} не вынуты из {REPLACED_PLAYLIST}")
    return len(ids)


def inherit_history(name, artist, album=""):
    """Carry listening history from the dead original onto our new file, so the
    leftover duplicate can be deleted without losing anything.

    Every count takes the LARGER of the two, on both the read and the write
    side. The read used to accumulate across matches, which is wrong twice over:
    it is not idempotent under repeated --repair, and the lookup is by name and
    artist alone, so two different recordings of one song add together. This
    library holds such a pair -- "Our Hearts Condemn Us" by Jozef Van Wissem
    exists on two albums with 3 and 6 plays -- and summing wrote 9 onto a track
    that had 3, with no way back since every later write only raises.

    Each write gets its own `try`. One shared `try` meant the first failure
    skipped the rest, and the statement that fails is known: copying a date
    object between tracks. It sat ahead of the favourite write, so the favourite
    was never reached."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    alb = f' and album is "{esc(album)}"' if album else ""
    scr = f'''
tell application "Music"
  set deadPlays to 0
  set deadSkips to 0
  set deadDate to missing value
  set deadLoved to false
  set deadRating to 0
  set found to false
  repeat with t in (every track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}"{alb})
    try
      if (cloud status of t as text) is "no longer available" then
        set found to true
        if (played count of t) > deadPlays then set deadPlays to (played count of t)
        try
          if (skipped count of t) > deadSkips then set deadSkips to (skipped count of t)
        end try
        try
          if (played date of t) is not missing value then
            if deadDate is missing value or (played date of t) > deadDate then set deadDate to (played date of t)
          end if
        end try
        try
          if favorited of t then set deadLoved to true
        end try
        try
          if (rating of t) > deadRating then set deadRating to (rating of t)
        end try
      end if
    end try
  end repeat
  if not found then return "0"

  set moved to 0
  repeat with t in (every file track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}"{alb})
    set didSomething to false
    try
      if deadPlays > (played count of t) then
        set (played count of t) to deadPlays
        set didSomething to true
      end if
    end try
    try
      if deadSkips > (skipped count of t) then set (skipped count of t) to deadSkips
    end try
    try
      -- Isolated on purpose: copying a date object between tracks is the write
      -- that fails, and it must not be able to take the others down with it.
      if deadDate is not missing value then
        set cur to missing value
        try
          set cur to (played date of t)
        end try
        if cur is missing value or deadDate > cur then set (played date of t) to deadDate
      end if
    end try
    try
      if deadLoved and not (favorited of t) then
        set (favorited of t) to true
        set didSomething to true
      end if
    end try
    try
      if deadRating > 0 and (rating of t) is 0 then set (rating of t) to deadRating
    end try
    if didSomething then set moved to moved + 1
  end repeat
  -- Report what changed, read back, not what the dead entry happened to hold.
  return (moved as text)
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"inherit_history: {r.stderr.strip()[:160]}")
    return r.stdout.strip()


def apply_export_history(row):
    """Write the play history recorded in the Apple export onto our new file.

    This is the backstop that makes history survive regardless of what Music does
    on import: a merge keeps it, a non-merge leaves a dead duplicate to inherit
    from, and a destroyed entry leaves nothing at all -- but the export always
    has the numbers. Counts are only raised, never lowered."""
    want = int(row.get("Plays") or 0)
    skips = int(row.get("Skips") or 0)
    last = row.get("Last Played") or ""
    # Composer is carried here rather than only in the file tags, so a repair pass
    # can restore it on tracks imported before it was noticed. BPM is deliberately
    # absent: the export has no such column and not one track in the library has it
    # set, so there has never been anything to carry.
    composer = (row.get("Composer") or "").strip()
    # The favourite has no other backstop. Once an entry merges, our file IS the
    # user's record and the only carrier of the flag -- 25 of 28 replaced tracks
    # are in that state. Set-only, never cleared, so it stays idempotent.
    fav = (row.get("Favorited") or "").strip().lower() == "true"
    pre = ""
    try:
        d, t = last.split(", ")
        dd, mm, yyyy = (int(x) for x in d.split("."))
        hh, mi = (int(x) for x in t.split(":"))
        # `current date` inside a `tell application "Music"` block is captured by
        # Music and errors out, so build the date in plain context first.
        pre = (f'set dt to (current date)\nset day of dt to 1\n'
               f'set year of dt to {yyyy}\nset month of dt to {mm}\nset day of dt to {dd}\n'
               f'set time of dt to ({hh} * hours + {mi} * minutes)\n')
    except Exception:
        pre = ""
    if not want and not skips and not pre and not composer and not fav:
        return "0"
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    scr = pre + f'''
tell application "Music"
  set t to (first file track of library playlist 1 whose name is "{esc(row['Name'])}" and artist is "{esc(row['Artist'])}")
  if {want} > (played count of t) then set (played count of t) to {want}
  try
    if {skips} > (skipped count of t) then set (skipped count of t) to {skips}
  end try
  {'try' if composer else ''}
  {f'if (composer of t) is "" then set (composer of t) to "' + esc(composer) + '"' if composer else ''}
  {'end try' if composer else ''}
  {'try' if fav else ''}
  {'if not (favorited of t) then set (favorited of t) to true' if fav else ''}
  {'end try' if fav else ''}
  {"try" if pre else ""}
  {"if (played date of t) is missing value or dt > (played date of t) then set (played date of t) to dt" if pre else ""}
  {"end try" if pre else ""}
  return (played count of t) as text
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    return r.stdout.strip() if r.returncode == 0 else "0"


def add_to_music(path):
    # The one interpolation in this file that used to go unescaped. A track
    # named `No Place for No Hero (From "Borderlands 2")` produces a path with a
    # double quote, and the script would not even compile -- so that track could
    # never be imported, by any number of retries.
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    # The file reference is built OUTSIDE the tell block. Written as
    # `tell application "Music" to add POSIX file "…"`, Music itself resolves the
    # `POSIX file` and intermittently answers -1700 "Can't make some data into the
    # expected type": four refusals on 2026-10-16 (Creelles Town once, Guns three
    # times in a row), and the same path added as an alias succeeded every time.
    scr = (f'set a to (POSIX file "{esc(str(path))}") as alias\n'
           f'tell application "Music" to add a')
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


REPLACED_PLAYLIST = REPLACED_PLAYLIST_NAME    # the dead Apple Music entries
REFETCHED_PLAYLIST = REFETCHED_PLAYLIST_NAME  # the files we brought back

# Leading marker on every comment we write. It identifies our own imports so a
# rerun can replace them without touching the user's own files, and it survives
# the extra lines appended after it -- which the bare URL it replaced did not.
MARK = "geo-unblocked"
OLD_MARKS = ("unblocked", "geo-refetch")  # comments written by earlier versions


def build_comment(row, src_url):
    """Carry the facts Music refuses to store anywhere else.

    `date added` is read-only in Music's scripting interface, so the original
    add date has no home in the library at all -- written here it at least stays
    attached to the track and survives export/import."""
    bits = [f"{MARK} · source: {src_url}"]
    orig = []
    if row.get("Date Added"):
        orig.append(f"added {row['Date Added']}")
    if row.get("Plays"):
        orig.append(f"{row['Plays']} plays")
    if row.get("Last Played"):
        orig.append(f"last played {row['Last Played']}")
    if row.get("Skips"):
        orig.append(f"{row['Skips']} skips")
    if orig:
        bits.append("original Apple Music entry: " + " · ".join(orig))
    if row.get("Bit Rate") or row.get("Kind"):
        was = " ".join(x for x in (row.get("Bit Rate"), row.get("Kind")) if x)
        bits.append(f"was: {was}")
    return "\n".join(bits)


PLAYLIST_NOTES = {
    REFETCHED_PLAYLIST: "Треки, погашенные геоблоком Apple Music по ошибке дистрибьютора: "
                        "найдены заново, скачаны, импортированы обратно — прослушивания и "
                        "сердечки сохранены, дата добавления нет. Источник каждого — в "
                        "комментарии трека. Собрано {date} проектом geo-unblocked.",
    REPLACED_PLAYLIST: "Мёртвые записи Apple Music, чья замена уже стоит в библиотеке. "
                       "История с них снята. Не опустошать вручную: после слияния ссылка "
                       "указывает на живой трек — только --retire-originals. {date}.",
}


def ensure_playlists():
    # Written only into an empty field, so a description the user wrote by hand
    # survives every later run. The cap is 255 characters and Music enforces it
    # by silently truncating mid-word, so these are built to fit.
    import datetime
    today = datetime.date.today().strftime("%d.%m.%Y")
    for p in (REPLACED_PLAYLIST, REFETCHED_PLAYLIST):
        note = PLAYLIST_NOTES.get(p, "").format(date=today).replace('"', "'")[:255]
        # Count, never just `exists`. Two playlists can end up sharing a name --
        # iCloud sync is enough to do it -- and every reference in this file is
        # BY NAME, which AppleScript resolves to whichever comes first. That is
        # how 230 imports and 228 imports ended up in two "Geo-Unblocked —
        # refetched" playlists that differed by two tracks. Merging them is
        # destructive and is the user's call, so this only refuses to be quiet.
        r = subprocess.run(["osascript", "-e", f'''
tell application "Music"
  set n to 0
  repeat with pl in (every user playlist)
    if (name of pl) is "{p}" then set n to n + 1
  end repeat
  if n is 0 then
    make new user playlist with properties {{name:"{p}"}}
  end if
  set pl to (first user playlist whose name is "{p}")
  try
    -- `missing value as string` is the literal text "missing value", not "",
    -- so an unset description reads as thirteen characters of content and a
    -- naive emptiness test never writes anything.
    set d to description of pl
    if d is missing value or (d as string) is "" then set description of pl to "{note}"
  end try
  return n
end tell'''], capture_output=True, text=True, timeout=60)
        try:
            if int(r.stdout.strip()) > 1:
                print(f"      ⚠ плейлистов с именем «{p}» больше одного "
                      f"({r.stdout.strip()}) — треки будут попадать в первый из них, "
                      f"объедини их вручную", flush=True)
        except ValueError:
            pass


def add_to_playlist(track_ref, playlist):
    subprocess.run(["osascript", "-e", f'''
tell application "Music"
  duplicate ({track_ref}) to user playlist "{playlist}"
end tell'''], capture_output=True, text=True, timeout=60)


def park_original(name, artist):
    """File the dead Apple Music entry into a playlist so it can be reviewed and
    culled in one go later. `cloud status = no longer available` is exactly what a
    geoblocked original reports, so it targets the dead entry and nothing else --
    our own import is a local file track and never carries that status."""
    # Reports the playlist's actual growth, not the number of duplicate calls.
    #
    # `before` is a reserved word in AppleScript (`insert ... before`), so the
    # earlier version of this script never compiled -- and the caller returned
    # "0" on any non-zero exit, making a syntax error indistinguishable from
    # "nothing to park". It parked nothing for the whole project and said so in
    # a way that read as normal. Hence the raise below, and hence no `try`
    # around the write.
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    scr = f'''
tell application "Music"
  set pl to user playlist "{REPLACED_PLAYLIST}"
  set wasCount to (count of tracks of pl)
  set alreadyThere to {{}}
  try
    set alreadyThere to (get database ID of every track of pl)
  end try
  repeat with t in (every track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}")
    set dead to false
    try
      if ((cloud status of t as text) is "no longer available") then
        -- Parking runs again on every repair pass, so a second call must not
        -- file a second copy of the same dead entry.
        if (database ID of t) is not in alreadyThere then set dead to true
      end if
    end try
    -- The duplicate is deliberately OUTSIDE the try. Wrapped, it failed
    -- silently for the entire project: 28 imports parked nothing at all, and
    -- the review-and-cull playlist was empty without anyone being told.
    if dead then duplicate t to pl
  end repeat
  return ((count of tracks of pl) - wasCount)
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    return r.stdout.strip() if r.returncode == 0 else "0"


def thumb_and_title(url):
    """One call for both: the source's image and its own title. The title is the
    only place a Russian artist's native spelling survives -- the export has it
    romanised -- and image search needs it to find a cover captioned in Cyrillic."""
    if not url:
        return None, ""
    try:
        r = subprocess.run(["yt-dlp", url, "--print", "thumbnail", "--print", "title",
                            "--no-warnings", "--skip-download", "--socket-timeout", "20"],
                           capture_output=True, text=True, timeout=90)
        lines = [x.strip() for x in r.stdout.split("\n") if x.strip()]
    except Exception:
        return None, ""
    thumb = lines[0] if lines and lines[0].startswith("http") else None
    title = lines[1] if len(lines) > 1 else ""
    return thumb, title


def retire_originals(apply=False):
    """Delete each dead geoblocked entry that now has a working replacement --
    but only after the replacement provably holds at least as much history.

    This is the last destructive step of the whole project and the one with no
    undo, so it does the checking itself rather than trusting that an earlier
    pass did. It was right not to trust: of sixteen pairs, two had never had
    their history copied at all. Both were `already_live` -- a song the user had
    swapped by hand at some point, where the tool correctly imported nothing and
    therefore also inherited nothing. One of them was carrying twelve plays and
    a favourite that a bulk delete would have taken with it.

    Raise-only, never a sum: thirteen of the sixteen pairs already matched
    exactly, and adding them would double a record that was already correct.
    That is the opposite of `--dedupe`, where each row counted listens the
    others did not."""
    scr = '''tell application "Music"
  set src to library playlist 1
  set S to (ASCII character 1)
  set B to (ASCII character 2)
  set AppleScript's text item delimiters to S
  set r to ((get name of every track of src) as text) & B
  set r to r & ((get artist of every track of src) as text) & B
  set r to r & ((get cloud status of every track of src) as text) & B
  set r to r & ((get persistent ID of every track of src) as text) & B
  set r to r & ((get played count of every track of src) as text) & B
  set r to r & ((get skipped count of every track of src) as text) & B
  set r to r & ((get favorited of every track of src) as text) & B
  set r to r & ((get rating of every track of src) as text)
  return r
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=300)
    if r.returncode:
        print("не смог прочитать библиотеку:", r.stderr.strip()); return
    cols = [b.split("\x01") for b in r.stdout.rstrip("\n").split("\x02")]
    if len({len(c) for c in cols}) != 1:
        print("столбцы разошлись — читать нельзя:", [len(c) for c in cols]); return
    keys = ["name", "artist", "status", "pid", "plays", "skips", "fav", "rating"]
    lib = [dict(zip(keys, [c[i] for c in cols])) for i in range(len(cols[0]))]

    live, dead = {}, []
    for t in lib:
        if t["status"] == "no longer available":
            dead.append(t)
        else:
            live.setdefault((norm(t["name"]), norm(t["artist"])), []).append(t)
    pairs = [(d, max(live[(norm(d["name"]), norm(d["artist"]))], key=lambda x: int(x["plays"])))
             for d in dead if live.get((norm(d["name"]), norm(d["artist"])))]
    orphans = len(dead) - len(pairs)
    if not pairs:
        print(f"нечего убирать: мёртвых {len(dead)}, замен нет"); return
    print(f"мёртвых записей: {len(dead)}   с готовой заменой: {len(pairs)}   останется: {orphans}")

    plan = []
    for d, l in pairs:
        lift = {}
        for f in ("plays", "skips", "rating"):
            if int(d[f]) > int(l[f]):
                lift[f] = d[f]
        if d["fav"] == "true" and l["fav"] != "true":
            lift["fav"] = "true"
        plan.append((d, l, lift))
        mark = "  <- " + ", ".join(f"{k}:{v}" for k, v in lift.items()) if lift else ""
        print(f"  {d['artist']} — {d['name']}: {d['plays']}/{l['plays']} просл.{mark}")
    n_lift = sum(1 for _, _, lift in plan if lift)
    print(f"\nисторию надо дотянуть у {n_lift} из {len(plan)}")
    if not apply:
        print("это разбор без изменений. Чтобы применить: --retire-originals --apply")
        return

    done = 0
    for d, l, lift in plan:
        sets = "".join(
            f'\n  set {p} of k to {v}' for p, v in (
                ("played count", lift.get("plays")), ("skipped count", lift.get("skips")),
                ("rating", lift.get("rating")),
                ("favorited", "true" if lift.get("fav") else None)) if v)
        # The date has to be bound to a variable: Music refuses the nested
        # specifier form of this assignment.
        pd = ('\n  set pdt to played date of x\n  set played date of k to pdt' if lift else "")
        scr2 = f'''tell application "Music"
  set k to (first track of library playlist 1 whose persistent ID is "{l['pid']}")
  set x to (first track of library playlist 1 whose persistent ID is "{d['pid']}"){sets}
  try{pd}
  end try
  delete x
  return (count of (every track of library playlist 1 whose persistent ID is "{d['pid']}"))
end tell'''
        r2 = subprocess.run(["osascript", "-e", scr2], capture_output=True, text=True, timeout=300)
        if r2.returncode:
            print(f"ОШИБКА на {d['artist']} — {d['name']}: {r2.stderr.strip()}")
        elif r2.stdout.strip() != "0":
            print(f"НЕ УДАЛЕНО: {d['artist']} — {d['name']} (осталось {r2.stdout.strip()})")
        else:
            done += 1
    print(f"убрано мёртвых записей: {done} из {len(plan)}")


def dedupe(apply=False):
    """Collapse library rows that are the same recording listed more than once.

    Not this tool's doing, and worth saying so plainly: Music re-downloads a
    subscription track it already has, writes `Track 1.movpkg` beside the
    original, and registers it as a *separate* row that then accumulates its own
    play count. One song was here three times over, carrying 25, 8 and 1 plays
    of the same 34 real listens.

    Everything is read in a single AppleScript call, because `track i of library
    playlist 1` addresses a different song in the next invocation -- joining two
    dumps by index invents rows that were never there. Only `persistent ID`
    survives between calls, so the second pass is keyed on that.

    The grouping deliberately includes duration and track number. Title, artist
    and album alone are not enough: Apple relabels entries, and two genuinely
    different songs can end up sharing all three."""
    scr = '''tell application "Music"
  set src to library playlist 1
  set S to (ASCII character 1)
  set B to (ASCII character 2)
  set AppleScript's text item delimiters to S
  set r to ((get name of every track of src) as text) & B
  set r to r & ((get artist of every track of src) as text) & B
  set r to r & ((get album of every track of src) as text) & B
  set r to r & ((get cloud status of every track of src) as text) & B
  set r to r & ((get persistent ID of every track of src) as text) & B
  set r to r & ((get track number of every track of src) as text) & B
  set r to r & ((get time of every track of src) as text) & B
  set r to r & ((get played count of every track of src) as text) & B
  set cl to {}
  repeat with c in (get comment of every track of src)
    try
      set end of cl to (paragraph 1 of ((c as text) & return))
    on error
      set end of cl to ""
    end try
  end repeat
  set r to r & (cl as text)
  return r
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=300)
    if r.returncode:
        print("не смог прочитать библиотеку:", r.stderr.strip()); return
    cols = [b.split("\x01") for b in r.stdout.rstrip("\n").split("\x02")]
    if len({len(c) for c in cols}) != 1:
        print("столбцы разошлись — читать нельзя:", [len(c) for c in cols]); return
    keys = ["name", "artist", "album", "status", "pid", "tn", "time", "plays", "comment"]
    lib = [dict(zip(keys, [c[i] for c in cols])) for i in range(len(cols[0]))]

    def secs(t):
        p = [x for x in t.split(":") if x.isdigit()]
        return sum(int(v) * 60 ** i for i, v in enumerate(reversed(p))) if p else -1

    marks = (MARK,) + tuple(OLD_MARKS)
    for t in lib:
        t["ours"] = t["comment"].startswith(marks)

    groups = {}
    for t in lib:
        k = (norm(t["name"]), norm(t["artist"]), norm(t["album"]), t["tn"], secs(t["time"]))
        groups.setdefault(k, []).append(t)
    # A dead original beside its replacement is the expected shape of this
    # project's work, not a duplicate -- parking already handles those.
    dups = [v for k, v in groups.items()
            if len(v) > 1 and k[4] >= 0
            and not any(x["status"] == "no longer available" for x in v)]

    # Our imports need a looser key. A download is never the same length to
    # the second as the store copy, and Apple re-credits artists and re-files
    # albums, so the strict key above missed `Потанцуем-помолчим` outright:
    # 241.6s live, 238.6s imported, artist `AIGEL` against `АИГЕЛ`. Same folded
    # title, a shared artist token and a length within a few seconds is enough
    # here, because one side carries our marker -- this is never a guess about
    # two strangers' rows.
    seen = {x["pid"] for v in dups for x in v}
    live = {}
    for t in lib:
        if not t["ours"] and t["status"] != "no longer available":
            live.setdefault(norm(t["name"]), []).append(t)
    for t in lib:
        if not t["ours"] or t["pid"] in seen:
            continue
        mates = [m for m in live.get(norm(t["name"]), [])
                 if m["pid"] not in seen
                 and (tokens(m["artist"]) & tokens(t["artist"])) - _ARTIST_STOP
                 and secs(m["time"]) >= 0 and abs(secs(m["time"]) - secs(t["time"])) <= DUR_TOLERANCE + 2]
        if mates:
            g = mates + [t]
            seen.update(x["pid"] for x in g)
            dups.append(g)
    if not dups:
        print("дублей не найдено"); return
    print(f"групп-дублей: {len(dups)}, лишних записей: {sum(len(v) - 1 for v in dups)}")

    # Second pass keyed on persistent ID, never on index. `date added` decides
    # which row survives: it is the one field no re-import can restore.
    pids = [x["pid"] for v in dups for x in v]
    lst = ", ".join(f'"{p}"' for p in pids)
    scr2 = f'''
on dt(d)
  try
    return ((year of d) as text) & "." & ((month of d as integer) as text) & "." & ¬
           ((day of d) as text) & "." & ((hours of d) as text) & "." & ((minutes of d) as text)
  on error
    return ""
  end try
end dt

tell application "Music"
  set o to {{}}
  repeat with p in {{{lst}}}
    set t to (first track of library playlist 1 whose persistent ID is (p as text))
    set pls to {{}}
    repeat with pl in (every user playlist)
      try
        if (smart of pl) is false and (special kind of pl) is none then
          if (count of (every track of pl whose persistent ID is (p as text))) > 0 then
            set end of pls to (name of pl)
          end if
        end if
      end try
    end repeat
    set AppleScript's text item delimiters to "|"
    set plt to pls as text
    set AppleScript's text item delimiters to tab
    set end of o to ((p as text) & tab & my dt(date added of t) & tab & my dt(played date of t) & tab & ¬
      ((skipped count of t) as text) & tab & ((favorited of t) as text) & tab & plt)
  end repeat
  set AppleScript's text item delimiters to linefeed
  return o as text
end tell'''
    r2 = subprocess.run(["osascript", "-e", scr2], capture_output=True, text=True, timeout=600)
    if r2.returncode:
        print("не смог прочитать подробности:", r2.stderr.strip()); return
    det = {}
    for line in r2.stdout.split("\n"):
        p = line.split("\t")
        if len(p) >= 6:
            det[p[0]] = {"added": p[1], "played": p[2], "skips": p[3],
                         "fav": p[4] == "true", "playlists": [x for x in p[5].split("|") if x]}

    def when(s):
        try:
            return [int(x) for x in s.split(".")]
        except Exception:
            return [9999]

    # What the index says each of our imports was GIVEN at import time --
    # `apply_export_history` copies the dead entry's count onto it.
    idx_plays = {}
    if LIST.exists():
        for row in csv.DictReader(LIST.open(), delimiter="\t"):
            try:
                idx_plays[(row["Name"], row["Artist"])] = int(row.get("Plays") or 0)
            except ValueError:
                pass

    def num(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0

    plan = []
    for v in dups:
        theirs = [x for x in v if not x["ours"]] or v
        theirs.sort(key=lambda x: when(det.get(x["pid"], {}).get("added", "")))
        keep = theirs[0]
        drop = [x for x in v if x is not keep]
        d0 = det.get(keep["pid"], {})
        # Music's own re-downloads: a SUM. Every row counted listens the others
        # did not, and the raise-only rule elsewhere exists for reruns, not this.
        plays = sum(num(x["plays"]) for x in theirs)
        skips = sum(num(det.get(x["pid"], {}).get("skips")) for x in theirs)
        # Our imports: only what was played ON them counts, because the rest is
        # a copy of the dead entry's history that the live copy already carries.
        # Measured 2026-10-16: `AIGEL :: Пыяла` live 2, ours 2, index 2 -- the
        # plain sum this used to take proposed 4 for two real listens. With no
        # index row the gain is unknowable, so the larger count wins rather
        # than inventing listens.
        for x in v:
            if x["ours"] and x is not keep:
                given = idx_plays.get((x["name"], x["artist"]))
                gain = num(x["plays"]) - given if given is not None else 0
                # And never below what the index holds: `Meek Mill :: Im a Boss`
                # had the user's own upload at 23 beside our import at 24, the
                # index's figure -- adding the gain to 23 lost a listen.
                plays = (max(plays, given) + max(gain, 0)) if given is not None \
                    else max(plays, num(x["plays"]))
        last = max((det.get(x["pid"], {}).get("played") or "" for x in v), key=when)
        last_from = next((x["pid"] for x in v if (det.get(x["pid"], {}).get("played") or "") == last), None)
        fav = any(det.get(x["pid"], {}).get("fav") for x in v)
        # Smart playlists re-evaluate; a plain one simply loses the song.
        # Not this project's own playlists: `refetched` holds OUR files, and a
        # store copy kept in place of one does not belong there.
        orphaned = sorted({p for x in drop for p in det.get(x["pid"], {}).get("playlists", [])}
                          - set(d0.get("playlists", []))
                          - {REFETCHED_PLAYLIST, REPLACED_PLAYLIST})
        plan.append((keep, drop, plays, skips, last, fav, orphaned, last_from))
        print(f"\n{keep['artist']} — {keep['name']} [{keep['time']}]")
        print(f"    оставляю: добавлен {d0.get('added','?')}, {keep['plays']} просл.")
        for x in drop:
            print(f"    удаляю:   добавлен {det.get(x['pid'],{}).get('added','?')}, {x['plays']} просл.")
        print(f"    итог: {plays} просл., {skips} проп." + (f", вернуть в: {', '.join(orphaned)}" if orphaned else ""))

    if not apply:
        print("\nэто разбор без изменений. Чтобы применить: --dedupe --apply")
        return

    for keep, drop, plays, skips, last, fav, orphaned, last_from in plan:
        adds = "\n".join(f'  duplicate k to user playlist "{p}"' for p in orphaned)
        dels = "\n".join(
            f'  delete (first track of library playlist 1 whose persistent ID is "{x["pid"]}")'
            for x in drop)
        # `set played date of a to played date of b` raises -10006; the date has
        # to be bound to a variable first. Same shape as every other nested
        # specifier Music refuses.
        # From the row that actually holds the latest date -- this used to take
        # it from whichever row happened to be dropped first.
        pd = (f'  set d to played date of (first track of library playlist 1 '
              f'whose persistent ID is "{last_from}")\n'
              f'  set played date of k to d\n') if last and last_from and last_from != keep["pid"] else ""
        scr3 = f'''tell application "Music"
  set k to (first track of library playlist 1 whose persistent ID is "{keep['pid']}")
  set played count of k to {plays}
  set skipped count of k to {skips}
  set favorited of k to {"true" if fav else "false"}
{pd}{adds}
{dels}
  return (count of (every track of library playlist 1 whose persistent ID is "{keep['pid']}"))
end tell'''
        r3 = subprocess.run(["osascript", "-e", scr3], capture_output=True, text=True, timeout=300)
        if r3.returncode:
            print(f"ОШИБКА на {keep['artist']} — {keep['name']}: {r3.stderr.strip()}")
        else:
            print(f"готово: {keep['artist']} — {keep['name']} ({plays} просл., осталось записей {r3.stdout.strip()})")


def repair(rows, state):
    """Patch already-imported tracks in place: artwork, history, comment.

    A normal rerun would delete and re-import, which resets `date added` a second
    time and throws away restored history. This touches only library metadata,
    so it is the right tool for tracks imported by an older version of this
    script -- it is not a capability the main path lacks."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    exp = {(r["Name"], r["Artist"]): r for r in rows}

    out = subprocess.run(["osascript", "-e", f'''
tell application "Music"
  set o to ""
  repeat with t in (every track of user playlist "{REFETCHED_PLAYLIST}")
    set o to o & (name of t) & tab & (artist of t) & tab & (count of artworks of t) & linefeed
  end repeat
  return o
end tell'''], capture_output=True, text=True, timeout=180).stdout

    art_fixed = hist_fixed = cmt_fixed = missed = 0
    unlisted = []
    for line in out.split("\n"):
        p = line.split("\t")
        if len(p) < 3:
            continue
        name, artist, nart = p[0], p[1], int(p[2])
        row = exp.get((name, artist))
        if not row:
            # Never a bare continue: this is the path that quietly did nothing
            # for 24 of 28 tracks while printing a healthy summary.
            unlisted.append(f"{name} — {artist}")
            continue
        rec = state.get(f"{artist} :: {name}", {})

        if nart == 0:
            thumb, src_title = thumb_and_title(rec.get("url"))
            art, src = get_art(row, thumb, rec.get("uploader") or "",
                               trusted_thumb=rec.get("src") == "album",
                               hint=rec.get("cand") or src_title)
            if art:
                r = subprocess.run(["osascript", "-e", f'''
tell application "Music"
  set t to (first file track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}")
  set data of artwork 1 of t to (read (POSIX file "{art}") as picture)
end tell'''], capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    art_fixed += 1
                    # Keep state truthful: --report reads it, and an artwork set
                    # here would otherwise still be filed as "none" forever.
                    if rec:
                        rec["art"] = src
                    print(f"  обложка  {name} — {artist}  ({src})")
                else:
                    missed += 1
            else:
                missed += 1
                print(f"  нет арта {name} — {artist}")

        if inherit_history(name, artist, row.get("Album", "")) not in ("0", ""):
            hist_fixed += 1
        if apply_export_history(row) not in ("0", ""):
            hist_fixed += 1

        cmt = build_comment(row, rec.get("url") or "")
        r = subprocess.run(["osascript", "-e", f'''
tell application "Music"
  set t to (first file track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}")
  set (comment of t) to "{esc(cmt)}"
end tell'''], capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            cmt_fixed += 1

    save_state(state)
    if unlisted:
        print(f"\nНЕ ТРОНУТО — нет строки в {LIST.name}: {len(unlisted)}")
        for u in unlisted:
            print(f"   {u}")
        print("   (обнови индекс: --refresh-index)")
    print(f"\nобложек проставлено: {art_fixed} (не нашлось: {missed})")
    print(f"история подтянута: {hist_fixed}")
    print(f"комментариев записано: {cmt_fixed}")


def review(rows, approve=None):
    """Show what is parked, with the evidence a person needs, and record verdicts.

    An approval is written to track_sources.json rather than acted on directly:
    a decision that lives only in a run is lost the next time, and this project
    has already lost a track that way."""
    st = load_state()
    idx = {(r["Name"], r["Artist"]): r for r in rows}
    items = sorted(((k, v) for k, v in st.items() if v.get("status") == "review"),
                   key=lambda kv: -(kv[1].get("score") or 0))
    if approve:
        try:
            src = json.loads(TRACK_SOURCES.read_text()) if TRACK_SOURCES.exists() else {}
        except Exception:
            src = {}
        picked = []
        for n in approve:
            if 1 <= n <= len(items):
                k, v = items[n - 1]
                src[k] = v.get("url")
                # Mark rather than delete. Removing the row leaves nothing for
                # --retry to select, so the approval reported success and then
                # nothing happened at all.
                st[k] = {"status": "approved", "url": v.get("url")}
                picked.append(k)
        TRACK_SOURCES.write_text(json.dumps(src, ensure_ascii=False, indent=2))
        save_state(st)
        print(f"одобрено и записано в track_sources.json: {len(picked)}")
        for k in picked:
            print(f"   {k}")
        print("теперь: python3 geo-unblock.py --retry approved")
        return

    print(f"\nВ РЕВЬЮ: {len(items)}\n")
    for i, (k, v) in enumerate(items, 1):
        a, n = k.split(" :: ", 1)
        want = v.get("want") or (idx.get((n, a), {}) or {}).get("Time") or 0
        dur = v.get("cand_dur")
        delta = f"{float(dur) - float(want):+.0f}s" if dur else "?"
        print(f"{i:2}. {n} — {a}")
        print(f"    хотим {want}s | кандидат {dur or '?'}s ({delta}) | score {v.get('score', 0):.0f} | {v.get('src', '?')}")
        print(f"    «{v.get('cand')}» — {v.get('uploader')}")
        print(f"    {v.get('url')}")
        # The measurement, when there is one: a candidate the audio already
        # rejected is not a judgement call, and approving it overrules evidence.
        au = v.get("audio")
        if au:
            print(f"    звук: волна {au.get('wave')} при фоне {au.get('bg')} — "
                  + ("та же запись" if au.get("same") else "ДРУГАЯ запись, одобрять незачем"))
        elif v.get("why"):
            print(f"    {v['why']}")
    print("\nодобрить:  python3 geo-unblock.py --review --approve 3,11,12")


def report(rows):
    """Human-readable summary of the run so far, read from state."""
    st = load_state()
    if not st:
        print("состояние пусто — прогонов ещё не было"); return
    order = {f"{r['Artist']} :: {r['Name']}": i for i, r in enumerate(rows)}
    by_status = {}
    for k, v in st.items():
        by_status.setdefault(v.get("status", "?"), []).append((k, v))

    total = len(st)
    ok = by_status.get("ok", [])
    print(f"\nОБРАБОТАНО: {total}")
    for name, label in (("ok", "заменено"), ("already_live", "замена не нужна"),
                        ("review", "в ревью"), ("not_found", "не найдено"),
                        ("error", "ошибка")):
        if by_status.get(name):
            print(f"  {label:18} {len(by_status[name])}")

    def tally(field, title):
        counts = {}
        for _, v in ok:
            counts[v.get(field) or "—"] = counts.get(v.get(field) or "—", 0) + 1
        if counts:
            print(f"\n{title}: " + ", ".join(
                f"{k} {n}" for k, n in sorted(counts.items(), key=lambda x: -x[1])))
    tally("src", "источник звука")

    # Artwork and bitrate are read back out of Music rather than from state.
    # State records what an import intended; a later repair changes the library
    # without touching it, and reporting the intention would be a lie.
    live = subprocess.run(["osascript", "-e", f'''
tell application "Music"
  set o to ""
  repeat with t in (every track of user playlist "{REFETCHED_PLAYLIST}")
    set o to o & (count of artworks of t) & tab & (bit rate of t) & linefeed
  end repeat
  return o
end tell'''], capture_output=True, text=True, timeout=180).stdout
    arts = [x.split("\t") for x in live.split("\n") if "\t" in x]
    if arts:
        with_art = sum(1 for a in arts if a[0] != "0")
        rates = sorted(int(a[1]) for a in arts if a[1].isdigit())
        print(f"\nв библиотеке: {len(arts)} треков, с обложкой {with_art}")
        if rates:
            print(f"битрейт: медиана {rates[len(rates)//2]}k, "
                  f"минимум {rates[0]}k, максимум {rates[-1]}k")

    secs = sorted(v["secs"] for v in st.values() if v.get("secs"))
    if secs:
        print(f"\nвремя на трек: медиана {secs[len(secs)//2]:.0f}с, "
              f"максимум {max(secs):.0f}с, суммарно {sum(secs)/60:.0f} мин")

    for name, label in (("review", "В РЕВЬЮ (не импортированы)"),
                        ("not_found", "НЕ НАЙДЕНО"), ("error", "ОШИБКИ")):
        items = sorted(by_status.get(name, []), key=lambda kv: order.get(kv[0], 9999))
        if not items:
            continue
        print(f"\n{label}:")
        for k, v in items:
            extra = v.get("cand") or v.get("error") or ""
            print(f"  {k}" + (f"\n      кандидат: {extra[:70]}" if extra else ""))
            rel = v.get("released") or ""
            if rel.startswith("store:"):
                print(f"      ЕСТЬ В APPLE MUSIC — добавь, историю перенесу: {rel[6:]}")
            elif rel.startswith("maybe:"):
                print(f"      возможно это оно, но артист записан иначе — проверь:"
                      f"\n      {rel[6:]}")
            elif rel.startswith("foreign:"):
                print(f"      есть в Apple Music, но в другой витрине — не в твоей:"
                      f"\n      {rel[8:]}")
            elif rel.startswith("deezer:"):
                print(f"      издан, но ни в Apple Music, ни у источников: {rel[7:]}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--no-soundcloud", action="store_true",
                    help="YouTube only; skip the SoundCloud fallback")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None, metavar="yt,sc,mr,mp,ytm,sl",
                    help="ask only these providers, in this order")
    ap.add_argument("--audition", action="store_true",
                    help="download and judge candidates against the reference, "
                         "import nothing and write nothing -- not even state")
    ap.add_argument("--track", action="append", default=None, metavar="'ARTIST :: NAME'",
                    help="process only this row (repeatable); with --audition, "
                         "also rows already done")
    ap.add_argument("--min-score", type=float, default=120,
                    help="below this a match is parked for review, not imported")
    ap.add_argument("--retry", metavar="STATUS", default=None,
                    help="process only rows already recorded with this status "
                         "(error, review, not_found); far cheaper than a full walk")
    ap.add_argument("--fingerprint", type=int, default=None, metavar="N",
                    help="keep a ~60KB preview fingerprint for up to N still-playing tracks")
    ap.add_argument("--mirror", type=int, default=None, metavar="N",
                    help="archive verified copies of up to N still-playing tracks (most-played first)")
    ap.add_argument("--catalog", action="store_true",
                    help="snapshot the WHOLE library (history + persistent IDs) into data/catalog.tsv")
    ap.add_argument("--snapshot", action="store_true",
                    help="fold favourites and counts of already-replaced tracks "
                         "back into the index; run before anything destructive")
    ap.add_argument("--refresh-index", action="store_true",
                    help="rebuild the track list from Music (no manual export needed)")
    ap.add_argument("--playlist", default=None,
                    help="narrow the scan to a playlist; default is every track "
                         "Music reports as no longer available")
    ap.add_argument("--review", action="store_true",
                    help="list parked candidates with the evidence to judge them")
    ap.add_argument("--approve", default=None,
                    help="comma-separated numbers from --review to accept")
    ap.add_argument("--report", action="store_true",
                    help="print a summary of the run so far and exit")
    ap.add_argument("--repair", action="store_true",
                    help="patch already-imported tracks in place instead of fetching")
    ap.add_argument("--dedupe", action="store_true",
                    help="find library rows that are the same recording twice over")
    ap.add_argument("--retire-originals", action="store_true",
                    help="delete dead entries whose replacement already carries their history")
    ap.add_argument("--apply", action="store_true",
                    help="with --dedupe: merge the history and delete the extra rows")
    a = ap.parse_args()

    # YouTube first: Topic channels carry label-delivered audio. SoundCloud next,
    # then Mail.Ru -- a user-upload catalogue that is often the only place old and
    # obscure Russian material still exists.
    # mp3party after Mail.ru: the same kind of catalogue, one request a query,
    # and ytm after them because it is the slow one. Soulseek is slower still --
    # a login and twelve seconds a query -- and holds what the others dropped.
    providers = ["yt"] if a.no_soundcloud else ["yt", "sc", "mr", "mp", "ytm", "sl"]
    if a.only:
        providers = a.only.split(",")
    DEST.mkdir(parents=True, exist_ok=True)
    # Only when something is actually going to be imported. Creating playlists
    # was unconditional, so `--report` -- a command whose whole job is to look
    # and not touch -- silently rebuilt a playlist the user had just deleted on
    # purpose. A read-only command must leave the library exactly as it found it.
    if not (a.report or a.review or a.approve or a.dedupe or a.retire_originals
            or a.snapshot or a.catalog or a.mirror is not None or a.fingerprint is not None or a.dry_run or a.audition):
        ensure_playlists()
    # `--audition` must leave both the library and the record as they were: it
    # exists to be run on a track that is already done, to see what the tool
    # would do now.
    save = (lambda _st: None) if a.audition else save_state
    st = load_state()
    # Reads the library, not the index -- it is about Music's own bookkeeping,
    # so it must run before anything insists a track list exists.
    if a.fingerprint is not None:
        fingerprint(a.fingerprint)
        return
    if a.mirror is not None:
        mirror(a.mirror, providers, a.min_score)
        return
    if a.catalog:
        live, total = catalog()
        print(f"в снимке библиотеки: {live} треков сейчас, {total} всего за всю историю снимков")
        return
    if a.dedupe:
        dedupe(a.apply)
        return
    if a.retire_originals:
        retire_originals(a.apply)
        return
    if a.refresh_index or not LIST.exists():
        why = "по запросу" if a.refresh_index else "списка ещё нет"
        print(f"читаю треки из Музыки ({why})…", flush=True)
        n = build_index(a.playlist)
        print(f"найдено недоступных треков: {n}")
        if a.refresh_index and not (a.repair or a.report):
            return

    rows = [r for r in csv.DictReader(LIST.open(), delimiter="\t") if r.get("Name")]

    if a.snapshot:
        print(f"обновлено полей в индексе: {snapshot_refetched()}")
        return

    if a.review or a.approve:
        nums = [int(x) for x in a.approve.split(",")] if a.approve else None
        review(rows, nums)
        return

    if a.report:
        report(rows)
        return

    if a.repair:
        repair(rows, st)
        return

    if a.track:
        want_keys = set(a.track)
        batch = [r for r in rows if f"{r['Artist']} :: {r['Name']}" in want_keys]
        missing = want_keys - {f"{r['Artist']} :: {r['Name']}" for r in batch}
        if missing:
            print(f"нет в индексе: {sorted(missing)}")
    elif a.retry:
        # A full walk re-runs every already_live probe and re-searches every
        # parked candidate -- minutes of work to reach a handful of rows. Select
        # them directly instead.
        wanted = {s.strip() for s in a.retry.split(",")}
        batch = [r for r in rows
                 if st.get(f"{r['Artist']} :: {r['Name']}", {}).get("status") in wanted]
        # Deliberately NOT clearing the selected rows first. Doing that made the
        # selection itself destructive: a crash mid-batch left the rows with no
        # status at all, so the next --retry matched nothing, printed "выбрано
        # строк: 0" and exited 0 -- indistinguishable from "there was nothing to
        # do". The loop overwrites each row's status as it finishes anyway.
        print(f"выбрано строк со статусом {sorted(wanted)}: {len(batch)}")
    else:
        batch = rows[a.start:a.start + a.limit]

    for i, row in enumerate(batch, start=(0 if a.retry or a.track else a.start)):
        key = f"{row['Artist']} :: {row['Name']}"
        if st.get(key, {}).get("status") == "ok" and not (a.audition and a.track):
            print(f"[{i}] skip (done): {key}"); continue
        # Duration is the load-bearing check for everything downstream, so a row
        # without one cannot be processed -- and must not take the whole run with
        # it. One malformed row killed two full passes at exactly the same track,
        # silently the first time, because the traceback went to a lost stream.
        raw_time = (row.get("Time") or "").strip()
        if not raw_time.isdigit():
            st[key] = {"status": "no_duration", "note": f"Time={row.get('Time')!r}"}
            print(f"[{i}] {key}\n      НЕТ ДЛИТЕЛЬНОСТИ в индексе — пропускаю")
            save(st)
            continue
        want = int(raw_time)
        t0 = time.monotonic()
        print(f"[{i}] {key}  ({want}s)", flush=True)

        if not (a.dry_run or a.audition) and already_live(row["Name"], row["Artist"]):
            # Nothing to import, but there is still something to save. The dead
            # entry holds the plays and the favourite; the copy the user swapped
            # in by hand may hold neither, and the moment the dead one is
            # deleted that history is gone. Skipping the import must not mean
            # skipping the inheritance -- two tracks reached the delete with
            # their record still only on the corpse, one of them carrying
            # twelve plays and a heart.
            st[key] = {"status": "already_live", "want": want, "secs": round(time.monotonic()-t0,1)}
            try:
                moved = inherit_history(row["Name"], row["Artist"], row.get("Album", ""))
                note = f" (история: {moved})" if moved else ""
            except Exception as ex:
                # Loudly. A history merge that fails quietly here is invisible
                # until the dead entry is deleted, and then it is unrecoverable.
                st[key]["history_error"] = str(ex)[:200]
                note = f"  ⚠ история НЕ перенесена: {str(ex)[:120]}"
            print("      УЖЕ ЕСТЬ живая копия — пропускаю" + note)
            save(st); continue

        # A named release beats any search: it is the actual record, not a guess
        # about which upload corresponds to it.
        vouched = (pick_from_track_source(row) or pick_from_archive(row)
                   or pick_from_album(row, want, a.min_score))
        cands = [vouched] if vouched else pick(
            row["Name"], row["Artist"], want, providers, row.get("Album", ""), a.min_score)

        # Fetched at most once per row, and only when a downloaded candidate
        # needs judging -- the sources behind it cost up to a minute together.
        _ref = {}

        def ref(row=row, want=want, _ref=_ref):
            if "r" not in _ref:
                _ref["r"] = reference(row, want)
            return _ref["r"]
        if not cands:
            st[key] = {"status": "not_found", "want": want, "secs": round(time.monotonic()-t0,1)}
            where, link = released_where(row, want)
            if where:
                st[key]["released"] = f"{where}:{link}"
                print("      НЕ НАЙДЕНО у источников. " + (
                    f"Но он есть в Apple Music: {link}\n      добавь — историю перенесу"
                    if where == "store" else
                    f"Возможно это оно, артист записан иначе — проверь:\n      {link}"
                    if where == "maybe" else
                    f"Он есть в Apple Music, но в ДРУГОЙ витрине — в твоей его нет,\n"
                    f"      добавить нечего: {link}"
                    if where == "foreign" else
                    f"Издан ({link}), но в Apple Music его нет —\n"
                    f"      добавить нечего, ждём, пока где-нибудь всплывёт копия"))
            else:
                print("      NOT FOUND")
            save(st); continue

        # Try candidates in order. A source can rank first and still fail to
        # deliver -- Mail.Ru's http file URLs 404'd for the whole project, which
        # alone cost twelve tracks in one pass while usable YouTube candidates
        # sat unused in the same result set. That one was ours to fix and is
        # fixed; the shape of the failure is not rare, so the loop stays.
        handled = False
        aliased = False
        tried = set()
        # A named release is the answer, not a guess to be widened past.
        # An archived copy that fails its check must not end the search.
        widened = bool(vouched) and vouched[2] != "archive"
        while True:
            usable = [c for c in cands if not out_of_play(c[2])]
            skipped = len(cands) - len(usable)
            if skipped:
                print(f"      пропускаю {skipped} кандидатов от источников, "
                      f"отказавших в этом прогоне")
            for attempt, (sc, e, prov) in enumerate(usable[:4]):
                if attempt:
                    print(f"      пробую следующего кандидата ({attempt+1} из {min(len(usable),4)})")
                url = e.get("url") or e.get("webpage_url")
                tried.add(url)
                print(f"      -> [{prov} {sc:.0f}] {e.get('uploader')} | {e.get('title')} | {e.get('duration')}s")
                # A candidate rejected only for being a few seconds long may simply carry
                # padded silence. That is cheap to settle with evidence: fetch it and
                # measure the sound itself. Anything still off is a different cut.
                prefetched = None
                wrong_cut = False
                off = abs((e.get("duration") or 0) - want)
                # Run this for every wide candidate headed for review, not only ones that
                # nearly cleared the bar: the whole point is that the score cannot decide
                # these, so gating the evidence on the score is circular.
                if (not a.dry_run and prov != "manual"
                        and DUR_TOLERANCE < off <= DUR_LONG_MAX and sc < a.min_score + 60):
                    try:
                        cand = fetch(url, str(DEST / safe(f"{row['Artist']} - {row['Name']}")))
                        eff = effective_duration(cand)
                        if eff and abs(eff - want) <= DUR_TOLERANCE:
                            # The real gap is now known, so rescore on it rather than
                            # patching the penalty back off an estimate.
                            sc = score({**e, "duration": eff}, row["Name"], row["Artist"],
                                       want, provider=prov)
                            print(f"      тишина по краям: чистого звука {eff:.0f}s "
                                  f"(Δ{eff - want:+.0f}s) — принято, score {sc:.0f}")
                            prefetched = cand
                        else:
                            shown = f"{eff:.0f}s (Δ{eff - want:+.0f}s)" if eff else "не измерился"
                            R = ref()
                            # Longer than the release is not by itself a different
                            # cut: a release can BE a cut of a longer piece, and an
                            # Apple preview says where -- see `apple_window`.
                            if eff and eff > want and R and R.get("window") is not None and R.get("dur"):
                                print(f"      чистого звука {shown} — длиннее релиза, "
                                      f"эталон покажет, где он внутри")
                                prefetched = cand
                            else:
                                print(f"      чистого звука {shown} — другой монтаж")
                                cand.unlink(missing_ok=True)
                                wrong_cut = True
                            # Text must never outvote the audio: a candidate the
                            # measurement rejected cannot stay above the import bar.
                            sc = min(sc, a.min_score - 1)
                    except Exception as ex:
                        print(f"      проверка тишины не удалась: {str(ex)[:60]}")

                # Below this, the match is a guess -- a live cut, a re-upload, a lookalike.
                # Park it for human review instead of quietly polluting the library --
                # UNLESS there is a reference to ask. A pseudonym release (`Spirit`
                # for `The Imperas`) and a translated title (`Capitán Jack
                # Sparrow`) fail the text checks by construction and are exactly
                # right; the audio can say so where no title can, so they are
                # downloaded and judged by it instead of handed to a person.
                low = sc < a.min_score
                if low and wrong_cut and ref():
                    # Already downloaded and measured: the length is wrong and
                    # nothing says where a release would sit inside it. The
                    # audio cannot rescue that, so do not fetch it twice.
                    st[key] = {"status": "review", "url": url, "score": sc, "cand": e.get("title"),
                               "uploader": e.get("uploader"), "want": want,
                               "cand_dur": e.get("duration"), "src": prov,
                               "why": "другой монтаж: длина не та, обрезать не по чему",
                               "secs": round(time.monotonic()-t0,1)}
                    continue
                if low and not a.dry_run and prov != "manual" and ref():
                    print(f"      ниже порога ({sc:.0f}) — решит звук, сверяю с эталоном")
                elif low:
                    st[key] = {"status": "review", "url": url, "score": sc, "cand": e.get("title"),
                               "uploader": e.get("uploader"), "want": want,
                               "cand_dur": e.get("duration"), "src": prov,
                               "secs": round(time.monotonic()-t0,1)}
                    # Candidates are sorted by score, so anything after this one is
                    # worse -- there is nothing better left to try.
                    print("      LOW CONFIDENCE -> review")
                    # Also here, not only in the not_found branch: a track with a
                    # bad candidate has been given up on just as completely as one
                    # with none, and `Сатана Печёт Блины :: Sigma` proved it --
                    # the only thing any source offered was a cover, so it parked
                    # for review while the real release sat published on Deezer.
                    where, link = released_where(row, want)
                    if where:
                        st[key]["released"] = f"{where}:{link}"
                        print(f"      но он есть в Apple Music: {link}\n"
                              f"      добавь его — историю перенесу" if where == "store" else
                              f"      возможно это оно, артист записан иначе — проверь:\n"
                              f"      {link}" if where == "maybe" else
                              f"      издан ({link}), но в Apple Music его нет")
                    save(st); break
                if a.dry_run:
                    st[key] = {"status": "dry", "url": url, "score": sc,
                               "cand": e.get("title"), "uploader": e.get("uploader")}
                    save(st); continue

                try:
                    f = prefetched or fetch(url, str(DEST / safe(f"{row['Artist']} - {row['Name']}")))
                    if not f:
                        raise RuntimeError("no output file")
                    # Measure what was DELIVERED. Every duration decision before
                    # this point was made on metadata the source typed, and the
                    # silence check ran only when the ADVERTISED length already
                    # looked wrong -- which is precisely when it is not lying.
                    # Mail.ru is a user-upload catalogue: one entry advertised
                    # 121s, matched the library to the second, scored 142, and
                    # delivered a 181KB 12kbps file that came out as 4.4 seconds
                    # of audio. It imported, and only the user noticed.
                    got = effective_duration(f)
                    if got is None:
                        f.unlink(missing_ok=True)
                        raise RuntimeError("длительность скачанного файла не измерилась")
                    # Ask the reference, when there is one. Before the length
                    # check, because the answer can CHANGE the length: a match
                    # that sits inside a longer file says where the release is.
                    R = ref()
                    verdict = compare_audio(R["path"], f) if R else None
                    if verdict:
                        print(f"      звук против эталона ({R['via']}): волна {verdict['wave']:.2f} "
                              f"при фоне {verdict['bg']:.2f}, огибающая {verdict['env']:.2f}, "
                              f"эталон с {verdict['lag']:.1f}s — "
                              + ("та же запись" if verdict["same"] else "ДРУГАЯ запись"), flush=True)
                    # Before the length check: "a different recording" says more
                    # than "the wrong length", and it is what review should show.
                    if verdict and not verdict["same"]:
                        if prov == "manual":
                            print("      ⚠ звук расходится с эталоном — беру, потому что "
                                  "источник указан вручную", flush=True)
                        else:
                            f.unlink(missing_ok=True)
                            raise AudioMismatch(f"звук не совпал с эталоном: волна {verdict['wave']:.2f}")
                    trim = e.get("trim")
                    if (not trim and got - want > DUR_TOLERANCE and verdict and verdict["same"]
                            and R.get("window") is not None and R.get("dur")):
                        # Apple's preview starts at a known point of the release,
                        # so where it sits in this file is where the release
                        # starts. Reconstructed this way by hand once, verified
                        # against Apple's own audio: `Creelles Town` is the first
                        # 72.051s of a 120s rip.
                        start = verdict["lag"] - R["window"]
                        if start > -0.5 and start + R["dur"] <= got + 0.5:
                            trim = [round(max(start, 0.0), 3), round(max(start, 0.0) + R["dur"], 3)]
                            print(f"      релиз — отрезок [{trim[0]}:{trim[1]}] этого файла "
                                  f"(превью Apple начинается с {R['window']:.0f}s релиза)")
                    if trim:
                        cut(f, *trim)
                        got = effective_duration(f)
                        if got is None:
                            f.unlink(missing_ok=True)
                            raise RuntimeError("после обрезки длительность не измерилась")
                    if verdict and verdict["same"] and got < want - DUR_TOLERANCE:
                        # Silence is part of the release. `Капитан Джек Воробей`
                        # matched its reference at 1.00 and was still rejected:
                        # 242s of file, 238s once the master's own quiet edges
                        # were stripped. With the audio proven the same, only the
                        # container can be short of the release; a padded-out
                        # fragment like the 4.4s Mail.ru file never gets here,
                        # because it cannot match a 30s reference.
                        got = container_duration(f) or got
                    if abs(got - want) > DUR_TOLERANCE:
                        # A human named this release; say so and take it anyway.
                        if prov == "manual":
                            print(f"      ⚠ по факту {got:.0f}s против {want}s — "
                                  f"беру, потому что источник указан вручную", flush=True)
                        else:
                            f.unlink(missing_ok=True)
                            raise RuntimeError(
                                f"скачано {got:.0f}s вместо {want}s — не та запись")
                    if low and not (verdict and verdict["same"]):
                        f.unlink(missing_ok=True)
                        raise AudioMismatch("эталон есть, но сравнить не удалось — без него ниже порога")
                    src_url = (e.get("source_url") or url) + (f" [{trim[0]}:{trim[1]}]" if trim else "")
                    if a.audition:
                        f.unlink(missing_ok=True)
                        print(f"      ПРОСЛУШКА: импортировал бы{' по звуку' if low else ''} "
                              f"— {src_url} ({got:.1f}s)")
                        handled = True
                        break
                    art_src = tag(f, row,
                                  e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None,
                                  src_url=src_url, uploader=e.get("uploader") or "",
                                  trusted_thumb=bool(e.get("trusted_art")),
                                  hint=e.get("title") or "")
                    # Park BEFORE importing: once our file is added Music may be mid-merge,
                    # and the dead original briefly stops being findable.
                    parked = park_original(row["Name"], row["Artist"])
                    purged = purge_ours(row["Name"], row["Artist"])
                    tid = add_to_music(f)
                    add_to_playlist(tid, REFETCHED_PLAYLIST)
                    # Only matters when the original did NOT merge; a merge already keeps
                    # the history, and inherit_history finds no dead entry to copy from.
                    inherit_history(row["Name"], row["Artist"], row.get("Album", ""))
                    plays = apply_export_history(row)
                    # Music copies imports into its own Media folder, so the staged file is
                    # now a second copy of the same audio. Drop it.
                    f.unlink(missing_ok=True)
                    st[key] = {"status": "ok", "url": url, "src": prov, "score": sc, "art": art_src,
                               "track": tid, "parked": parked, "purged": purged,
                               "plays": plays, "uploader": e.get("uploader"),
                               "cand": e.get("title"),
                               "secs": round(time.monotonic()-t0,1)}
                    if verdict:
                        st[key]["audio"] = {**verdict, "ref": R["via"], "ref_page": R.get("page")}
                    if trim:
                        st[key]["trim"] = trim
                    print(f"      ADDED  art={art_src} parked={parked} plays={plays} "
                          f"({time.monotonic()-t0:.0f}s)")
                    handled = True
                    break
                except AudioMismatch as ex:
                    # Parked with the measurement, not filed as an error: the
                    # source delivered, and what it delivered is a different
                    # recording. The next candidate may well be the right one.
                    st[key] = {"status": "review", "url": url, "score": sc, "cand": e.get("title"),
                               "uploader": e.get("uploader"), "want": want,
                               "cand_dur": e.get("duration"), "src": prov, "why": str(ex),
                               "audio": verdict, "secs": round(time.monotonic()-t0,1)}
                    print(f"      ОТВЕРГНУТ  {ex}")
                except Exception as ex:
                    # Record whether a delete already happened. Adjacent is not atomic:
                    # if the import failed after purge_ours removed the previous copy,
                    # the track is gone and a later run must know to re-add rather than
                    # treat this as an ordinary failure.
                    st[key] = {"status": "error", "url": url, "error": str(ex),
                               "purged": locals().get("purged", "?"),
                               "secs": round(time.monotonic()-t0,1)}
                    print(f"      ERROR  {ex}")
                    note_refusal(prov, str(ex))
                    # Deliberately no break: the next candidate is the whole point.
            # `not tried` means nothing was ever downloaded: the best candidate
            # was already below the bar, so pick() never returned early and every
            # provider has been asked. Anything else means the audio is what we
            # failed to get, and the score -- including a LOW CONFIDENCE one
            # reached only because the better candidates would not download -- is
            # the wrong thing to end the search on.
            if handled or not (tried or skipped):
                break
            if not widened:
                # The ranking was never the problem: the providers pick() skipped
                # were simply never asked.
                widened = True
                wider = pick(row["Name"], row["Artist"], want, providers,
                             row.get("Album", ""), a.min_score, exhaustive=True)
                cands = [c for c in wider
                         if (c[1].get("url") or c[1].get("webpage_url")) not in tried]
                if cands:
                    print(f"      ни один кандидат не скачался — добираю остальные "
                          f"источники ({len(cands)} новых)")
                    continue
            # Last round, and only with a reference to judge by: the same
            # recording under another credit. Never past a named source -- that
            # is the answer, not a guess. Every candidate is held below the bar,
            # so nothing found here can import on text: its score was earned
            # against the ALIAS's name, not this row's.
            if aliased or vouched or a.dry_run or not ref():
                break
            aliased = True
            alts = aliases(row, want)
            if not alts:
                break
            print(f"      ищу ту же запись под другими именами: {', '.join(alts)}")
            cands = []
            for alt in alts:
                best = [c for c in pick(base_title(row["Name"]), alt, want, ["yt", "sc", "mr"],
                                        "", a.min_score)
                        if (c[1].get("url") or c[1].get("webpage_url")) not in tried][:1]
                cands += [(min(sc, a.min_score - 1), e, prov) for sc, e, prov in best]
            if not cands:
                break
        save(st)

    ok = sum(1 for v in st.values() if v.get("status") == "ok")
    print(f"\n-- done: {ok} ok / {len(st)} seen --")


if __name__ == "__main__":
    main()
