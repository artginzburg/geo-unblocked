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
track itself, so no playlist has to exist. (One could not be created anyway —
see below; making it by hand is still worth doing, for a different reason.)

```bash
python3 geo-unblock.py --refresh-index          # scan the library
python3 geo-unblock.py --refresh-index --playlist "Some Playlist"   # narrow the scope
```

The scan runs automatically the first time if no list exists yet, takes about a
second on a 6500-track library, and writes `data/list.tsv`. Rerun it whenever
the library changes; tracks already replaced drop out on their own, since they
are no longer unavailable.

Store lookups (artwork, availability) go to your own storefront, plus `us` as a
second look. **The storefront is detected**, from the signed-in account rather
than from macOS's region — the two are not the same thing, and only the Apple
ID's country moves the storefront, so travelling does not change it. You can see
what was found with:

```bash
defaults read com.apple.itunesstored AccountsNotificationPlugin.ActiveStorefrontIdentifier
```

The leading number is the storefront id, and the iTunes API takes it directly,
so no country code is involved. Override it with a two-letter code if you want a
different catalogue:

```bash
GEO_UNBLOCKED_STORE=us python3 geo-unblock.py --repair
```

`--playlist` is for scoping to a subset — a playlist you maintain by hand, or a
smart playlist with extra conditions. Without it the whole library is scanned.

### A standing smart playlist, worth ninety seconds of your time

The tool does not need one. You probably want one anyway: geoblocking keeps
happening, and a live playlist is the only thing that tells you it has happened
again without your running anything. Make it once and it stays a permanent
readout of how much is currently broken.

**Music can only make it through its own interface.** In the scripting
dictionary `smart` is read-only and no criteria property or command exists at
all, so no script — this one or any other — can create it or add the rule.

1. **File → New → Smart Playlist** (⌥⌘N).
2. Set the one rule to **iCloud Status** · **is** · **No Longer Available**.
3. Leave **Live updating** checked. That is the entire point of it.
4. Name it anything; `Geoblocked` reads well in the sidebar.

That rule is the same condition the tool applies when it scans, so the playlist
and `--refresh-index` always agree. Its count going up is the signal to run
again — and once a run finishes, what stays in it is what could not be
rebuilt.

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
6. **Checks the audio against the release itself**, when a short recording of
   the release can be found — see below. A file that is not the same recording
   is rejected, whatever its title says; one that is gets imported even when its
   title or artist would not have passed.
7. **Finds artwork** through nine sources, each verified before use.
8. **Imports,** carries the listening history across, and files leftovers into
   playlists.

Anything neither the rules nor the audio can settle is parked for review rather
than guessed at.

**A candidate has to match the title, not just the artist and the length** —
unless the audio proves it. A song by the right artist, of the right length,
under a completely different name is never imported on text alone: three such
imports had slipped through before this rule existed (`Gin Wigmore :: Oh My` had
been replaced by her own `Hey Ho`). A release published under a **translated**
title (`Капитан Джек Воробей` as `Capitán Jack Sparrow`) or a pseudonym fails the
same rule and is right; when a reference recording exists, the comparison below
decides it, and only without one does it wait for a person.

## Overriding the automation

Three files let a person's judgement outrank the algorithm. All are applied
before any search runs, and none is merely a convenience — each exists because
something was lost without it.

| file | keyed by | for |
|---|---|---|
| `data/track_sources.json` | `Artist :: Track` | a source approved for one track, no score gate; optionally `{"url": …, "trim": [start, end]}` when the release is a cut of it |
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

## How the tool tells the right file from a lookalike

Titles, artists and lengths are typed by whoever uploaded, and every one of them
has let a wrong file in at some point: covers, radio rips, a re-mix by the same
artist, a copy pitch-shifted by 12 cents. The only identifier that survives all
of that is the audio. So for every track it is about to import, the tool first
looks for **a short recording of the actual release** — a reference — and
compares the downloaded file against it.

**Where the reference comes from**, cheapest first. A 30-second store preview is
enough, and Apple's outlives the track: it stays on Apple's CDN after the release
leaves every storefront.

| source | what it gives | example it found |
|---|---|---|
| Apple's search API | the preview of a release still listed in your store or in `us` | `Sex Bob-Omb :: Summertime`, gone from `ge`, live in `us` |
| Deezer | its 30s preview | `Сатана Печёт Блины :: Sigma` |
| Genius | the Apple preview of the track it linked, plus the exact length | `MARU NARA :: I GET DOWN`, whose album was re-delivered without it |
| Wayback Machine, Apple pages | every preview an archived album or song page listed | `405Okced :: Creelles Town`, captured 2024 |
| Spotify | the public embed page's preview; for a restricted track, an archived copy of its page | — |

A reference is accepted only when its own artist, title and length match the
library entry, and is cached in `work/refs/`. A source that failed is reported as
a failure, never as "no reference".

**How the comparison works.** The tool slides the reference along the candidate
to find where it sits, then correlates the two waveforms in five-second
segments. Each segment is also scored at two deliberately wrong offsets, so
every number comes with its own background. Measured on known answers:

| candidate | waveform | verdict |
|---|---|---|
| three independent uploads of `Summertime` | 0.97–1.00 | same recording |
| `Summertime` pitch-shifted by −12 cents | 0.04 | rejected, though its loudness contour matched at 0.80 |
| Beck's own mix of `Summertime` | 0.25 | rejected |
| a cover of `Sigma` sung over the original backing track | 0.69 | rejected |
| wrong songs of the right length | 0.01–0.05 | rejected |
| background (wrong offsets) | 0.02–0.06 | |

The bar is 0.85. A candidate the text checks would have parked — another
artist's name, a translated title — is downloaded and judged this way instead,
and imported when it matches. Without a reference, the text rules stand.

**A release can be a cut of something longer, and the tool finds the cut.**
Apple's preview starts at a fixed point of the release — measured on one album:
45s into a track of 75s or more, 30s into a 60–75s track, the whole of a track
under 30s. So where Apple's preview sits in a longer file says where the release
starts; for lengths nobody has measured, the tool does not guess.
`Creelles Town` is a 72s release cut from a 120s piece, and the tool derives
`[0:72.052]` on its own. A cut can also be named in `track_sources.json` as
`"trim": [start, end]`.

**`--audition`** runs all of this for one or more rows, including ones already
done, and stops short of the library: it searches, downloads, compares and
prints what it would import, writing nothing.

```bash
python3 geo-unblock.py --audition --track "Sex Bob-Omb :: Summertime"
```

### What it still cannot reach

"Not found" means "not found by its sources", never "gone". Some of what remains
sits behind something the tool does not do, and it says which:

- **A source that refuses** — HTTP 403/429, a CAPTCHA, YouTube's sign-in wall —
  is reported as a refusal and dropped for the rest of the run, not counted as
  an absence.
- **VK** holds tracks no store does, but its search needs a signed-in session,
  and the tool does not sign in.
- **A store purchase or a stream** it cannot use: it reports when the record is
  in your storefront (one click in Music, and the tool carries the history
  across), in another storefront, or on Deezer only.

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

**Apple may rename the entry.** When iCloud Music Library *matches* an import
against Apple's catalogue (`cloud status = matched`), the library row takes the
store's title, artist and album while the file keeps ours. One import landed as
`Paul Brooks — Theme from "Twin Peaks"` on `Ultimate TV Themes` instead of
`The Imperas — Twin Peaks Theme`. Nothing is lost — the plays and the comment
come across, and a matched track streams from Apple — but the entry is no longer
findable by the name the index holds, so a later run would import it again.
Writing the name, artist and album back onto the track fixes it and sticks.

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

**Do not copy the whole media folder.** On a subscription the catalogue
re-downloads from Apple. Measured 2026-10-16: of 6548 tracks, 6193 are
`subscription` and only 330 are `uploaded` or `matched` — the user's own uploads
plus this tool's imports. Those live under `Media.localized/Music`, and
subscription content under `Media.localized/Apple Music` (53GB), so copying just
the first is the whole job:

```bash
ditto ~/Music/Music/Media.localized/Music ~/Backups/music-$(date +%F)/own-files
```

That came to 9.0GB in 11882 files here — far more files than there are library
rows, because that folder also accumulates orphans Music left behind on earlier
imports. Copying it is still cheap next to the 53GB that comes back on its own.

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
| `--retry STATUS` | process only rows recorded as `error`, `review` or `not_found` |
| `--dry-run` | resolve matches, download and import nothing |
| `--audition` | download and check candidates against the reference; import and record nothing |
| `--track 'A :: T'` | process only this row (repeatable); with `--audition`, rows already done too |
| `--min-score` | confidence below which a match goes to review (default 120) |
| `--no-soundcloud` | YouTube only |
| `--report` | counts, sources, timings, and anything left in review |
| `--repair` | fix artwork, history and comments on tracks already imported |
| `--dedupe` | list library rows that are the same recording twice over |
| `--dedupe --apply` | merge their history onto the oldest row and delete the rest |
| `--retire-originals` | list dead entries whose replacement is already in place |
| `--retire-originals --apply` | lift any history the replacement is missing, then delete them |

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

## Duplicates Music makes on its own

Separate from anything this tool does, Music sometimes re-downloads a
subscription track it already has — writing `Track 1.movpkg` beside the
original — and registers each download as its own library row. The rows look
identical and each counts its own plays, so a song listened to 34 times can show
25, 8 and 1 across three entries.

```bash
python3 geo-unblock.py --dedupe           # list them, change nothing
python3 geo-unblock.py --dedupe --apply   # merge and collapse
```

Rows are grouped on title, artist, album, **track number and duration** — the
first three alone are not enough, because Apple relabels entries and two
different songs can end up sharing all of them. The oldest row survives, since
it holds the real `date added`; the others' play and skip counts are added to
it, not maxed, and any plain playlist that would lose the song gets the survivor
put back. Dead geoblocked entries beside their replacements are left alone —
that pair is what the originals playlist is for.

## Playlists

- **Geo-Unblocked — refetched** — the files brought back.
- **Geo-Unblocked — originals** — "parking". Music usually *merges* an import
  into the existing entry, leaving one track. When it does not, two remain: the
  dead geoblocked one and the new file. The dead one is filed here so it can be
  reviewed and deleted in one pass later.

  Do not empty it by hand, and do not trust that it still holds what it did.
  Once Music merges, a parked reference resolves to the *merged* track, so the
  playlist ends up listing live music alongside corpses. Use
  `--retire-originals`, which decides from `cloud status` rather than from
  playlist membership, and lifts any history the replacement is missing before
  deleting anything. When it is done, the geoblocked list holds only the tracks
  that genuinely could not be rebuilt.

## Requirements

`yt-dlp`, `ffmpeg`, Python 3, and macOS Music with AppleScript automation
allowed. **Keep `yt-dlp` current.** YouTube refused every download with
`LOGIN_REQUIRED` for a whole month on 2026.7.4 and downloads fine again on
2026.08.19, with no cookies and no PO token — so a source that looks dead may
only be an old binary. Reading files inside `~/Music/Music` additionally needs Full Disk
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
