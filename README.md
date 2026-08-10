# geo-unblocked

Rebuilds Apple Music tracks that went dark behind a region lock, and puts them back
into the library **as the same entry** — same play count, same last-played date, same
place in every playlist.

Apple reports these as `cloud status: no longer available`. They are not blocked
everywhere and usually not on purpose: a distributor ticks the wrong set of
territories, and a paid-for track stops playing in one country while remaining
available in the next one over. This tool re-acquires the audio, tags it to match
what Apple had, and re-imports it.

## What it does per track

1. **Skip if already fine.** A geoblocked entry whose song also exists as a playable
   subscription copy needs nothing — it was swapped by hand at some point and the
   dead entry never got cleaned up.
2. **Find the audio.** YouTube, then SoundCloud, then Mail.Ru — strictly as
   fallbacks. The search stops at the first match good enough to import unattended.
3. **Verify by duration.** A candidate must land within ±4s of the length Apple
   reported. This is the gate that keeps out covers, live cuts, remixes and
   "sped up" edits, which title text alone cannot distinguish. Matches from 4s to
   12s off are plausible remasters and go to review instead of the library.
4. **Download** native AAC where available, so nothing is transcoded.
5. **Find artwork** through five sources, each verified before use (below).
6. **Import,** then carry the history over and file the leftovers into playlists.

## Artwork sources, in descending order of trust

| source | verified on | notes |
|---|---|---|
| named by hand | a human said so | `data/artwork_sources.json`; beats every guess |
| another library copy | — | literally the cover Apple was showing |
| iTunes | artist + album | search by *track*; searching by album misses constantly |
| Deezer | artist + album | catches compilations and small labels Apple dropped |
| Genius | artist + title | no album exposed, so a weaker check; wraps art in a gold frame |
| Spotify | artist + title | needs credentials (below); skipped silently without them |
| source page image | uploader is the artist | a re-uploader's thumbnail is a picture *they* picked |
| image search | square + art CDN + artist and title in caption | last resort; all three conditions, never fewer |

The image-search source needs all three of its conditions at once. Any one alone
is far too weak: searching a track title returns a square, correctly-sized Apple
cover for a *different* artist's song of the same name, and it looks entirely
convincing until someone opens it. It also queries using the matched source's own
title, because the export romanises Russian artists and a romanised query never
reaches a cover captioned in Cyrillic.

A release named in `data/album_sources.json` is trusted outright — its cover *is*
the album cover, so it bypasses the uploader check.

### Naming a cover by hand

For the handful of tracks every source misses, put the cover in
`data/artwork_sources.json`, keyed `Artist :: Track`. The value is either a
direct image URL or a **Spotify track link**, which is resolved through
Spotify's public oEmbed endpoint — no credentials, no account. oEmbed hands back
the 300px variant and the CDN id maps straight onto the 640px one.

A hand-named cover is tried first: a deliberate human choice outranks every
heuristic in the chain.

### Spotify Web API (optional, and probably not worth it)

The search-based Spotify source needs an app of your own, and **Spotify gates
the Web API checkbox behind a Premium subscription** — a second music
subscription is an absurd price for a few covers, so the oEmbed route above is
the better answer. The unofficial web-player token endpoints are not an
alternative: they are blocked, and Spotify states outright that using them
breaches the Developer Terms.

If you happen to have Premium: create an app at
<https://developer.spotify.com/dashboard> selecting **Web API**; the dashboard
demands a redirect URI even though the client-credentials flow never uses one,
so any valid value does (`http://127.0.0.1:8888/callback` — plain
`http://localhost` is no longer accepted). Then copy
`data/spotify.json.example` to `data/spotify.json` and fill it in, or export
`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.

`data/spotify.json` is gitignored. The secret is yours: fill it in locally, and
do not paste it into a chat or commit it anywhere.

Every cover is force-cropped to a centred square at 1200×1200. Apple itself serves
some covers a few pixels off square, and Music renders anything non-square badly.

**No artwork beats wrong artwork.** When nothing verifies, the field is left empty —
a merged entry still shows Apple's own cover, and overwriting it with a lookalike
from another release makes things worse.

## What survives, and what cannot

Play counts, last-played, skips, loved and rating are all carried over. They come
from the merge when Music merges, from the dead duplicate when it does not, and from
the Apple export when neither exists.

**`date added` cannot be restored.** Music refuses writes to that field
(`Unknown object type`), by any route. Every replaced track shows today's date. The
original is written into the track comment instead, along with the source URL and
the original bitrate:

```
geo-unblocked · source: https://www.youtube.com/watch?v=…
original Apple Music entry: added 26.08.2019, 02:37 · 20 plays · last played 30.07.2020, 13:14
was: 256 AAC audio file
```

The leading marker identifies the tool's own imports, so a rerun can replace them
without touching anything of the user's.

## Quality ceiling

YouTube's `- Topic` channels carry label-delivered audio — the same masters the
stores use — but cap at **AAC 130 kbps**. SoundCloud occasionally does better
(164 kbps observed). Apple Music was 256 kbps AAC, so every replacement is a step
down. Opus is sometimes higher-bitrate but Music cannot play it, and transcoding to
AAC loses more than the bitrate gains.

## Usage

```bash
python3 geo-unblock.py --start 0 --limit 30        # process a slice
python3 geo-unblock.py --start 0 --limit 30 --dry-run   # search only, import nothing
python3 geo-unblock.py --repair                    # patch already-imported tracks in place
```

| flag | effect |
|---|---|
| `--dry-run` | resolve matches, download and import nothing |
| `--min-score` | confidence below which a match goes to review (default 120) |
| `--no-soundcloud` | YouTube only |
| `--repair` | fix artwork, history and comments on tracks already imported |

`--repair` exists for tracks imported by an older version. A normal rerun would
delete and re-import them, resetting `date added` a second time and discarding
restored history; repair only touches library metadata.

Progress lives in `data/state.json` and every run resumes from it.

## Playlists

- **Geo-Unblocked — refetched** — the files brought back
- **Geo-Unblocked — originals** — dead entries that did *not* merge and are now
  duplicates. History has already been copied off them, so they can be deleted
  without losing anything.

## Requirements

`yt-dlp`, `ffmpeg`, Python 3, and macOS Music with AppleScript automation allowed.
Reading files inside `~/Music/Music` additionally needs Full Disk Access, but the
tool does not require it — it reads artwork out of Music itself.

## Seeing what happened

```bash
python3 geo-unblock.py --report
```

Counts, sources, per-track timings, and everything left in review or unfound.
Artwork and bitrate figures are read back out of Music rather than from the run
log — state records what an import *intended*, and a later repair changes the
library without touching it, so reporting the intention would be a lie.
