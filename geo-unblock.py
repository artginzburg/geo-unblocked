#!/usr/bin/env python3
"""geo-unblocked -- re-acquire geoblocked Apple Music tracks and import them into Music.app.

Sources are consulted strictly as fallbacks, in order: YouTube, SoundCloud, Mail.Ru.
The search stops at the first match good enough to import unattended.
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

# Beyond the exact window, up to this much difference is still plausibly the same
# recording -- a different master, or trimmed silence. Those are penalised hard so
# they land in review rather than being imported unseen. Past this, it is a
# different cut of the song (production music ships 30/60/90-second edits) and
# gets dropped outright.
DUR_WIDE = 12
WIDE_PENALTY = 45


def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^a-z0-9а-яё ]+", " ", s).strip()


def tokens(s):
    return set(norm(s).split())


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def search(query, provider, n=6):
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


def score(entry, name, artist, want_dur):
    """Rank a candidate. Returns None when the duration disqualifies it."""
    dur = entry.get("duration")
    if not dur or abs(dur - want_dur) > DUR_WIDE:
        return None
    title = entry.get("title") or ""
    uploader = entry.get("uploader") or entry.get("channel") or ""
    s = 100 - min(abs(dur - want_dur), DUR_TOLERANCE) * 3
    if abs(dur - want_dur) > DUR_TOLERANCE:
        s -= WIDE_PENALTY

    # "<Artist> - Topic" channels are the label-delivered audio YouTube generates
    # from the same masters the stores use. Always the best available source.
    if uploader.endswith("- Topic"):
        s += 60
    if tokens(artist) & tokens(uploader):
        s += 25

    t = tokens(title)
    s += 20 * len(t & tokens(name)) / max(len(tokens(name)), 1)
    if tokens(artist) & t:
        s += 10

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

    best = None
    for prov in providers:
        for q in queries:
            for e in search(q, prov):
                sc = score(e, name, artist, want_dur)
                if sc is not None and (best is None or sc > best[0]):
                    best = (sc, e, prov)
            if best and best[0] >= min_score:
                return best
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


def itunes(term, entity):
    import urllib.parse, urllib.request
    url = (f"https://itunes.apple.com/search?term={urllib.parse.quote(term)}"
           f"&entity={entity}&limit=8")
    try:
        return json.load(urllib.request.urlopen(url, timeout=15))["results"]
    except Exception:
        return []


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

    # By album: catches titles the song search fumbles (numeric names like "27").
    if album:
        for r in itunes(f"{artist} {album}", "album"):
            if close(r.get("artistName", ""), artist) and close(r.get("collectionName", ""), album):
                return big(r["artworkUrl100"])
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
    q = urllib.parse.quote(f'artist:"{artist}" track:"{row["Name"]}"')
    try:
        res = json.load(urllib.request.urlopen(
            f"https://api.deezer.com/search?q={q}&limit=8", timeout=15)).get("data", [])
    except Exception:
        return None
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


def artist_uploaded(uploader, row):
    """True when the upload comes from the artist themselves -- their own channel,
    or someone credited in the track title (a `feat.` collaborator). Their page
    image is the release art; a random re-uploader's is whatever they picked."""
    if not uploader:
        return False
    if uploader.endswith("- Topic"):
        return True
    return close(uploader, row["Artist"]) or bool(tokens(uploader) & tokens(row["Name"]))


def get_art(row, thumb_url, uploader=""):
    art = SCRATCH / "cover.jpg"
    raw = SCRATCH / "art_raw"
    for p in (art, raw):
        p.unlink(missing_ok=True)

    # 1. Another copy already in the library -- the cover Apple itself showed.
    if library_art(row, str(raw)) and raw.stat().st_size > 2000 and squarify(raw, art):
        raw.unlink(missing_ok=True)
        return art, "library"
    raw.unlink(missing_ok=True)

    # 2-4. Stores first (artist+album verified), then Genius (artist+title only).
    for url, label in ((itunes_art(row), "store"), (deezer_art(row), "deezer"),
                       (genius_art(row), "genius")):
        if not url:
            continue
        subprocess.run(["curl", "-sL", "-o", str(raw), url], timeout=60)
        if raw.exists() and raw.stat().st_size > 2000 and squarify(raw, art):
            raw.unlink(missing_ok=True)
            return art, label
        raw.unlink(missing_ok=True)

    # 5. Last resort: the source page image, but only when the artist posted it
    # themselves. A random re-uploader's thumbnail is a picture they chose, and
    # embedding it would overwrite correct art. No artwork beats wrong artwork.
    if thumb_url and artist_uploaded(uploader, row):
        subprocess.run(["curl", "-sL", "-o", str(raw), thumb_url], timeout=60)
        if raw.exists() and raw.stat().st_size > 1000 and squarify(raw, art):
            raw.unlink(missing_ok=True)
            return art, "source-thumb"
        raw.unlink(missing_ok=True)
    return None, "none"


def tag(path, row, thumb_url, src_url="", uploader=""):
    art, art_src = get_art(row, thumb_url, uploader)
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


def inherit_history(name, artist):
    """Carry listening history from the dead original onto our new file, so the
    leftover duplicate can be deleted without losing anything. Play counts are
    summed: both numbers are real plays of the same song, and the dead entry is
    on its way out. `date added` is deliberately absent -- Music refuses writes
    to it, so the original add date cannot be preserved by any means."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    scr = f'''
tell application "Music"
  set deadPlays to 0
  set deadSkips to 0
  set deadDate to missing value
  set deadLoved to false
  set deadRating to 0
  set found to false
  repeat with t in (every track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}")
    try
      if (cloud status of t as text) is "no longer available" then
        set found to true
        set deadPlays to deadPlays + (played count of t)
        try
          set deadSkips to deadSkips + (skipped count of t)
        end try
        try
          if (played date of t) is not missing value then
            if deadDate is missing value or (played date of t) > deadDate then set deadDate to (played date of t)
          end if
        end try
        try
          if loved of t then set deadLoved to true
        end try
        try
          if (rating of t) > deadRating then set deadRating to (rating of t)
        end try
      end if
    end try
  end repeat
  if not found then return "0"

  set moved to 0
  repeat with t in (every file track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}")
    try
      set (played count of t) to ((played count of t) + deadPlays)
      try
        set (skipped count of t) to ((skipped count of t) + deadSkips)
      end try
      if deadDate is not missing value then
        set cur to missing value
        try
          set cur to (played date of t)
        end try
        if cur is missing value or deadDate > cur then set (played date of t) to deadDate
      end if
      if deadLoved then set (loved of t) to true
      if deadRating > 0 and (rating of t) is 0 then set (rating of t) to deadRating
      set moved to moved + 1
    end try
  end repeat
  return (deadPlays as text)
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    return r.stdout.strip() if r.returncode == 0 else "0"


def apply_export_history(row):
    """Write the play history recorded in the Apple export onto our new file.

    This is the backstop that makes history survive regardless of what Music does
    on import: a merge keeps it, a non-merge leaves a dead duplicate to inherit
    from, and a destroyed entry leaves nothing at all -- but the export always
    has the numbers. Counts are only raised, never lowered."""
    want = int(row.get("Plays") or 0)
    skips = int(row.get("Skips") or 0)
    last = row.get("Last Played") or ""
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
    if not want and not skips and not pre:
        return "0"
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    scr = pre + f'''
tell application "Music"
  set t to (first file track of library playlist 1 whose name is "{esc(row['Name'])}" and artist is "{esc(row['Artist'])}")
  if {want} > (played count of t) then set (played count of t) to {want}
  try
    if {skips} > (skipped count of t) then set (skipped count of t) to {skips}
  end try
  {"try" if pre else ""}
  {"set (played date of t) to dt" if pre else ""}
  {"end try" if pre else ""}
  return (played count of t) as text
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    return r.stdout.strip() if r.returncode == 0 else "0"


def add_to_music(path):
    scr = f'tell application "Music" to add POSIX file "{path}"'
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


REPLACED_PLAYLIST = "Geo-Unblocked — originals"   # the dead Apple Music entries
REFETCHED_PLAYLIST = "Geo-Unblocked — refetched"  # the files we brought back

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
    # Report the playlist's actual growth, not the number of duplicate calls made:
    # Music merges most imports into the existing entry, after which there is no
    # dead original left to park and the duplicate quietly does nothing.
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    scr = f'''
tell application "Music"
  set pl to user playlist "{REPLACED_PLAYLIST}"
  set before to (count of tracks of pl)
  repeat with t in (every track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}")
    try
      if (cloud status of t as text) is "no longer available" then duplicate t to pl
    end try
  end repeat
  return ((count of tracks of pl) - before)
end tell'''
    r = subprocess.run(["osascript", "-e", scr], capture_output=True, text=True, timeout=120)
    return r.stdout.strip() if r.returncode == 0 else "0"


def thumb_for(url):
    if not url:
        return None
    try:
        r = subprocess.run(["yt-dlp", url, "--print", "thumbnail", "--no-warnings",
                            "--skip-download", "--socket-timeout", "20"],
                           capture_output=True, text=True, timeout=90)
        t = r.stdout.strip().split("\n")[0].strip()
        return t if t.startswith("http") else None
    except Exception:
        return None


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
    for line in out.split("\n"):
        p = line.split("\t")
        if len(p) < 3:
            continue
        name, artist, nart = p[0], p[1], int(p[2])
        row = exp.get((name, artist))
        if not row:
            continue
        rec = state.get(f"{artist} :: {name}", {})

        if nart == 0:
            art, src = get_art(row, thumb_for(rec.get("url")), rec.get("uploader") or "")
            if art:
                r = subprocess.run(["osascript", "-e", f'''
tell application "Music"
  set t to (first file track of library playlist 1 whose name is "{esc(name)}" and artist is "{esc(artist)}")
  set data of artwork 1 of t to (read (POSIX file "{art}") as picture)
end tell'''], capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    art_fixed += 1
                    print(f"  обложка  {name} — {artist}  ({src})")
                else:
                    missed += 1
            else:
                missed += 1
                print(f"  нет арта {name} — {artist}")

        if inherit_history(name, artist) not in ("0", ""):
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

    print(f"\nобложек проставлено: {art_fixed} (не нашлось: {missed})")
    print(f"история подтянута: {hist_fixed}")
    print(f"комментариев записано: {cmt_fixed}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--no-soundcloud", action="store_true",
                    help="YouTube only; skip the SoundCloud fallback")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-score", type=float, default=120,
                    help="below this a match is parked for review, not imported")
    ap.add_argument("--repair", action="store_true",
                    help="patch already-imported tracks in place instead of fetching")
    a = ap.parse_args()

    # YouTube first: Topic channels carry label-delivered audio. SoundCloud next,
    # then Mail.Ru -- a user-upload catalogue that is often the only place old and
    # obscure Russian material still exists.
    providers = ["yt"] if a.no_soundcloud else ["yt", "sc", "mr"]
    DEST.mkdir(parents=True, exist_ok=True)
    ensure_playlists()
    st = load_state()
    rows = [r for r in csv.DictReader(LIST.open(), delimiter="\t") if r.get("Name")]

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

        hit = pick(row["Name"], row["Artist"], want, providers,
                   row.get("Album", ""), a.min_score)
        if not hit:
            st[key] = {"status": "not_found", "want": want, "secs": round(time.monotonic()-t0,1)}
            print("      NOT FOUND"); save_state(st); continue

        sc, e, prov = hit
        url = e.get("url") or e.get("webpage_url")
        print(f"      -> [{prov} {sc:.0f}] {e.get('uploader')} | {e.get('title')} | {e.get('duration')}s")
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
            base = DEST / safe(f"{row['Artist']} - {row['Name']}")
            f = fetch(url, str(base))
            if not f:
                raise RuntimeError("no output file")
            art_src = tag(f, row,
                          e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None,
                          src_url=url, uploader=e.get("uploader") or "")
            # Park BEFORE importing: once our file is added Music may be mid-merge,
            # and the dead original briefly stops being findable.
            parked = park_original(row["Name"], row["Artist"])
            purged = purge_ours(row["Name"], row["Artist"])
            tid = add_to_music(f)
            add_to_playlist(tid, REFETCHED_PLAYLIST)
            # Only matters when the original did NOT merge; a merge already keeps
            # the history, and inherit_history finds no dead entry to copy from.
            inherit_history(row["Name"], row["Artist"])
            plays = apply_export_history(row)
            # Music copies imports into its own Media folder, so the staged file is
            # now a second copy of the same audio. Drop it.
            f.unlink(missing_ok=True)
            st[key] = {"status": "ok", "url": url, "src": prov, "score": sc, "art": art_src,
                       "track": tid, "parked": parked, "purged": purged,
                       "plays": plays, "uploader": e.get("uploader"),
                       "secs": round(time.monotonic()-t0,1)}
            print(f"      ADDED  art={art_src} parked={parked} plays={plays} "
                  f"({time.monotonic()-t0:.0f}s)")
        except Exception as ex:
            st[key] = {"status": "error", "url": url, "error": str(ex),
                       "secs": round(time.monotonic()-t0,1)}
            print(f"      ERROR  {ex}")
        save_state(st)

    ok = sum(1 for v in st.values() if v.get("status") == "ok")
    print(f"\n-- done: {ok} ok / {len(st)} seen --")


if __name__ == "__main__":
    main()
