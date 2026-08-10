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


def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(CYR.get(c, c) for c in s)
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def tokens(s):
    return set(norm(s).split())


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
    if playlist:
        container = f'user playlist "{playlist}"'
        gate = "true"
        statuses = ""
    else:
        container = "library playlist 1"
        # bulk-read once; per-track reads of this property over 6500 tracks are slow
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
  set o to ""
  repeat with i from 1 to (count of tracks of src)
    if {gate} then
      set t to track i of src
      set o to o & my f(name of t) & tab & my f(artist of t) & tab & my f(album of t) & tab & ¬
        my f(genre of t) & tab & my f(year of t) & tab & my f(duration of t) & tab & ¬
        my f(played count of t) & tab & my dt(played date of t) & tab & ¬
        my f(skipped count of t) & tab & my dt(date added of t) & tab & ¬
        my f(bit rate of t) & tab & my f(composer of t) & tab & my f(favorited of t) & tab & ¬
        my f(track number of t) & tab & my f(track count of t) & linefeed
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
    out = []
    for vid in [x.strip() for x in r.stdout.split("\n") if x.strip()][:n]:
        try:
            j = subprocess.run(["yt-dlp", f"https://music.youtube.com/watch?v={vid}",
                                "-J", "--no-warnings", "--skip-download",
                                "--socket-timeout", "20"],
                               capture_output=True, text=True, timeout=90)
            d = json.loads(j.stdout)
        except Exception:
            continue
        if not d.get("duration"):
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
        entries = json.loads(r.stdout).get("entries", []) or []
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
    url = srcs.get(f"{row['Artist']} :: {row['Name']}")
    if not url:
        return None
    r = subprocess.run(["yt-dlp", url, "-J", "--no-warnings", "--skip-download",
                        "--socket-timeout", "30"],
                       capture_output=True, text=True, timeout=120)
    try:
        d = json.loads(r.stdout)
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


def search(query, provider, n=6):
    if provider == "ytm":
        return search_ytm(query, n)
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
            return []
        entries = json.loads(out.stdout).get("entries", []) or []
        return entries[:n]
    except Exception:
        return []


def score(entry, name, artist, want_dur, lenient=False, vouched=False):
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
    if tokens(artist) & tokens(uploader):
        s += 25

    t = tokens(title)
    want_tokens = tokens(name)
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

    # Penalise anything advertising itself as a different rendition. The duration
    # gate catches most of these, but remasters and edits can match on length.
    for bad in ("cover", "remix", "live", "karaoke", "reaction", "sped up",
                "slowed", "instrumental", "8d", "nightcore", "lyrics video"):
        if bad in title.lower() and bad not in name.lower():
            s -= 45
    return s


def pick(name, artist, want_dur, providers, album="", min_score=120):
    """Find the best source, consulting providers strictly as fallbacks.

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
    queries.append(name)

    best, seen = None, []
    for prov in providers:
        for q in queries:
            for e in search(q, prov):
                seen.append((e, prov))
                sc = score(e, name, artist, want_dur)
                if sc is not None and (best is None or sc > best[0]):
                    best = (sc, e, prov)
            if best and best[0] >= min_score:
                return best

    # Nothing in the exact window. Fall back to the shortest take that is still
    # at least as long as the original -- rescoring what was already fetched, so
    # this costs no extra queries.
    if best is None or best[0] < min_score:
        for e, prov in seen:
            sc = score(e, name, artist, want_dur, lenient=True)
            if sc is not None and (best is None or sc > best[0]):
                best = (sc, e, prov)
    return best


def safe(s):
    return re.sub(r'[/:\\]', "-", s).strip() or "track"


def fetch(url, out_base, attempts=3):
    """Download native AAC where possible -- no transcode, and Music plays m4a
    directly. Transient failures (throttling, a dropped connection mid-fragment)
    are common enough that a single attempt loses tracks that are perfectly fine."""
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
    last = None
    for i in range(attempts):
        try:
            subprocess.run(cmd, check=True, timeout=600)
            f = Path(f"{out_base}.m4a")
            if f.exists():
                return f
            last = RuntimeError("yt-dlp produced no output file")
        except Exception as ex:
            last = ex
            Path(f"{out_base}.m4a").unlink(missing_ok=True)
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


def itunes(term, entity):
    import urllib.parse, urllib.request
    url = (f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}"
           f"&entity={entity}&limit=8")
    try:
        return json.load(urllib.request.urlopen(url, timeout=15))["results"]
    except Exception:
        return []


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
            thumb = json.loads(r.stdout).get("thumbnail_url")
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


def deezer_art(row):
    """Second store, checked the same way as iTunes. Catches releases missing from
    Apple -- compilations and small labels especially."""
    import urllib.parse, urllib.request
    artist, album = row["Artist"], re.sub(r" - (Single|EP)$", "", row.get("Album") or "")
    res = []
    for title in dict.fromkeys([row["Name"], base_title(row["Name"])]):
        q = urllib.parse.quote(f'artist:"{artist}" track:"{title}"')
        try:
            res += json.load(urllib.request.urlopen(
                f"https://api.deezer.com/search?q={q}&limit=8", timeout=15)).get("data", [])
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
        data = json.loads(r.stdout)
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
        _spotify_token["value"] = json.loads(r.stdout)["access_token"]
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
        items = json.loads(r.stdout)["tracks"]["items"]
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
            return json.loads(r2.stdout).get("results", [])
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
                d = json.loads(o.stdout)
            except Exception:
                continue
            if norm(d.get("title") or "") != want:
                continue
            thumb = d.get("thumbnail_url")
            if thumb:
                return thumb.replace("ab67616d00001e02", "ab67616d0000b273")
        return None

    pat = r"open\.spotify\.com/(?:intl-[a-z]+/)?track/[A-Za-z0-9]{22}"
    for q in queries:
        r = subprocess.run(
            ["curl", "-sL", "-A", UA_BROWSER, "--max-time", "25", "-w", "\n%{http_code}",
             "https://search.brave.com/search?q=" + urllib.parse.quote(q)],
            capture_output=True, text=True, timeout=60)
        body, _, code = r.stdout.rpartition("\n")
        # Brave rate-limits after a handful of queries and answers 429 with a
        # CAPTCHA page. That page contains no links, so an unchecked status turns
        # "we were blocked" into "there is no cover" -- say so instead.
        if code.strip() == "429" or "captcha" in body[:4000].lower():
            print("      Brave ограничил запросы (429) — ссылку найти нечем", flush=True)
            break
        got = check(["https://" + x for x in
                     sorted(set(re.findall(pat, urllib.parse.unquote(body))))])
        if got:
            return got

    # Second pass: image results carry their source page, which is often a
    # Spotify track even when the web results do not surface it.
    for q in queries:
        r = subprocess.run(
            ["curl", "-s", "-A", UA_BROWSER, "--max-time", "20",
             "https://duckduckgo.com/?q=" + urllib.parse.quote(q) + "&iax=images&ia=images"],
            capture_output=True, text=True, timeout=40)
        mm = re.search(r'vqd="?([\d-]+)"?', r.stdout)
        if not mm:
            continue
        r2 = subprocess.run(
            ["curl", "-s", "-A", UA_BROWSER, "-H", "Referer: https://duckduckgo.com/",
             "--max-time", "20",
             f"https://duckduckgo.com/i.js?l=us-en&o=json&q={urllib.parse.quote(q)}"
             f"&vqd={mm.group(1)}&f=,,,&p=1"],
            capture_output=True, text=True, timeout=40)
        try:
            results = json.loads(r2.stdout).get("results", [])
        except Exception:
            continue
        links = []
        for x in results:
            links += ["https://" + y for y in re.findall(
                pat, (x.get("url") or "") + " " + (x.get("source") or ""))]
        got = check(links)
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


def already_live(name, artist):
    """A geoblocked entry whose song also exists as a playable subscription copy
    needs no replacement -- it was already swapped by hand at some point and the
    dead entry just never got cleaned up. Downloading over it would add a worse
    128k file next to a 256k one that already works."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    scr = f'''
tell application "Music"
  repeat with t in (every track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}")
    try
      set cs to (cloud status of t as text)
      if cs is "subscription" or cs is "matched" or cs is "purchased" then return "1"
    end try
  end repeat
  return "0"
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    return r.returncode == 0 and r.stdout.strip() == "1"


def purge_ours(name, artist):
    """Music matches a re-imported file to the existing entry and KEEPS the old
    one -- so re-running silently changes nothing unless the previous import is
    removed first. Only our own imports are touched: a file track carrying our
    marker (or the bare URL comment older runs wrote), or one sitting in the
    refetched playlist. The user's own local files have neither, and streaming
    originals are not file tracks at all."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    scr = f'''
tell application "Music"
  set n to 0
  set ours to {{}}
  try
    set ours to (get database ID of every track of user playlist "{REFETCHED_PLAYLIST}")
  end try
  repeat with t in (every file track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}")
    set c to ""
    try
      set c to comment of t
    end try
    if c starts with "{MARK}" or c starts with "{OLD_MARKS[0]}" or c starts with "{OLD_MARKS[1]}" or ((database ID of t) is in ours) then
      delete t
      set n to n + 1
    end if
  end repeat
  return n
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    return r.stdout.strip() if r.returncode == 0 else "0"


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
    scr = f'tell application "Music" to add POSIX file "{esc(str(path))}"'
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


def ensure_playlists():
    for p in (REPLACED_PLAYLIST, REFETCHED_PLAYLIST):
        subprocess.run(["osascript", "-e", f'''
tell application "Music"
  if not (exists user playlist "{p}") then
    make new user playlist with properties {{name:"{p}"}}
  end if
end tell'''], capture_output=True, text=True, timeout=60)


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
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--no-soundcloud", action="store_true",
                    help="YouTube only; skip the SoundCloud fallback")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-score", type=float, default=120,
                    help="below this a match is parked for review, not imported")
    ap.add_argument("--snapshot", action="store_true",
                    help="fold favourites and counts of already-replaced tracks "
                         "back into the index; run before anything destructive")
    ap.add_argument("--refresh-index", action="store_true",
                    help="rebuild the track list from Music (no manual export needed)")
    ap.add_argument("--playlist", default=None,
                    help="narrow the scan to a playlist; default is every track "
                         "Music reports as no longer available")
    ap.add_argument("--report", action="store_true",
                    help="print a summary of the run so far and exit")
    ap.add_argument("--repair", action="store_true",
                    help="patch already-imported tracks in place instead of fetching")
    a = ap.parse_args()

    # YouTube first: Topic channels carry label-delivered audio. SoundCloud next,
    # then Mail.Ru -- a user-upload catalogue that is often the only place old and
    # obscure Russian material still exists.
    providers = ["yt"] if a.no_soundcloud else ["yt", "sc", "mr", "ytm"]
    DEST.mkdir(parents=True, exist_ok=True)
    ensure_playlists()
    st = load_state()
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

    if a.report:
        report(rows)
        return

    if a.repair:
        repair(rows, st)
        return

    batch = rows[a.start:a.start + a.limit]

    for i, row in enumerate(batch, start=a.start):
        key = f"{row['Artist']} :: {row['Name']}"
        if st.get(key, {}).get("status") == "ok":
            print(f"[{i}] skip (done): {key}"); continue
        want = int(row["Time"])
        t0 = time.monotonic()
        print(f"[{i}] {key}  ({want}s)", flush=True)

        if not a.dry_run and already_live(row["Name"], row["Artist"]):
            st[key] = {"status": "already_live", "want": want, "secs": round(time.monotonic()-t0,1)}
            print("      УЖЕ ЕСТЬ живая копия — пропускаю"); save_state(st); continue

        # A named release beats any search: it is the actual record, not a guess
        # about which upload corresponds to it.
        hit = (pick_from_track_source(row)
               or pick_from_album(row, want, a.min_score)
               or pick(
                  row["Name"], row["Artist"], want, providers,
                  row.get("Album", ""), a.min_score))
        if not hit:
            st[key] = {"status": "not_found", "want": want, "secs": round(time.monotonic()-t0,1)}
            print("      NOT FOUND"); save_state(st); continue

        sc, e, prov = hit
        url = e.get("url") or e.get("webpage_url")
        print(f"      -> [{prov} {sc:.0f}] {e.get('uploader')} | {e.get('title')} | {e.get('duration')}s")
        # A candidate rejected only for being a few seconds long may simply carry
        # padded silence. That is cheap to settle with evidence: fetch it and
        # measure the sound itself. Anything still off is a different cut.
        prefetched = None
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
                    sc = score({**e, "duration": eff}, row["Name"], row["Artist"], want)
                    print(f"      тишина по краям: чистого звука {eff:.0f}s "
                          f"(Δ{eff - want:+.0f}s) — принято, score {sc:.0f}")
                    prefetched = cand
                else:
                    shown = f"{eff:.0f}s (Δ{eff - want:+.0f}s)" if eff else "не измерился"
                    print(f"      чистого звука {shown} — другой монтаж")
                    # Text must never outvote the audio: a candidate the
                    # measurement rejected cannot stay above the import bar.
                    sc = min(sc, a.min_score - 1)
                    cand.unlink(missing_ok=True)
            except Exception as ex:
                print(f"      проверка тишины не удалась: {str(ex)[:60]}")

        # Below this, the match is a guess -- a live cut, a re-upload, a lookalike.
        # Park it for human review instead of quietly polluting the library.
        if sc < a.min_score:
            st[key] = {"status": "review", "url": url, "score": sc, "cand": e.get("title"),
                       "uploader": e.get("uploader"), "want": want,
                       "secs": round(time.monotonic()-t0,1)}
            print("      LOW CONFIDENCE -> review"); save_state(st); continue
        if a.dry_run:
            st[key] = {"status": "dry", "url": url, "score": sc,
                       "cand": e.get("title"), "uploader": e.get("uploader")}
            save_state(st); continue

        try:
            f = prefetched or fetch(url, str(DEST / safe(f"{row['Artist']} - {row['Name']}")))
            if not f:
                raise RuntimeError("no output file")
            art_src = tag(f, row,
                          e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None,
                          src_url=url, uploader=e.get("uploader") or "",
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
            print(f"      ADDED  art={art_src} parked={parked} plays={plays} "
                  f"({time.monotonic()-t0:.0f}s)")
        except Exception as ex:
            # Record whether a delete already happened. Adjacent is not atomic:
            # if the import failed after purge_ours removed the previous copy,
            # the track is gone and a later run must know to re-add rather than
            # treat this as an ordinary failure.
            st[key] = {"status": "error", "url": url, "error": str(ex),
                       "purged": locals().get("purged", "?"),
                       "secs": round(time.monotonic()-t0,1)}
            print(f"      ERROR  {ex}")
        save_state(st)

    ok = sum(1 for v in st.values() if v.get("status") == "ok")
    print(f"\n-- done: {ok} ok / {len(st)} seen --")


if __name__ == "__main__":
    main()
