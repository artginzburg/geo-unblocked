# geo-unblocked

Rebuilds Apple Music tracks that went dark behind a region lock, and puts them
back into the library **as the same entry** — same play count, same last-played
date, same place in every playlist.

Apple reports these as `cloud status: no longer available`. They are usually not
blocked deliberately and rarely blocked everywhere: a distributor ticks the
wrong set of territories, and a paid-for track stops playing in one country
while remaining available in the next one over. This tool re-acquires the audio,
tags it to match what Apple had, and re-imports it.

## Getting the list of broken tracks

**No export, no playlist, nothing to set up.** The tool asks Music directly for
every track it reports as `cloud status: no longer available` — which is the
same rule a "geoblocked" smart playlist would hold, except the rule lives on the
track itself, so no playlist has to exist. (One could not be created anyway:
Music's scripting interface makes plain playlists but cannot give them rules.)

```bash
python3 geo-unblock.py --refresh-index          # scan the library
python3 geo-unblock.py --refresh-index --playlist "Some Playlist"   # narrow the scope
```

The scan runs automatically the first time if no list exists yet, takes about a
minute on a 6500-track library, and writes `data/list.tsv`. Rerun it whenever
the library changes; tracks already replaced drop out on their own, since they
are no longer unavailable.

`--playlist` is for scoping to a subset — a playlist you maintain by hand, or a
smart playlist with extra conditions. Without it the whole library is scanned.

An Apple **File → Library → Export Playlist** dump can still be dropped in as
`data/list.tsv` and works unchanged, but it is no longer needed and is strictly
worse: it has no `Favorited` column, and the favourite flag has no other backup.

## What it does per track

1. **Skips what is already fine.** A geoblocked entry whose song also exists as
   a playable subscription copy needs nothing — it was swapped by hand at some
   point and the dead entry never got cleaned up.
2. **Finds the audio.** A source named by hand first, then a named release, then
   searches — YouTube, SoundCloud, Mail.Ru, YouTube Music — strictly as
   fallbacks, stopping at the first match good enough to import unattended.
3. **Verifies by duration**, asymmetrically. Within ±4s of Apple's length is
   accepted. *Shorter* is rejected outright: audio the original had is missing.
   *Longer* is penalised but allowed as a fallback, since it still contains the
   whole recording.
4. **Measures the audio when the length is off** by 4–12s. That is usually
   padded silence rather than a different recording, and `silencedetect` settles
   which — one track ran 206s in the file but 198s of actual sound against an
   expected 195s, and was accepted on that evidence.
5. **Downloads** native AAC where available, so nothing is transcoded.
6. **Finds artwork** through nine sources, each verified before use.
7. **Imports,** carries the listening history across, and files leftovers into
   playlists.

Anything the rules cannot settle is parked for review rather than guessed at.

## Overriding the automation

Three files let a person's judgement outrank the algorithm. All are applied
before any search runs, and none is merely a convenience — each exists because
something was lost without it.

| file | keyed by | for |
|---|---|---|
| `data/track_sources.json` | `Artist :: Track` | a source approved for one track, no score gate |
| `data/album_sources.json` | album name | a whole release, when search cannot find it |
| `data/artwork_sources.json` | `Artist :: Track` | one cover: an image URL, or a Spotify link |

**Approved sources must be written down.** A verdict held only in conversation
is lost on the next clean run, which re-parks the track in review — and if its
library entry has merged by then, the delete preceding that run takes the track
out of the library altogether. That happened once.

**A named release beats any search.** JioSaavn holds this project's
production-music compilation at 320kbps — above the 256 the tracks originally
had — while its own search returns karaoke covers for the same query. One entry
naming the album resolves all fifteen of its tracks, and their durations match
Apple's to within a second each.

A cover in `artwork_sources.json` may be a Spotify track link; it resolves
through Spotify's public oEmbed endpoint, which needs no account.

The tool also tries to **find** such a link by itself, searching Brave and then
DuckDuckGo's image results, and resolving every candidate through oEmbed —
accepting it only when Spotify's own title matches. That is stronger than
trusting the caption beside a picture, which routinely names a different track
from the same album.

It works, and it has a hard ceiling: Brave is the only engine that serves these
links in plain HTML, and it rate-limits after a handful of queries with a CAPTCHA
page that looks like an empty result. Over a couple of hundred tracks it will be
blocked long before the end, so the hand-named map is still the reliable route
for the tail.

## Artwork sources, in descending order of trust

| source | verified on | notes |
|---|---|---|
| named by hand | a human said so | `data/artwork_sources.json` |
| named release | the release itself | its cover *is* the album cover |
| another library copy | — | literally the cover Apple was showing |
| iTunes | artist + album | search by *track*; searching by album misses constantly |
| Deezer | artist + album | catches compilations and small labels Apple dropped |
| Genius | artist + title | no album exposed, so a weaker check; adds a gold frame |
| Spotify | artist + title | needs credentials, see below; skipped without them |
| Spotify, link discovered | Spotify's own title, via oEmbed | no account needed; limited by search recall |
| source page image | uploader is the artist | a re-uploader's thumbnail is a picture *they* picked |
| image search | square **and** art CDN **and** artist and title in the caption | last resort |

Image search needs all three of its conditions at once. Any one alone is far too
weak: searching a track title returns a square, correctly-sized Apple cover for
a *different* artist's song of the same name, and it looks entirely convincing
until someone opens it. It also queries using the matched source's own title,
because the export romanises Russian artists and a romanised query never reaches
a cover captioned in Cyrillic.

Every cover is force-cropped to a centred square at 1200×1200. Apple itself
serves some a few pixels off square, and Music renders anything non-square
badly.

**No artwork beats wrong artwork.** When nothing verifies the field is left
empty — a merged entry still shows Apple's own cover, and overwriting it with a
lookalike from another release makes things worse.

## What survives, and what cannot

Play counts, last-played, skips, favourite and rating are carried across, along
with the composer. They come from the merge when Music merges, from the dead
duplicate when it does not, and from the Apple export when neither exists.

**`date added` cannot be restored.** Music refuses writes to that field
(`Unknown object type`) by any route. Every replaced track shows today's date.
The original goes into the track comment instead, with the source URL and the
original bitrate:

```
geo-unblocked · source: https://www.youtube.com/watch?v=…
original Apple Music entry: added 26.08.2019, 02:37 · 20 plays · last played 30.07.2020, 13:14
was: 256 AAC audio file
```

The leading marker identifies the tool's own imports, so a rerun can replace
them without touching anything of yours.

**The favourite flag is only as safe as the index.** Music's export has no such
column, so a scan of the library is the only place it is captured. Refresh the
index before doing anything destructive; once a dead entry is gone, a flag that
was never indexed is gone with it.

## Quality ceiling

YouTube's `- Topic` channels carry label-delivered audio — the same masters the
stores use — but cap at **AAC 130 kbps**. SoundCloud sometimes does better (164
and 391 kbps both observed). A named release on JioSaavn can beat the original
outright at 320. Apple Music was 256 kbps AAC, so most replacements are a step
down and a few are a step up.

Opus is occasionally higher-bitrate, but Music cannot play it and transcoding to
AAC loses more than the bitrate gains.

## Before a full run, back the library up

A slice of thirty is repairable by hand; a full pass is not. Both halves matter,
and the small one matters more:

```bash
osascript -e 'tell application "Music" to quit'
ditto ~/Music/Music/"Music Library.musiclibrary" ~/Backups/music-$(date +%F)/"Music Library.musiclibrary"
open -a Music
```

That is the whole backup. The database holds play counts, playlists, favourites
and add dates — the part no download restores — and it is about 96MB.

**Do not copy the media folder.** On a subscription the catalogue re-downloads
from Apple. In this library only 149 of 6519 tracks are `uploaded` or `matched`
(the user's own uploads plus this tool's imports) — 0.41GB against 56.9GB of
subscription content. Back up those few files if you like; copying everything is
hours of I/O for nothing.

Keep the backup outside `~/Music` so Music does not index it.

## Usage

```bash
python3 geo-unblock.py --refresh-index                 # rebuild the list from Music
python3 geo-unblock.py --start 0 --limit 30            # process a slice
python3 geo-unblock.py --start 0 --limit 30 --dry-run  # search only, import nothing
python3 geo-unblock.py --report                        # summarise the run so far
python3 geo-unblock.py --repair                        # patch already-imported tracks
```

| flag | effect |
|---|---|
| `--snapshot` | fold favourites and counts of replaced tracks back into the index |
| `--refresh-index` | rebuild `data/list.tsv` from Music; runs automatically if absent |
| `--playlist NAME` | scan a playlist instead of the whole library |
| `--dry-run` | resolve matches, download and import nothing |
| `--min-score` | confidence below which a match goes to review (default 120) |
| `--no-soundcloud` | YouTube only |
| `--report` | counts, sources, timings, and anything left in review |
| `--repair` | fix artwork, history and comments on tracks already imported |

The playlists the tool creates are named from `$GEO_UNBLOCKED_NAME`, default
`Geo-Unblocked`. Set it if that clashes with something you already have.

`--report` reads artwork and bitrate back out of Music rather than from the run
log: state records what an import *intended*, and a repair changes the library
without touching it, so reporting the intention would be a lie.

`--repair` exists for tracks imported by an older version. A normal rerun would
delete and re-import them, resetting `date added` a second time; repair only
touches library metadata. It is safe to run repeatedly — every operation it
performs converges rather than accumulating.

Progress lives in `data/state.json` and every run resumes from it.

## Playlists

- **Geo-Unblocked — refetched** — the files brought back.
- **Geo-Unblocked — originals** — "parking". Music usually *merges* an import
  into the existing entry, leaving one track. When it does not, two remain: the
  dead geoblocked one and the new file. The dead one is filed here so it can be
  reviewed and deleted in one pass later. Its history has already been copied
  onto the replacement, so nothing is lost by emptying this playlist — and
  without it the duplicates would accumulate unseen.

## Requirements

`yt-dlp`, `ffmpeg`, Python 3, and macOS Music with AppleScript automation
allowed. Reading files inside `~/Music/Music` additionally needs Full Disk
Access, but the tool does not need it — it reads artwork out of Music itself.

### Spotify Web API (optional, and probably not worth it)

The search-based Spotify source needs an app of your own, and **Spotify gates
the Web API checkbox behind a Premium subscription** — a second music
subscription is an absurd price for a few covers, so a Spotify link in
`artwork_sources.json` is the better answer. The unofficial web-player token
endpoints are not an alternative: they are blocked, and Spotify states outright
that using them breaches its Developer Terms.

If you do have Premium: create an app at
<https://developer.spotify.com/dashboard> selecting **Web API**. The dashboard
demands a redirect URI even though the client-credentials flow never uses one,
so any valid value does — `http://127.0.0.1:8888/callback`, since plain
`http://localhost` is no longer accepted. Then copy `data/spotify.json.example`
to `data/spotify.json` and fill it in, or export `SPOTIFY_CLIENT_ID` and
`SPOTIFY_CLIENT_SECRET`.

`data/spotify.json` is gitignored. The secret is yours: fill it in locally, and
do not paste it into a chat or commit it anywhere.
