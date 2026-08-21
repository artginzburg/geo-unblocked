# geo-unblocked — working notes

For agents working on this repo. The README says what the tool does; this file
says how it behaves in practice. Nearly everything here was learned by getting
it wrong once, and none of it is guessable from the code.

## Keeping these two files current

**Update both docs in the same change that alters behaviour** — not afterwards,
not when asked. A fix whose reasoning lives only in a commit message is a fix
the next agent will undo.

- **CLAUDE.md** is read by agents. Record invariants, traps, and *why* a rule
  exists. When a defect is found, write down the mistaken assumption behind it,
  not just the correction — the assumption is what will recur. Approaches that
  were tried and rejected belong here too, with the evidence that killed them,
  so they are not reinvented.
- **README.md** is read by people and agents both. Keep it about what the tool
  does, what it cannot do, and how to run it. Practical consequences belong
  here; post-mortems belong in CLAUDE.md.
- Anything asserted in either file should be checkable. Prefer a measured number
  ("13 tracks had inflated counts") over a characterisation ("counts drifted").

## The goal, restated

Not "download the songs." **Replace a dead library entry with a live one while
the library keeps believing it is the same track.** Play history and playlist
membership matter as much as the audio. A replacement that loses 24 plays is a
failure even if it sounds perfect.

## Verify against Music, never against a proxy

The most expensive recurring mistake here: reporting what the script *intended*
instead of what the library *holds*.

- The log said `art=store` for tracks whose artwork was still a padded 480×270
  video frame; Music had silently kept the old file. Read artwork back with
  `raw data of artwork` and measure it.
- Two counters reported successes that never happened, because they incremented
  on the attempt rather than the result. Count what changed.
- A smart playlist's size was read as proof that favourites had been destroyed.
  Its criterion was compound (geoblocked **and** loved), so tracks leaving it
  had merely stopped being geoblocked. Nothing was lost. **Measure the property
  itself, never a number that depends on it.**

## How Music actually behaves on import

- **It merges.** Adding a file whose song already exists usually folds it into
  the existing entry, preserving play counts and playlist membership. Best
  outcome, and it happens for roughly 4 out of 5 tracks.
- **A re-import does not replace.** If our file is already there, Music matches
  the new copy to it and keeps the old one. Re-running changes nothing unless
  the previous import is deleted first.
- **It leaves orphans.** Rather than overwriting it writes `Track 1.m4a`,
  `Track 2.m4a` beside the original. These accumulate and are invisible from the
  library; find them by diffing files on disk against `location` of every file
  track, never by date alone — one file dated the same day was the user's own.
- **Merged entries are indistinguishable from ours.** After a merge our file
  *is* the user's entry, so deleting "our" tracks in bulk destroys their record.
  This has cost real data twice: one track's 2017 add date and 3 plays, and a
  second track that vanished from the library entirely because a hand-approved
  match was not re-added afterwards. Inside the script it is safe, because
  delete and add are adjacent. As a standalone cleanup it is not.

## Two playlists can share a name, and every reference here is by name

Music allows duplicate playlist names and `user playlist "X"` resolves to
whichever comes first. Two `Geo-Unblocked — refetched` playlists appeared in one
session -- iCloud sync is enough to do it -- and imports split between them, 230
in one and 228 in the other, differing by exactly the two most recent tracks.
Nothing reported it: `ensure_playlists()` only asked `exists`, which is true of
both. It now counts and prints a warning when there is more than one. Merging is
destructive and stays the user's call; the diff to check is which persistent IDs
one holds and the other does not.

## The third playlist cannot be built, and the dictionary says so

A live smart playlist tracking `no longer available` is the obvious companion to
this tool — it tells the user geoblocking has struck again without their running
anything. It cannot be created programmatically, and this is settled, not a
matter of not having found the trick: in `com.apple.Music.sdef`, `user playlist`
carries `smart` as `access="r"`, and there is no criteria property, no
constructor argument and no command anywhere in the dictionary. Only the app's
own File → New → Smart Playlist builds one.

UI scripting through System Events does reach the dialog — the menu path
resolves and the window opens as `AXDialog`. It is not worth shipping: the
window answers to `window 1` but not to `window "Smart Playlist"`, `entire
contents` returns nothing, and re-reading the same index raises "Invalid
index" moments later. That is before considering that every rule name in it is
localised. The README documents the four manual steps instead.

If you open that dialog while exploring, close it (`key code 53`) before doing
anything else. It is modal, and leaving it open freezes the user's Music.

## A read-only command must leave the library as it found it

`ensure_playlists()` ran before the command was dispatched, so `--report` --
whose entire job is to look and not touch -- recreated a playlist the user had
deliberately deleted a minute earlier. It is now gated on the run actually
importing something. Any command that only reads (`--report`, `--review`,
`--dedupe` and `--retire-originals` without `--apply`, `--dry-run`) must be
safe to run at any moment, including in the middle of deciding what to delete.

Two more things about playlist descriptions, both learned by writing one:

- **The field caps at 255 characters** and Music enforces it by silently
  truncating mid-word. A description written blind reads as finished prose and
  ends in the middle of a sentence. Read it back and count.
- **`missing value as string` is the literal text "missing value"**, thirteen
  characters long, so a never-set description fails an emptiness test and a
  "write only if empty" guard never fires. Test `is missing value` first.

## Skipping the work is not skipping the bookkeeping

`already_live` -- a geoblocked entry whose song also exists as a playable copy
the user swapped in by hand -- correctly imports nothing. For the whole project
it also *inherited* nothing, and that looked fine, because the plays and the
favourite were still visible on the dead entry sitting right beside it.

It stopped looking fine at the delete. Of sixteen dead entries with
replacements, two had their entire record only on the corpse: Iron Maiden's
"Wasting Love" was carrying twelve plays and a favourite the live copy did not
have. A bulk delete would have taken both, silently, with the summary still
reporting sixteen tidied.

Two rules fell out of it, and both are now in the code:

- **Any branch that returns early still owes the inheritance.** The import
  decision and the history decision are separate questions; answering the first
  with "nothing to do" says nothing about the second.
- **A destructive pass verifies its own precondition, rather than trusting that
  an earlier pass established it.** `--retire-originals` re-reads both sides and
  raises the survivor before deleting anything. That is why the two were caught.

## Music duplicates its own subscription tracks, unprompted

Not everything wrong with the library is this tool's doing, and assuming it is
wastes a session. Music sometimes re-downloads a subscription track it already
has, writing `10 Psycho Killer ( LP Version ) 1.movpkg`, then ` 2.movpkg`, and
creating a **separate library row per download** — same name, artist, album,
duration and track number, each accumulating its own play count. One song was
found three times over, with 25, 8 and 1 plays: 34 real listens split across
rows that all looked like the same entry.

Telling it apart from our own work is one property: our imports are `.m4a` with
`geo-unblocked ·` in the comment, and land as `uploaded`. Music's re-downloads
are `.movpkg`, comment empty, `subscription`.

- **Find them on disk, not in the library.** `find ~/Music/Music/Media.localized
  -name "* [0-9].movpkg"` lists every re-download directly. The library-side
  equivalent — grouping on name+artist+album — needs duration and track number
  to separate real duplicates from different songs that share a title, and see
  the index warning below before joining that data across calls.
- **Merging is a sum, not a max.** The usual raise-only rule exists because
  `--repair` reruns; here each row counted different real listens, so the
  keeper gets the total. Keep the oldest row: it holds the true `date added`.
- **Check playlist membership before deleting.** Smart playlists re-evaluate and
  need nothing, but a plain playlist loses the song when its only member row is
  deleted — re-add the keeper first. Two of the three rows above were in a
  synced Shazam playlist that the keeper was absent from.

## AppleScript traps

- **A subscription track will not hold a written play count while a same-named
  dead twin is still in the library.** Writing `played count` and `skipped
  count` onto the store copy of `Bad Style :: Time Back` read back correct and
  was 0 again seconds later, while `played date` and `comment` on the same track
  held. iCloud reconciles the two entries and the counter loses. Deleting the
  dead entry FIRST and then writing worked on the first try and stuck. The same
  transfer onto `Geek Music — Borderlands 2 …` held immediately, because there
  the store title differs from the library's and no twin existed. So the order
  is: delete the dead entry, then carry the history onto the replacement — the
  opposite of the order used for our own imported files, where the dead entry is
  the only thing holding the record. `data/list.tsv` is what makes deleting
  first safe: it already holds the counts.
- **Verify a write after a delay, never by the readback in the same script.**
  Every reverted write in this project read back correct at the moment it was
  made. An immediate `return played count of t` proves the value was accepted,
  not that it was kept.
- **Never write to a track that is playing.** Music overwrites the metadata of
  the current track with its own playback bookkeeping, so a scripted write lands,
  reads back correct, and is gone a minute later. Diagnosed the long way: a
  played count set to 52 read back as 52 and was 50 again on the next check.
  Check `player state` and `current track` before writing, or write while
  stopped.
- **The snapshot-list/live-index trap bites one-off scripts too, not just
  `build_index`.** An ad-hoc AppleScript that bulk-read `name`/`cloud status`
  and then did `set t to track i of src` to write a play count sent the write to
  a neighbouring track -- `Sex Bob-Omb :: Summertime` went from 38 plays to 52,
  found only by listing tracks whose `modification date` was today. Addressing
  by `persistent ID` cannot miss; index arithmetic can. This was written hours
  after fixing the identical bug in `build_index`, in a throwaway snippet
  nobody reviewed.

- **`date added` is read-only.** `set date added` returns `Unknown object type`
  by every route tried. Not a bug to work around — write it into the comment.
- **`favorited`, not `loved`.** `loved` raises a descriptor mismatch on current
  Music. Both these calls sat inside a `try` that swallowed the error, so the
  flag was never carried and nothing reported a failure. **A silent `try` around
  a write is how a feature stays broken for a whole project.**
- **`current date` inside `tell application "Music"`** is captured by Music and
  fails the same way. Build dates outside the tell block.
- **Deleting while iterating invalidates indices.** Collect `database ID`s
  first, delete after.
- **Park the original before importing.** During a merge the dead entry briefly
  stops being findable, so a park that runs afterwards silently finds nothing.
- **Writable:** `played count`, `played date`, `skipped count`, `favorited`,
  `rating`, `comment`, `composer`, artwork via `set data of artwork 1`.
  **Not writable:** `date added`.
- **Copying a date object between tracks fails in one expression.** `set played
  date of a to played date of b` raises -10006; `set d to played date of b` and
  then `set played date of a to d` succeeds. The property is writable — it is
  the nested specifier that Music refuses.
- **`year`, `month`, `day` are Music's own track properties**, so `year of d`
  inside `tell application "Music"` asks Music for a track's year and fails on a
  date object. Pull date components apart in a handler defined outside the tell
  block and call it with `my`.
- **`before` is a reserved word** (`insert … before`). `set before to …` does not
  compile — and the caller returned `"0"` on any non-zero exit, so a script that
  never ran once looked exactly like "there was nothing to do". Parking was dead
  for the entire project because of this pair. **Never let a failure and a zero
  share a return value.**
- **`whose` under-returns, silently.** `every track whose cloud status is no
  longer available` returned 214 tracks while walking the same library and
  comparing the status text found 215. The missing one reports `database ID`
  10319 when enumerated and resolves to a different id when looked up, so
  Music's own index disagrees with itself. **Never trust a `whose` filter as a
  complete set** — bulk-read the property and walk the matching indices.
- **Track indices are not stable between calls.** `track 5676 of library
  playlist 1` is a different song in the next `osascript` invocation — the
  library reorders on its own, without the library changing. So *correlating
  data across calls by index silently fabricates rows*: a dump of names joined
  to a later dump of durations produced a dozen convincing "same title,
  different song" findings, every one an artefact of the join. Read every
  property you intend to compare **in one call**, or key on `persistent ID`.
  **Not even within one script, once that script is slow.** `build_index` used
  to bulk-read `cloud status` and then resolve `track i of src` live for each
  match — a snapshot list joined against a live index across ~60s of walking.
  Music reorders during it. Measured 2026-08-20: one rebuild wrote four playable
  subscription tracks into the list (Justice & Tame Impala, Nicolas Snyder,
  Serum 114, Sevdaliza) and left out four genuinely dead ones (Bass Battle,
  Universal Theme, Garbage Truck, Sex Bob-Omb's Threshold) — four false
  positives and four tracks with no backup row, from one call. **A snapshot and
  a live read are two calls no matter how they are spelled.**
- **A list of specifiers cannot be bulk-read.** `get name of (every track whose
  …)` fails; only `get name of every track of <container>` works. So filtering
  and bulk-reading cannot be combined — read the whole container, then index.
- Per-track property reads over a whole library are slow (~50s for 15 fields on
  215 tracks, far worse on 6500) — but the cure is **bulk-read every field you
  will emit**, not "bulk-read the selector and then read the rest per track".
  That halfway version is what misaligned `build_index` above. Sixteen bulk
  reads over 6489 tracks take **1.0s**; the per-track walk they replaced took
  ~60. Bulk is both the correct answer and the fast one.
- **Numbers and dates come back in the system locale.** `duration` arrives as
  `195,906` with a comma. A date formats as "Wednesday, 20 December 2023 at
  04:43:51", which no fixed parser survives — emit `year`/`month`/`day`/`hours`/
  `minutes` as separate components and assemble them outside AppleScript.
- Apple's own export **truncates** durations (195.906 → 195). Anything building
  an equivalent index must truncate too, or matching scores shift by a second.

## Verify along the path you changed

A range replace that rewrote `inherit_history` also deleted
`apply_export_history`, which sat between it and the next function. Two call
sites remained, so every real run would have died on `NameError`. It was
committed because the only thing run afterwards was `--dry-run`, which never
reaches that function. **Exercise the code path the change touches, not a
cheaper one nearby.** `--dry-run` proves nothing about import, history or
artwork.

## Human-vouched sources are exempt from heuristics

Requiring artist evidence in `score()` was right, and applying it everywhere was
not: entries from a named release carry no uploader at all, so the check quietly
rejected the very compilation it had been told to trust, and five tracks lost
their artwork. Anything reached through `track_sources.json` or
`album_sources.json` passes `vouched=True`. Demoting a human verdict is the same
defect as forgetting it.

## The storefront is configuration, and it is not the OS region

`AppleLocale` is a macOS setting. The Apple Media Services storefront follows
the **Apple ID's country**. Reading `en_US@rg=byzzzz` as "the Belarusian store"
ran a whole availability survey of 27 tracks against a catalogue that was not
the user's, while his own share links said `/ge/`. Music does not expose the
account store to AppleScript and it is in no readable preference, so it cannot
be detected — `STORE` is an env var (`GEO_UNBLOCKED_STORE`), defaulting to `us`
because that is what an omitted `country` has always silently meant, and 135 of
229 covers were resolved that way.

**How much the storefront actually matters, measured** on the 14 tracks still
dead on 2026-08-20, across `ge`/`by`/`us`: 11 identical. Two apparent
differences were HTTP 403s that rechecked as real absences. **One was genuine**
— `Sex Bob-Omb :: No Fun (Bonus Track)`, exact artist, exact title, 134s against
134s wanted, streamable in `us` and in no other store tried. So it is rare, real,
and worth querying more than one store for. `itunes()` now merges the configured
store with `us` rather than falling back when the first is empty: a storefront
answering with the *wrong* record is not an empty answer, and `ge` does exactly
that for this track. `limit` went 8 → 25 for the same reason — the exact match
sat below the cut, and a truncated list reads as an absence too.

Apple 403s an address that queries too fast, and keeps doing it for the rest of
a session. Retries do not beat that, so the refusal is printed. An unlogged 403
becomes "this record has no cover".

## A store's absence is not the artist's absence

"Not in iTunes" was once read as "this artist does not reach stores" — of a
track sitting in the user's Apple Music library, which is where the whole
project starts. Every track here was distributed; if a lookup misses, the query
is wrong or the storefront differs.

**The library's artist is often not the store's artist**, and no
artist-keyed query can bridge that. `No Place for No Hero (From "Borderlands
2")` is credited in the library to `FPS Allstars`, an aggregator; the store
publishes it as **Geek Music**, streamable, 237s against 240s. Every
artist-keyed probe reported "not in the store" — a false statement about a track
that was one click away, and a human found it in a browser in seconds.

Dropping the artist is not the fix. Title-only queries over the same 14 tracks
returned Mylaneil for "Time Back", Nona Hendryx for "Confidential Liar", J No
for "Can't Do It Like Me", Vólan for "Такие дела", Secret Garden for "Sigma" —
right title, right length, wrong song every time. The fix is to *report* a
title+duration store candidate as something for a person to judge, never to
accept it and never to print "not in the store" while one exists.

Queries by `artist + title` fail on common words: "DJ Quads Friends" returns The
Killers and Nickelback. Searching the artist's catalogue instead
(`attribute=artistTerm`) returned all 178 of their tracks — proving the artist
is there — while the specific release was simply not in that storefront.
Spotify had it, reachable through oEmbed with no account.

## Verified discovery beats a trusted caption

Spotify covers can be reached with no account: search for the release, take the
page URL out of the image results, resolve it through oEmbed, and accept it only
when Spotify's own title matches. The earlier instinct — trust the caption
printed beside the image — is what the third near-miss came from; captions
routinely name a different track from the same album.

The mechanism is verified end to end (it resolved Elvis Costello's "Beautiful"
unaided). Its limit is DuckDuckGo's recall on obscure releases: two tracks whose
Spotify pages exist were not surfaced at all across four query shapes and two
result pages. Google finds them, and blocks scraping. So this source helps where
it can and the hand-named map covers the tail — do not mistake "not found" here
for "no cover exists".

## Order the index deterministically

`--start/--limit` address rows, so any change to row order silently changes what
a slice means. Rebuilding the index once reordered it and a 30-track run walked
22 tracks nobody had agreed to. Sort by artist then title, always.

## Finding a Spotify link: what works, and its ceiling

Some releases survive only on Spotify — a track is unavailable precisely because
Apple removed it, so no Apple lookup can return it, and Deezer, Genius, JioSaavn
and MusicBrainz do not carry the long tail either. Spotify's API is behind
Premium; its oEmbed endpoint works with no account but needs a track link.

**Brave is the only search engine that serves those links in plain HTML.**
Measured, all returning zero: DuckDuckGo html and lite, Bing (including its
base64 `u=a1…` redirects decoded), Mojeek, Marginalia, Startpage, Ecosia,
Yandex. DuckDuckGo's *image* endpoint does work as a second pass, since results
carry their source page.

**Brave rate-limits after a handful of queries** — HTTP 429 with a CAPTCHA page
that parses as "no results". So it cannot be the primary route for a 200-track
pass, and the status code must be checked or a block reads as an absence. For
the tail, `artwork_sources.json` stays the reliable answer.

Whatever is found is resolved through oEmbed and accepted only when Spotify's
own title matches. Never trust the caption beside an image or a search ranking.

## A network failure must never look like an absence

Every artwork source ends in a download, and an unretried one turns a blip into
"не нашлось" — the same message the run prints when no cover exists anywhere.
The two are indistinguishable in the output, so nobody investigates.

This cost the same track its cover twice: a person found the link by hand, it
was recorded in `artwork_sources.json`, and the run still reported no artwork
because a single `curl` came back empty. `fetch_image` now retries for every
source, and `override_art` retries its oEmbed lookup.

The general rule: when something is fetched over a network, a failure and an
absence must not produce the same result.

## Anything that runs twice must be idempotent

`--repair` is expected to be run repeatedly, so every operation it performs has
to converge. `inherit_history` added the dead entry's play count to the current
one — correct once, wrong on every later pass. One track reached **174 plays
against an export figure of 24**, and 13 tracks were inflated before anyone
looked. It now raises to the larger of the two.

Applies to anything else added: prefer "set to max", "set if empty", "replace"
over "add" or "append".

## Matching

**A qualifier the library carries is optional unless it names a rendition.**
`No Fun (Bonus Track)` and a source's `No Fun` are the same 134s master, but
scoring against the full title charged the candidate for two missing words and
left it at 119 against a bar of 120 -- the last track of that soundtrack the
tool could not recover, for a reason that had nothing to do with the audio.
Dropping every qualifier is the opposite mistake: strip `(8 Bit)` and a plain
`Threshold` outscores the 8-bit arrangement the library actually holds. The
`RENDITION` list decides which kind it is, and the same list penalises a
candidate that advertises one.

**Query the base title as well.** `pick()` built four queries and every one
carried `(Bonus Track)`. Mail.ru returns nothing for
`Sex Bob-Omb - No Fun (Bonus Track)` and the right 134s file for
`Sex Bob-Omb - No Fun`, so the provider looked empty while holding the track.
`base_title()` had existed since the beginning -- used only for store artwork
lookups, never for audio search.

**Quality cannot be ranked before downloading, so rank the source.** SoundCloud
publishes no bitrate at all; Mail.ru publishes a wrong one -- it advertised 128
for a file that arrived at 240. Measured over the twelve recoveries of
2026-08-20: Mail.ru delivered 271-424kbps, SoundCloud 160-163k. Hence
`PROVIDER_QUALITY`, deliberately small: it settles a near-tie and must never
outweigh duration or artist. Without it `No Fun` imported at 96k while a 240k
copy of the same master sat one rank below, against a 256k original.

**When metadata cannot decide, ask the audio -- by comparing candidates to each
other.** A radio rip with an announcer talking over the intro passed every check
this tool has: 134.6s against 134.5s wanted (the *best* duration match of nine
candidates, because radio pads the ends) and 245kbps. Downloading all nine and
correlating a 200ms RMS envelope with a lag search separated them instantly:

| pair | similarity |
|---|---|
| `Боль Новая` (mr, 241k) ↔ `Alan Lugo` (sc, 95k) | **0.92** — both the radio rip |
| `Scott Pilgrim Music` ↔ `h3artsick`, both "Official version" | **0.93** — the clean master |
| across the two clusters | 0.24 |

Both 134s candidates the tool chose, on two separate attempts, were the same
contaminated recording. The clean master runs 130.7s -- **3.8s shorter, so the
duration rule actively preferred the bad one.** Two independent uploaders
agreeing at 0.93 is evidence no title carries. Not in the code yet; the open
questions are when to spend the downloads and what to do when the vote splits.

**Measure the file that arrived. Always, not conditionally.** Every duration
decision before the download is made on metadata the *source* supplied. The
silence measurement used to run only when the advertised length was already
outside tolerance — which is exactly the case where the source is being honest
about being different. When it lies, nothing looked.

What that cost: a Mail.ru entry advertised 121s against a library entry of 121s,
carried the right artist and title, scored 142, and imported. The file was
181KB — **12kbps, 4.4 seconds of audio**. It sat in the library as the
replacement for a track with 50 plays until the user played it and asked what
the hell had happened. The check now runs after every fetch, before tagging and
before anything is parked or deleted; a mismatch raises and the loop moves to
the next candidate. Only a `manual` source is exempt, and it prints a warning.

This one was found, named, and deferred before it fired. It was written up in
this session as "the duration gate is applied to the candidate's advertised
length, not to the delivered file", then left alone as "a separate change" —
and a brand-new provider was opened through it ten tracks wide the same hour.
**A known hole is not a smaller hole for being known.**

Duration is the only trustworthy signal. Titles lie constantly — "(lyrics)",
"(Official Audio)", live rips and sped-up edits all read as the real thing. A
live rip of Akon scored respectably on text alone and was caught only by length.

**The duration rule is asymmetric.** Shorter than the original means audio is
missing and can never be right; longer still contains the whole recording. Short
is rejected outright, long is penalised and allowed as a second-pass fallback.

**Measure the audio, not the container.** A candidate a few seconds long is
often the same recording with padded silence. `silencedetect` settles it: one
track ran 206s in the file and 198s of actual sound against an expected 195s.

The Apple export **romanises Cyrillic artists** ("Akvarium", "Aleksey Vishnya"),
so artist-qualified queries miss entirely for Russian material. Both the matcher
and the artwork search fold Cyrillic to Latin, and image search additionally
queries using the source's own title, where the native spelling survives.

`- Topic` channels are label-delivered audio and outrank everything else.

**On SoundCloud an uploader name is self-declared, and worth nothing.** The
`- Topic` rule works because YouTube generates those channels from label
deliveries. SoundCloud has no equivalent: anyone may name an account anything.
A fan cover of Sex Bob-Omb's "Threshold" was uploaded by an account literally
named `Sex Bob-omb`, so `tokens(artist) & tokens(uploader)` matched, the length
was 104s against 107s wanted, and it scored **148** and auto-imported. The user
played it and said it was a cover or a home recording. `We Are Sex Bob-Omb`
went the same way at 137, from a private account.

**Engagement is the provenance SoundCloud does have, and it separated these
cleanly.** Measured on the three imports of 2026-08-20:

| upload | plays | likes | verdict |
|---|---|---|---|
| `Lyric McKissack` — We are sex bob-omb | 23 | 2 | amateur, removed |
| `Martin Nebritsevsky` — sex bob omb garbage truck | 46,927 | 876 | a rip of the film audio |
| `Two Feet` — Go Fuck Yourself | — | — | the artist's own account |

Twenty-three listens is not a distributed recording. Both numbers are in the
yt-dlp JSON already (`view_count`, `like_count`) and neither is consulted.

**Verify the artist, every time.** Three near-misses came from matching a title
and a duration and nothing else: a different artist's "Лодка" with a convincing
Apple cover, a "Nuvole Bianche" from a baby-sleep compilation two seconds off,
and an "American Funeral" seven seconds off by an unrelated band. Common titles
and common lengths are not evidence.

## Human verdicts must outlive the run

Scoring cannot rank everything. When a person approves a low-scoring match, that
decision goes in `data/track_sources.json`, applied ahead of every search with
no score gate. This is not a convenience: a verdict held only in conversation
was lost on the next clean run, which re-parked the track in review — and
because its entry had merged, the delete preceding that run took it out of the
library altogether.

Same shape, different scope: `data/album_sources.json` names a whole release,
`data/artwork_sources.json` names one cover.

## Fields, and which ones are real

Carried: play count, last played, skips, favorited, rating, composer.

**BPM is not missing — it was never there.** Zero of the 220 geoblocked entries
and zero file tracks in the whole 6500-track library have it set. Apple does not
populate the field and the export has no column for it. Do not go looking.

`grouping` is likewise empty everywhere. `composer` is the one that mattered: 17
of the 241 tracks have one, and it was being dropped until noticed.

## Back the library up before a full pass

A slice touches thirty entries and a mistake is repairable by hand. A full pass
touches every broken track in the library, and this project has already lost
data twice in ways nobody noticed until much later. Before running the whole
list, take both:

**Do not copy the media folder.** With an Apple Music subscription the catalogue
re-downloads from Apple; copying it is hours of I/O for nothing. Measured here:
of 6519 tracks, 6370 are subscription content (56.9GB) that comes back on its
own, and only 149 are `uploaded`/`matched` — the user's own uploads and this
tool's imports. Their files total **0.41GB in 85 items**. A blind 17GB copy was
roughly 95% waste.

Two things are irreplaceable, and both are small:

```bash
# 1. the library database — quit Music first, or the snapshot may be inconsistent
osascript -e 'tell application "Music" to quit'
ditto ~/Music/Music/"Music Library.musiclibrary" ~/Backups/music-<date>/"Music Library.musiclibrary"
open -a Music

# 2. only the files Apple cannot re-serve: cloud status uploaded or matched
#    (walk file tracks BY INDEX -- iterating a filtered list yields specifiers
#     whose properties will not read, and a `try` around it returns nothing at all)
```

The database carries play counts, playlists, favourites and add dates — the part
no download restores — at ~96MB.

Keep the backup **outside** `~/Music`. A few thousand audio files inside the
media folder invite Music and LaunchServices to index them, which is its own
class of trouble (see the stray-bundle problem in the user's global notes).

## Snapshot before you destroy

`--refresh-index` only sees tracks Music still reports unavailable, and a
successful replacement is exactly what removes that status. So a heart the user
added to an already-replaced track lives ONLY in the library — and a clean rerun
begins by deleting those very entries. `--snapshot` folds favourites and counts
from the refetched playlist back into the index first. It raises, never lowers.

Run order for a clean pass, and not in any other order:

```bash
python3 geo-unblock.py --snapshot        # capture what only the library knows
python3 geo-unblock.py --refresh-index   # pick up anything newly broken
# ... then delete our imports, clear the playlists, reset state, and run
```

Measured on the first attempt at this: exactly one track (`Шприц`) was hearted
in the library and unrecorded in the index. Without the snapshot the clean run
would have eaten it, and nothing would have reported the loss.

## The index is the backup

**`purge_ours` now folds what it destroys back into the index**, because the
`--snapshot` rule below relies on somebody remembering it, and across one long
session nobody did. A favourite added to an import after the last snapshot lives
in exactly one place -- that file -- and `purge_ours` is what deletes it;
`apply_export_history` cannot restore it because it is set-only and the index
still said `false`. `No Fun (Bonus Track)` was purged and re-imported three
times and lost its heart on the first pass, silently. The function now reads
`favorited` and `played count` off each track it is about to delete and raises
the index row with them. Verified by staging the exact failure: index forced to
`false`, favourited copy purged, index came back `true`, and the re-import
restored the heart with no manual step.

`data/list.tsv` is not just an input list. It holds `Plays`, `Last Played`,
`Skips`, `Date Added` and `Favorited` for every track, and it is the *only*
surviving record once a library entry is gone. It has already been used to
restore history that was destroyed. **Refresh it before anything destructive.**

It is built by `--refresh-index`, reading Music directly: the rule behind the
"geoblocked playlist" is a track property, so no playlist is needed — and none
could be created, since Music's scripting interface cannot give a playlist
rules. `--playlist NAME` narrows the scan when a subset is wanted.

An Apple export dropped in at the same path still works, but is strictly worse:
it has no `Favorited` column, and that flag has no other source. It cannot be
recovered from anywhere once the dead entry is gone.

## Sources, in practice

- **Mail.ru was never broken. Our URL scheme was.** For the whole project this
  file recorded "Mail.Ru answers search with file URLs that 404" as a property
  of the source. It cost twelve tracks in a single pass, and `pick()` returns a
  ranked list instead of one winner specifically to survive it. The truth:
  `moosic.my.mail.ru` serves **nothing** over plain `http` — 404 from nginx, no
  redirect — and serves the file over `https`. Same path, same id. The first
  one tried returned a 320kbps mp3 of exactly the right length.
  yt-dlp is not at fault either: `mailru.py:219` is `audio_url = t['URL']`,
  taken verbatim from Mail.ru's own API, which hands out an `http://` URL its
  own CDN refuses. `search()` now upgrades the scheme.
  **What it is, once reachable: a user-upload catalogue, and its tags are typed
  by whoever uploaded.** Title, artist and duration are not the file. Searching
  one track returned two different uploads claiming the same recording, at 121s
  and 58s, and the 121s one held 4.4 seconds of 12kbps audio. Nine of twelve
  recoveries came from here and nine are good — but an exact duration match from
  Mail.ru is a match against a text field, so the delivered file decides.
  **The lesson is the expensive part.** A source whose search returns correct,
  exact-duration metadata and whose every file 404s is not a dead source — that
  combination is nearly impossible. It means the link is wrong or the contract
  changed. Zero of 221 successful imports had ever come from this provider, and
  that number was visible the whole time; it was read as "the source is bad"
  instead of "we have never once succeeded here, which is suspicious." **A
  provider with a 0% delivery rate is a bug report, not a fact about the world.**

- **State of the sources on 2026-08-20**, and the shape of the failures matters
  more than the list: **YouTube** searches fine and refuses every download with
  `playability status: LOGIN_REQUIRED` — verified across every player client
  (`tv`, `ios`, `mweb`, `tv_embedded`, `web_safari`, `android_vr`), on videos
  that downloaded successfully in the previous pass, on yt-dlp 2026.7.4 with
  nothing newer in Homebrew. **YouTube Music** is the same extractor and the
  same wall. **SoundCloud** works. **Mail.ru** works too — see below; it spent
  this project looking dead for a reason that was ours.
- **A PO-token provider does not fix `LOGIN_REQUIRED`.** bgutil 1.3.1 was
  installed and works — `node build/generate_once.js` mints a valid token — but
  yt-dlp requested one **zero** times, because the refusal lands at the
  playability stage, before any token is relevant. PO tokens sign the request
  for format URLs; they cannot answer a demand to sign in. Only cookies can.
  Plugin at `~/.config/yt-dlp/plugins/`, server at `~/bgutil-ytdlp-pot-provider`.

- **JioSaavn's "320" is a tier label, not a promise.** Requesting format 320
  explicitly still returned 98kbps for one track while another from the same
  album arrived at an honest 324. Check what came back.
- **JioSaavn search is useless for Western catalogue.** "Gorillaz White Light"
  returns nothing, "Fall Out Boy 27" returns karaoke, "2 Ton Truck" returns
  Hindi songs containing "To". No hit there means the search failed, not that
  the record is absent — so a library cannot be surveyed through it. The album
  route is the only reliable way in.
- **YouTube Music indexes Art Tracks plain YouTube search does not**, and gives
  structured artist/album/track fields. That is how a release published under a
  translated title was found: "Капитан Джек Воробей" is up as "Capitán Jack
  Sparrow" and matched on artist and an exact duration despite sharing no words.
- **Spotify's Web API is gated behind Premium**, so its search source is
  effectively unavailable. Its public oEmbed endpoint needs no account but works
  only from a track link. The unofficial web-player token endpoints are blocked
  and their use breaches Spotify's Developer Terms — not an option.
- **Production-music libraries** (Bosshouse/Transphonic here) license rather
  than distribute; only showcase compilations are public, and Apple's copy may
  be a 90-second edit that exists nowhere else. Fifteen of the 241 tracks come
  from one such compilation.

## Performance

Every search is a `yt-dlp` network round-trip, and with five source paths and
four query variants that reaches a dozen calls per track. Providers are
therefore **strict fallbacks**: stop the moment a match clears the auto-import
threshold. Getting this wrong cost 26s per track; fixing it brought the median
to 7s. Per-track timings live in `data/state.json` as `secs` and `--report`
prints them.

**But "found" is not "obtainable", and the early stop conflated them.** A
provider that can search and cannot deliver turns the optimisation into a total
loss. On 2026-08-20 YouTube answered every download with `LOGIN_REQUIRED`; a
YouTube candidate scored 139 on the first provider, the search ended there, all
four candidates failed to download, and the run moved on — **0 of 27 tracks
across two full passes, while SoundCloud held downloadable copies nobody ever
queried.** `pick()` now takes `exhaustive=True` for a second round, run only
when every candidate failed to *download*; the gate is `not tried`, never the
score, because a LOW CONFIDENCE reached only after the better candidates refused
says nothing about the sources never asked.

**A refusal is not a failure, and must not be retried like one.** `Sign in to
confirm`, `Private video`, `removed by the uploader` are the source answering.
`fetch()` raises `Refused` and stops as soon as *every* variant has refused —
"every", because the embedded-player variant exists precisely to pass walls the
default client hits, so one refusal is not the final word.

**Drop a refusing provider from the SEARCH, not just from downloads.** Doing
only the latter left the wall-clock almost unchanged (112s/track against 115s),
because the cost was never the downloads. Measured per query: `yt` 1.3s, `sc`
1.0s, `mr` 1.7s, **`ytm` 10.4s returning nothing** — it resolves each hit with a
second call, and that call is what the wall refuses. Four queries a track made
`ytm` alone 42s of a 104s median. Refusals are counted once in `REFUSALS`,
shared by `search()`, `search_ytm()` and `fetch()`, and a source is out after
three. `search()` returning `[]` now checks stderr first: an empty answer and a
refused one are indistinguishable from the call site, and only one of them means
the source has nothing.

## Working agreements with Art

- **Do not ask whether to run the full pass.** It happens when Art says so; a
  slice is the development set. The target is all of the slice, not most of it.
- One change at a time; report a latent fragility once rather than pushing it.
- Nothing personal goes in the repository, and `.gitignore` is not evidence
  that it did not: `git check-ignore -v <path>` is. One rule carried a trailing
  comment, which git does not support, and 241 tracks of listening history sat
  in the history behind a line that looked like it excluded them.
- Commit locally so GPG signing applies. Never write files through the GitHub
  Contents API.
- Long-running work goes in the background with a waiter. `pgrep -f "name.py"`
  matches the waiting shell's own command line; write `pgrep -f "name[.]py"` so
  it cannot match itself.
