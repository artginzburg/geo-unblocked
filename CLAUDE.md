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

## A `matched` import is renamed by Apple, and the file is not

`tag()` writes the library's own title, artist and album into the file before
import, so the entry should keep its identity. It does not always. When iCloud
Music Library **matches** the audio against Apple's catalogue, the library row
takes the STORE's metadata and the file keeps ours. Measured 2026-10-16 on
`The Imperas :: Twin Peaks Theme (From 'twin Peaks')`:

| | artist | title | album |
|---|---|---|---|
| file on disk (`ffprobe`) | The Imperas | Twin Peaks Theme (From 'twin Peaks') | Back Massage |
| library row | **Paul Brooks** | **Theme from "Twin Peaks"** | **Ultimate TV Themes** |

`cloud status` was `matched`, and Music had even filed the file under
`Media.localized/Music/Compilations/Ultimate TV Themes/`. The history came
across correctly (15 plays, the comment), so nothing was lost -- but the entry
is no longer findable by the name and artist the index holds, which is what
`already_live`, `inherit_history`, `purge_ours` and `park_original` all key on.
A later pass would treat the track as missing and import it again.

Incidentally this is more evidence for the pseudonym story below: the same
master is `The Imperas`, `Spirit`, `The One World Ensemble` AND `Paul Brooks`,
and Apple's own matcher knows them to be one recording.

**So verify the library row after an import, not just the file.** Writing
`name`, `artist` and `album` back onto the matched track restores the identity
and is accepted; per the rule below, check it again after a delay rather than by
the readback in the same script.

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

## Apple re-credits artists, and `already_live` goes blind when it does

`already_live()` asks Music for a track whose `name is X and artist is Y`, both
exact, and its whole job is to stop the tool putting a 128k download next to a
working 256k stream. On 2026-10-16 it did the opposite for two tracks, because
**Apple had re-credited them between passes**: the index rows say `АИГЕЛ`
(Cyrillic, as the library once held them) and the live subscription copies are
now credited `AIGEL` (Latin). Exact match fails, `already_live` returns false,
and the tool imports a second copy of a song that plays perfectly — measured:
`AIGEL · Потанцуем-помолчим · subscription · 241.6s` sits in the library beside
`АИГЕЛ · Потанцуем-помолчим · uploaded · 238.6s`, our file, with our comment.

The matcher already folds Cyrillic to Latin for exactly this reason; the
*liveness probe* does not, so the two disagree about whether a track is the same
song. Two defects in one line, in fact — it is also a `whose` filter, which this
file has already measured as under-returning silently (214 against 215). The
repair: one bulk read of `name`/`artist`/`cloud status` per run (3.1s over
6550 tracks), title compared through `norm()`, artist by shared tokens minus a
few stop-words (`the` alone must not join The Smiths to The Decemberists) —
the same folding the scorer uses. Checked both ways: all **81** rows the old
probe had found `already_live` are still found, both `АИГЕЛ` rows now are, and
`MARU NARA`/`Dead Confederate` still are not; then the real loop was run on the
two reopened `АИГЕЛ` rows and recorded both `already_live` without importing.

## A stale `cloud status` makes a working track look dead

`build_index` selects on `cloud status is no longer available`, and that is the
right rule — but Music does not always clear the field. `Sex Bob-Omb ::
Summertime` reports `no longer available` while being a `file track` with a real
location (`…/Media.localized/Music/Sex Bob-Omb/…/18 Summertime.m4a`, 130.011s at
325kbps, our comment on it) — the import merged into the dead entry, attached
the file, and the status never moved. So the track plays, and the index reports
it dead on every rebuild.

It cost a wrong diagnosis in the same session: read through a broken `POSIX path`
expression (see the AppleScript traps), the location came back empty and the
entry was written up as "the import never attached a file". Both halves were
wrong. **Before concluding a track is dead, read its location with `get` and
look at the file.**

## `purge_ours` deleted inside a `whose` loop, and reported a delete it did not make

Called by hand on `$uicideBoy$ :: N****z` it returned `1` -- one track deleted,
history folded into the index -- and the file track carrying our marker was
**still there** afterwards, at the same persistent ID. Deleting it by persistent
ID worked on the first try. The function does
`repeat with t in (every file track … whose name is … and artist is …)` and
calls `delete t` INSIDE that loop, which is both traps this file already lists:
`whose` under-returns, and deleting while iterating invalidates the specifiers
the loop is holding. In a destructive function.

The library was checked for collateral damage, and there was none: 6550
persistent IDs before and 6550 after, with 14 gone and 14 added, every one of
them accounted for by this session's imports. So the failure here was a no-op
and a false report, not a wrong delete -- but the shape of the bug allows a
wrong delete, and the return value cannot be trusted either way.

**Fixed** the way this file prescribes for every other delete: one bulk read
picks the targets by `persistent ID` (and takes their favourite and count for
the index first), a second script deletes exactly those IDs, and the number
returned is what a fresh read says is gone — a target that survives raises.
Exercised on a throwaway: a 3s silent track with our marker was added, purged
(returned 1, a fresh read found 0), purged again (returned 0), and its orphaned
file removed from the media folder by hand.

### And a merged entry has no original left behind it

`purge_ours` on `Gin Wigmore :: Oh My` deleted the single row the library held
and the track was then **gone from the library entirely** -- no dead original
underneath, because the earlier import had merged into the user's entry and
become it. `park_original` only *duplicates* the dead entry into a review
playlist, so for the Decemberists and the rest the original survived the purge;
here there was nothing to survive. The index kept the history (1 play), and the
recovery was to add the track back from the store.

So: **before purging an import, count the rows.** One row means purging removes
the track, not the replacement.

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
- **Merging is a sum, not a max** — *for Music's own re-downloads.* The usual
  raise-only rule exists because `--repair` reruns; there each row counted
  different real listens, so the keeper gets the total. Keep the oldest row: it
  holds the true `date added`.
- **But the sum is WRONG when the duplicate is one of our imports**, and
  `--dedupe` does not tell the two cases apart. Our import gets its play count
  written onto it by `apply_export_history` from the index — the *same* listens
  the original already counted, not new ones. Measured 2026-10-16: `AIGEL ::
  Пыяла` had the live subscription row at 2 plays and our duplicate import at 2
  plays, and `--dedupe` proposed "итог: 4 просл." A genuine re-download pair has
  two independent histories; a row carrying our `geo-unblocked ·` comment has a
  copy of the other one's. The discriminator is already in the comment, and
  `dedupe` did not look at it. **Fixed:** for a row carrying our marker only
  what was played *on* it counts — the index says what it was given — so the
  keeper gets `max(its own count, the index's) + plays gained on our copy`. The
  floor matters: the first version of the fix took the user's own upload of
  `Meek Mill :: Im a Boss` at 23 plus nothing and lost a listen against the
  index's 24. With no index row the larger count wins rather than inventing
  listens. Music's own re-downloads keep the sum.
  `--dedupe` also grouped on exact duration, so it missed the second pair
  entirely — `Потанцуем-помолчим` was 241.6s live against 238.6s imported. Our
  imports now get a second, looser pass: same folded title, a shared artist
  token, length within six seconds — loose is safe there because one side
  carries our marker. A kept store copy is no longer "returned" into the
  `refetched` playlist, which is for our files. Read-only over the whole library
  this finds two real pairs and nothing else; `--apply` itself has not been run
  since the change.
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
  **The twin is not the only cause.** On 2026-10-16 `Gin Wigmore :: Oh My` was
  added from the store minutes after its only other row had been deleted — one
  row in the library, no twin — and the same thing happened: `played count`
  written as 1 read back 1, and was 0 two minutes later, while `played date`
  and `comment` held. Written again a few minutes after that, it held at 1 on a
  check 150s later. So a freshly added subscription track gets reconciled with
  iCloud's counter once, twin or not. **Write, verify after a delay, and expect
  to write the count a second time.**
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
- **`location` is the one field that cannot be bulk-read.** `get location of
  every track of library playlist 1` raises -1728 ("Can't get location of every
  track of…") while the sixteen fields `build_index` reads come back fine. So the
  "bulk-read every field you will emit" rule has exactly one exception, and
  anything needing file paths must walk live — reading *every* property it
  compares off the same live specifier, never joining a live location to a bulk
  status snapshot.
- **`POSIX path of (location of t)` silently returns nothing; `POSIX path of
  (get location of t)` works.** Without the `get`, AppleScript sends Music the
  whole chain and then tries to coerce a *reference* — the error reads `Can't
  make «class pLoc» of «class cFlT» id 34045 … into type Unicode text`, which
  looks like "this track has no file" and is nothing of the kind. Measured on one
  track, all four forms: no-`get` **ERR**, `get location of t` ✓, `get location
  of track 5 of src` ✓, `set L to location of t` then `POSIX path of L` ✓.
  Inside a `try` this is invisible: a walk of all 6548 tracks returned **zero**
  uploaded files while a bulk status read said 330, and the only symptom was an
  empty file. It also produced a wrong diagnosis in the same session — a merged
  entry was written up as "the import never attached a file" when the file was
  there all along. **A `try` whose body can fail for a reason you have not
  enumerated is not error handling, it is a blindfold.**
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

## The storefront is readable, and it is not the OS region

`AppleLocale` is a macOS setting. The Apple Media Services storefront follows
the **Apple ID's country**. Reading `en_US@rg=byzzzz` as "the Belarusian store"
ran a whole availability survey of 27 tracks against a catalogue that was not
the user's, while his own share links said `/ge/`.

**This file then said the storefront "cannot be detected", and that was wrong.**
The mistake was looking in Music: the app does not expose the account store to
AppleScript, so the search stopped there. The signed-in account's storefront is
in `itunesstored`, as a number:

```bash
defaults read com.apple.itunesstored AccountsNotificationPlugin.ActiveStorefrontIdentifier
# 143615-2,42     -- storefront id, then a platform suffix
```

And nothing has to map that number to a country code, because **the iTunes
Search API takes the id directly as `s=`** in place of `country=`. Measured
2026-10-16: `search?s=143615` answers `"country": "GEO"`, while `country=rs`
answers `SRB` and `country=by` answers `BLR`. So this account is on the
**Georgian** store — exactly what the share links said, for the whole project,
while `STORE` silently defaulted to `us`. `account_storefront()` reads it now;
`GEO_UNBLOCKED_STORE` still overrides, and `country=us` is the fallback when the
preference cannot be read, because that is what an omitted `country` has always
silently meant and 135 of 229 covers were resolved that way.

**Moving countries does not change it.** The user went Belarus → Serbia between
passes and the storefront stayed `ge`; only the Apple ID's country moves it. So
"he is in a new country" is never by itself a reason to re-survey availability —
but newly geoblocked tracks appear anyway, 13 of them on 2026-10-16.

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

## A 30-second preview is a reference recording

Deezer and Spotify both hand out a short clip of the exact master with no
account, and a clip is all an audio comparison needs. That turns "the record
exists but we cannot have it" from a dead end into a *ruler*: download the
candidates, slide the preview along each one, and the right lineage shows up as
a sharp peak at a definite offset. Measured on three tracks, 2026-10-16:

- `The Imperas :: Twin Peaks Theme` — the 20.8s Spotify preview matched a
  **`Spirit - Topic`** upload at **0.97**, at offset 75.5s, and an independent
  `The One World Ensemble` upload at **0.97** at the same offset, while three
  other uploads of a song by that title scored 0.48, 0.32 and 0.20. Durations
  agree to 15ms (150.706 against 150.721).
- `Сатана Печёт Блины :: Sigma` — the Deezer preview against the one plausible
  candidate: **0.68**, against a different track of the same length **0.18**,
  against itself shifted **1.000**. 0.68 is not a re-upload; it is the cover the
  title said it was, and the measurement is what settled it.
- The calibration matters as much as the number. Run the preview against itself
  and against a known-wrong track of the same length before trusting a middle
  value.

**And this is how a pseudonym release is reached at all.** Budget labels issue
one master under several invented artist names -- the same Twin Peaks recording
is `The Imperas` on Spiritual Health, `Spirit` on Travelscape and
`The One World Ensemble` on K-tel. No artist-keyed query can find it, and
`score()`'s artist requirement would reject the file even when handed it. So the
route is: find the release on Spotify by title, take its preview, and use the
preview to identify which differently-credited upload is the same recording.
Spotify's own related-tracks list named the pseudonym first, which is a free
lead worth checking before any of this.

This is the same lesson as `FPS Allstars` being published as `Geek Music`, with
a method attached: **the library's artist is often not the distributor's artist,
and the audio is the only identifier that survives the renaming.**

### Apple's own preview outlives the track, and the Wayback Machine knows where it is

Spotify and Deezer both return no preview for a restricted track, so the method
above looked unavailable for exactly the tracks that need it. Apple's does not
go away: **a removed track's preview stays on Apple's CDN**, and the URL is in
any archived copy of the album page. Measured 2026-10-16 on `405Okced ::
Creelles Town` — the Wayback snapshot (2024-09-10) of Apple album 1687591289
listed `audio-ssl.itunes.apple.com/itunes-assets/AudioPreview116/…plus.aac.p.m4a`
for all 21 tracks, and every one still answered HTTP 200 with 30s of 272k AAC,
years after the album left every storefront. Ask the CDX index by the exact page
prefix (`music.apple.com/us/album/<slug>*`); domain-wide queries time out.

**Apple's preview window is predictable, which makes it a ruler for edits too.**
On that album: a track of 79s or more previews from 45.0s, a 72s track from
30.0s, a 25s track from 0. Four tracks known to equal the developer's rips
landed at 44.94-44.98s of the rip, so the offset is real and constant. That is
what settled where the distributor had cut a 72s release out of a 120s piece:
the preview sat at 29.94s of the rip, so the release starts at the rip's 0.
`Boss Rush`, previewed in full at 25s, confirmed the same scheme with no fade
and gain unchanged to 0.0 dB. The exact copy is therefore `rip[0:72.051]` —
reconstructed, not found, and verified against Apple's own audio for 30 of its
72 seconds.

It was imported by hand on 2026-10-16 through the module's own `tag`,
`park_original`, `add_to_music`, `inherit_history` and `apply_export_history`,
in the main loop's order, with the comment naming the cut
(`3pUhABVI0Xs [0:72.051, …]`) and the cover named in `artwork_sources.json`.
**The recipe is now the tool's, two ways.** `track_sources.json` takes
`{"url": …, "trim": [0, 72.052]}`, and that entry is in the file, so a clean
pass reproduces the import. And with no trim named, the main loop derives it:
the downloaded rip is 48s too long, the Wayback reference sits at 30.0s of it,
`apple_window(72.052)` says the preview starts 30s into the release, so the
release is `[0.0:72.052]` -- 1ms from the hand-made cut, because Apple's page
says 72052ms. `cut()` was checked on its own: `[10:82.052]` of a Summertime
upload came out 72.059s with the preview moved by exactly 10.000s.

`apple_window` trusts only what was measured: 45s when the release is at least
75s, 30s when it is at least 60s, 0 when the preview is the whole track. Between
30s and 60s it returns nothing and nothing is trimmed.

`add_to_music` failed once on this import with `-1700 Can't make some data into
the expected type` and succeeded on an identical retry seconds later, returning
the same track id twice (no duplicate). It happened right after a `music://` URL
had been opened in Music; the likeliest reading is that Music was busy, not that
the path was wrong.

`Hobart Ocean :: Confidential Liar (Live)` still has no reference: its Spotify
EP lists the track as `COUNTRY_RESTRICTED` with no preview, and the Wayback
Machine holds no capture of the track page, its embed, or the album.

### More places a dead track's preview survives — measured 2026-10-16

- **Genius keeps Apple's preview URL.** `https://genius.com/songs/<song id>/apple_music_player`
  embeds the `audio-ssl.itunes.apple.com/…plus.aac.p.m4a` of the track Genius
  linked, and the CDN still serves it after Apple drops the track. It gave the
  only reference for `MARU NARA :: I GET DOWN`, whose album was re-delivered
  without it and whose Apple page Wayback never captured.
- **A Wayback snapshot of a Spotify track page holds `p.scdn.co/mp3-preview/…`**,
  and that URL also still answers: a 2020 snapshot gave 30s of `Yartie :: Marked
  Bills`, restricted everywhere today.
- **song.link's public API is gone (401), but its HTML page still carries
  `__NEXT_DATA__`** with every platform id it ever matched — Pandora, Tidal,
  Amazon ASINs, Yandex ids.
- Reference first, then search: it is what proved `Dead Confederate :: Guns`
  alive (Apple's AthFest preview against the re-delivered EP: waveform 0.91,
  background 0.03) and what turned `Twin Peaks` from a lookalike into a match.

### Where the "withdrawn everywhere" tracks actually were

Round three re-asked every source that had *refused* earlier rounds, and three
of the five "gone from every platform" verdicts fell:

- `Dead Confederate :: Guns` is on Apple Music — the re-delivered EP
  `Start Me Laughing` (2010, id 371709990), buyable for £0.99 and streamable in
  GB, the EU, UA and TR. Just not in `ge`.
- `Hobart Ocean :: Confidential Liar (Live)` is on Spotify, EP id
  `2bRaFjKyJVWSPpLBYUcNSX`, 232.000s exactly — restricted only from here.
- `Bass Boosted :: Pyrotex` sat on mp3party at 188.000s; the "missing" 4.3s of
  the only copy found before were the release's trailing silence.

**mp3party serves a file only with `Range: bytes=0-`** -- exactly that; no
header and `bytes=0-63` both get the same 29 bytes, `failed to get file info:
nil`. **And that same 29-byte answer also means "listed but not stored"**: on
the same host some ids return the full file and others never do, cookies and
Referer notwithstanding -- the listing for `MARU NARA :: I GET DOWN` exists and
its file does not. Test a known-good id beside the candidate before calling it
an IP block.

That test is now the provider's (`fetch_mp3party`, control id 8982205, the
Pyrotex file). Measured at 19:10 the same day, it was needed at once: every id
tried -- Pyrotex itself, which had been downloaded hours earlier, and five
random popular ones -- got the 29 bytes, from curl and from a real browser on
the same address (`<audio>` error 4). So the provider reports "this address is
refused" and leaves the run, rather than writing "no file" against a track.
HEAD answers `200 audio/mpeg` either way, so it proves nothing.

**The built-in browser plays through the user's speakers, with no window.** An
agent opened an mp3party page there to check Pyrotex, the page autoplayed, and
the user heard a track with nothing on screen to explain it — only macOS's Now
Playing showed it. Agents get audio with curl/yt-dlp; a browser tab is for
reading and is closed afterwards.

### VK has what the stores dropped

Logged into VK in the built-in browser (the user signs in — an agent never does),
audio search on 2026-10-16 found three of the seven tracks every other source
lacked: `maru nara — i get down` at exactly 1:32, `Sigma (feat. ПодлецМерзавец,
Nutakoe)` at 3:19 inside a 267-track fan playlist, and `Dead Confederate — Guns`
at 4:58 (three seconds short of the 301s master — check it against the AthFest
preview before trusting it). VK's guest view shows nothing; the search needs a
session. Reference previews for all three are in `work/refs/` (gitignored).

Practical facts about the page: VK has no download button; its new UI keeps
results in React state (no `data-audio` attributes, nothing in the HTML); the page
fires thousands of requests, so the network buffer drops a stream request within
seconds; and its player has no volume control until something plays, so muting
before pressing play takes patching `HTMLMediaElement`. The order of the rows'
"Start listening" buttons shifts after a reload — check which row you start.

**What got the files out, 2026-10-16:** the stream is HLS on `*.vkuseraudio.ru`.
Enlarge the page's resource-timing buffer *before* pressing play
(`performance.setResourceTimingBufferSize(1e6); performance.clearResourceTimings()`
— VK's request flood overflows the default within seconds), press play, take the
newest `vkuseraudio` entry — usually a segment, `…/seg-05-a2.ts?siren=1` — and
replace the file name with `index.m3u8?siren=1`. `ffmpeg -i <that> -c copy x.ts`
then fetches the whole track; some segments are AES-128 (`key.pub` beside them,
`crypto+https` in the log) and ffmpeg decrypts them itself. Content was mp3
256k in MPEG-TS. All three tracks matched their references: Guns 0.990 against
the AthFest preview, Sigma 0.986 against Deezer's (the cover: 0.646), I GET DOWN
0.980 against Apple's.

## The reference is the tool's job, and the audio decides

Everything above was run by hand on 2026-10-16. `reference()` and
`compare_audio()` make it the tool's: for each row it is about to import, the
tool finds a short recording of the release, caches it in `work/refs/`
(`index.json` says where each came from), and judges every downloaded candidate
against it before anything touches the library.

**Sources, cheapest first, stopping at the first that fits** -- artist, title
and length checked against the row, as strictly as `released_where`, because a
wrong reference rejects the right file and blesses a lookalike: Apple's Search
API (own store merged with `us`), Deezer, Genius's `apple_music_player`, the
Wayback Machine's Apple album and song pages (CDX by exact prefix, `us` and the
account's store), and Spotify (the public embed page, which carries name,
artists, length and `audioPreview` with no account, and an archived copy of the
track page when the track is restricted). Found on the first run, 2026-10-16:
Sigma from Deezer, I GET DOWN from Genius, Summertime and Guns from Apple's API,
Creelles Town from Wayback. Twin Peaks, Marked Bills and Confidential Liar need
Spotify, and Brave was answering 429 all session.

Only a found reference is cached. A miss is asked again next run, because a
source that failed is not evidence that no reference exists.

**`jload` turned a found capture into none.** The CDX index answers with a JSON
*list*, and `jload` returns `{}` for anything that is not an object -- by
design, for yt-dlp's `null`. So the first version of `_cdx` read every capture
as "never archived", and Creelles Town's reference, sitting in the archive,
came back as no reference at all. Nothing errored. `_cdx` parses with
`json.loads` and lets a malformed answer raise.

**The verdict is the waveform; the envelope only says where to look.** The
200ms RMS envelope finds the offset (it was right to the 0.1s on every case
below), a 1kHz waveform pass pins it to the millisecond, and 8kHz waveform
correlation through a 3kHz low-pass, in 5s segments with +-5ms slack, decides.
Every segment is also scored at two wrong offsets (+0.37s, -0.53s) -- the
background, which is the "against a known-wrong track" calibration done for free.
Measured with the code as committed:

| candidate against reference | envelope | waveform | background |
|---|---|---|---|
| Summertime: three uploads of the master | 0.77-1.00 | 0.97-1.00 | 0.03 |
| Summertime: the old import, shifted -12 cents | **0.80** | **0.04** | 0.03 |
| Summertime: Beck's own mix | 0.74 | 0.25 | 0.03 |
| Sigma: `САТАНА ПЕЧЕТ БЛИНЫ-SIGMA(COVER)` | 0.73 | **0.69** | 0.05 |
| Guns: the review candidate (another song), the live take | 0.68-0.72 | 0.01-0.02 | 0.02 |
| Sigma: five other songs by the band, one 199s long | 0.28-0.64 | 0.01-0.05 | 0.01-0.03 |

Two things in that table set the rules. The shifted copy's envelope beat two
genuine uploads, so **the envelope can never be the verdict**. And the Sigma
"cover" is not a re-performance: it reads 0.65-0.84 per segment, because the
uploader sang over the **original backing track** -- part of the file is the
master. A bar at 0.6, the first guess, let it through. `WAVE_SAME` is **0.85**:
above every contaminated copy measured, below every genuine one (0.97+) and the
AthFest Guns pair (0.91).

**What the verdict changes in the loop.** A candidate that matches is imported
even when its score is below the bar -- that is what reaches pseudonyms
(`Spirit` for `The Imperas`) and translated titles, which fail
`NO_TITLE_CAP`/`NO_ARTIST_CAP` by construction. A candidate that does not match
is rejected whatever it scored, and the next is tried; the row parks in review
with the measurement in `state.json` only when none passes. Without a reference
the old rules stand. A `manual` source is compared and warned about, never
rejected -- a human verdict outranks the heuristic, as everywhere else.

Cost: the comparison is 2-6s of pure Python per candidate (no numpy on the
system Python, and none needed). The reference is the expensive part when there
is none to find -- up to five Wayback probes and two Brave queries, about 20s --
and it is fetched only once a candidate is actually downloaded.

**`--audition`** runs the real loop -- search, download, reference, verdict,
trim -- and stops where the import would start. It writes neither the library
nor `state.json`, and with `--track` it runs on rows already done. It is how
every number in this section was produced through the changed path rather than
beside it.

**A known gap, not fixed here:** YouTube search returns *nothing* for a query
containing `Bob-Omb` -- `-Omb` reads as an exclusion. `sex bob-omb summertime`
gives zero results, `Summertime Scott Pilgrim` gives eight. `pick()` puts the
artist in most queries, so every hyphenated artist is searched on YouTube by the
bare title alone.

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

**The RMS envelope is not enough: correlate the waveform, segment by segment.**
Measured 2026-10-16 on twelve candidates for `Sex Bob-Omb :: Summertime`. The
envelope method above put the file this tool had already imported at **0.64-0.70**
against the master cluster -- low, but not obviously disqualifying, and five
independent uploads of the master sat at 0.81-0.99 among themselves, so the two
bands overlap. Aligning the files and correlating the **waveform** at 8kHz
through a 3kHz low-pass, in 5-second segments, separates them with no overlap at
all: five uploaders agree at **0.97-0.99 in every one of 26 segments**, and the
imported file reads **0.00, dipping to -0.13**.

Why the envelope was fooled: that file is not a different take and not a radio
rip. It is the master **shifted -12 cents and band-limited to 16.5kHz** -- its
spectrum matches the master at 0.998 once the shift is undone. A pitch shift
leaves the loudness contour almost intact, which is all the envelope sees, while
destroying phase alignment, which is what the waveform sees. So:

- **Envelope correlation answers "is this the same performance".** It is cheap
  and it did separate `Winter Jam` from `Holiday Jam` -- same 285.04s length,
  same file size, different pieces -- at 0.377 against 0.976 for two copies of
  the same track.
- **Waveform correlation answers "is this the same file lineage".** Use it to
  pick between candidates that already passed the envelope test. It also caught
  Beck's own solo release of this recording at **-0.36**: a genuinely different
  mix, 1s shorter, which no title or duration check would have flagged.
- **A bigger container is not better audio.** The copy being replaced is 325kbps
  with content to 16.5kHz; the one replacing it is 192kbps with content to
  21.3kHz and `encoded_by=iTunes 9.2.1` in its tags. Bitrate ranks the encode,
  not the recording.

It is in the code now, against a reference rather than candidate against
candidate -- see "The reference is the tool's job" below.

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

**A romanised artist cannot reach a Cyrillic catalogue, and one back-
transliterated query is the whole fix.** Mail.ru answers
`Raznye Ludi - Такие дела` with four unrelated songs and `Такие дела Разные`
with the record on the second row. Measured over the 31 library tracks that have
a Cyrillic title and a romanised artist:

| queries | found by duration |
|---|---|
| the existing set | **23 of 31** |
| title + one back-transliterated token | 9 |
| title + up to eight variants | 10 |

Back-transliteration alone is far worse -- but it reaches two tracks the existing
queries never can (`Raznye Ludi :: Такие дела`, `Alisa :: Пуля`), so it is added
as ONE extra query, not as a replacement. Eight variants instead of one bought
exactly one additional track, so the variant machinery was dropped.

Three details it took measuring to get right. **Only the longest artist token**
is used: search engines AND their terms, so `Разные луди` (from `Ludi`, where
Apple wrote `ю` as `u`) finds nothing while `Разные` alone works. **Inverting
`CYR` mechanically is wrong** -- `y` is both `ы` and `й`, and the dict order
turns `Raznye` into `разнйе`; `_UNCYR` names the likelier letter explicitly.
And **`ь` is dropped by romanisation and never comes back**: `Nol` cannot become
`Ноль`. That costs nothing here only because the existing queries already find
every `Nol` track.

A related wrinkle, measured and left alone: our transliteration and Apple's
disagree, so `Raznye Ludi` tokenises to `{raznye, ludi}` and `Разные люди` to
`{raznye, lyudi}`. The artist match survives on `raznye`, so nothing was changed
-- but a two-token artist whose every token diverges would score as having no
artist evidence at all.

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

**Verify the TITLE every time too — that half was missing.** "Verify the
artist" has been in this file since the beginning and the converse never was, so
`score()` had `NO_ARTIST_CAP` and no equivalent for the title. The artist can be
right while the title names a different song, and the duration window is four
seconds wide, which is enough. Found by a mechanical sweep of all 249 `ok` rows,
comparing each candidate's title to the title it replaced — **three shared not
one word with it**, and all three had auto-imported:

| wanted | imported instead | score |
|---|---|---|
| `MARU NARA :: I GET DOWN (feat. Nvpvlm)` (ZE, 92s) | `BREAK A LEG` (OMA HOODY, 90s) | 122 |
| `$uicideBoy$ :: N****z` (Magnolia, 261s) | `Now and at the Hour of Our Death (Feat. Bones)` — **from a 2025 album** | 137 |
| `Gin Wigmore :: Oh My` (Holy Smoke, 257s) | `Hey Ho` (261s), her own, different song | 123 |

All three came from the artist's own channel, so every artist check passed. The
bar is 120 and the duration alone is worth 100. `NO_TITLE_CAP` now parks a
non-vouched zero-overlap candidate at 115 instead of importing it.

The honest cost: it also parks a **translated** title, which is sometimes right
— `Antoine Barry :: Капитан Джек Воробей` is published as `Capitán Jack Sparrow`,
zero shared tokens, and genuinely the same record. That is one human verdict in
`track_sources.json`, which is where human verdicts belong anyway, against three
wrong songs in the library. Measured both ways before choosing:

| candidate | before | after |
|---|---|---|
| `Severed` ← `The Decemberists - Severed` | 144 | 144 |
| `No Fun (Bonus Track)` ← `Sex Bob-Omb - No Fun` | 144 | 144 |
| the three above | 122–137 → imported | 117 → parked |
| `Капитан Джек Воробей` ← `Capitán Jack Sparrow` | 125 → imported | 117 → parked |

**And run that sweep again after any scoring change.** It took one query over
`state.json` to find three years' worth of wrong songs; nothing else in this
project had ever compared the two titles after the fact.

**With a reference, the cap no longer has the last word.** A candidate it parks
is now downloaded and compared (see "The reference is the tool's job"), and
`Capitán Jack Sparrow` reads **1.00** against Deezer's preview of `Капитан Джек
Воробей`, while the two other YouTube Music results the same search ranked next
-- different pirate songs at 240s and 244s -- read 0.01 and 0.03. The human
verdict is still the route when no reference exists, and still outranks one.

That same run found **a defect in the delivered-file check**. The exact master
was rejected as `скачано 238s вместо 242s`: the release has four seconds of
silence of its own, and `effective_duration` strips silence by design, so a
perfect copy measured short. The check exists for the 4.4s Mail.ru fragment
padded out to 121s, which no reference could ever match -- so when the audio is
proven the same and only the silence-stripped length is short, the container
length is what gets compared. Without a reference the strict check stands.

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

- **Deezer cannot be an audio source, and never was one.** Its public API serves
  a 30-second 128kbps `preview` and yt-dlp has no Deezer extractor at all. It is
  called in exactly one place, `deezer_art()`.
- **"Released" is three different answers, and only one is actionable.**
  `released_where()` separates them, in this order, because a guess must never
  shadow a fact:
  1. `store` — in the user's Apple Music storefront, artist and duration both
     matching. He clicks add, the history is carried across. Worked twice on
     2026-08-20.
     **The click is a person's, and that is measured, not assumed.** Music's
     dictionary has one `add` command and it takes *files* (`<type type="file"
     list="yes"/>`); nothing adds a catalogue item. Driving Music's UI is not an
     option in this environment either: computer-use control of Music is
     blocked by policy, and scripting its buttons through System Events would be
     a way around that block, not a solution. The Apple Music API can add to a
     library (`POST /v1/me/library`), but needs a MusicKit developer token AND a
     Music User Token from the user's own sign-in — a setup out of proportion to
     the handful of tracks this ever applies to. What the tool can do is cut the
     human part to one click: `open "music://music.apple.com/…"` lands on the
     track, and carrying the history afterwards is ours.
  2. `foreign` — in Apple Music, but in a storefront this account cannot reach.
     **There is nothing to click here either**, and for one revision this
     function said there was: `Sex Bob-Omb :: Summertime` is streamable at
     130.733s in `us` and absent from `ge` altogether, and it was reported as
     `store` with a `/us/` link. `itunes()` merges the own store with `us` on
     purpose -- right for artwork, wrong for availability -- so each result now
     carries `_own_store` and only an own-store hit may answer `store`. The
     news is still worth printing: the record exists and may reach his store
     later.
  3. `deezer` — published, but not in Apple Music. **There is nothing to click.**
     You cannot add a Deezer track to an Apple Music library, and one revision
     of this printed "добавь из стриминга" about `Сатана Печёт Блины :: Sigma`,
     which is in no Apple storefront under any of four query forms. What it is
     still worth saying: the record is real, so title, artist and length are
     confirmed and a copy may surface later.
  4. `maybe` — the title and duration match in the store but the artist does
     not, which is exactly what an aggregator credit looks like. Requiring the
     artist made the check blind to `FPS Allstars :: No Place for No Hero`,
     published as **Geek Music** — the very case that prompted writing it.
  **Every word of the wanted title must be present** for a `maybe`, not merely
  some. Measured over the 11 tracks still dead: partial overlap produced three
  leads and all three were wrong — "Confidential Liar (Live)" answered by "Keep
  It Confidential" on the word `confidential` alone, "Sigma (feat …)" by Epic
  Soul Factory's unrelated "Sigma", "Can't Do It Like Me" by "Do It Like Me".
  Requiring the whole title leaves one lead and it is the true one. A line that
  is wrong four times out of five teaches the reader to skip it.

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

- **YouTube downloads again, as of 2026-10-16.** yt-dlp 2026.08.19 fetches audio
  with no cookies and no PO token, through the `visionOS` player API; the
  `LOGIN_REQUIRED` wall described below is simply gone. Verified on a video out
  of `track_sources.json` that the August pass could not touch, and across the
  whole 2026-10-16 run. **So the wall was a yt-dlp/YouTube version problem, not a
  property of the source** — which is the same lesson Mail.ru taught: a provider
  that searches and never delivers is a bug report, not a fact about the world.
  Re-test it before writing off `yt`; it is the best source when it works
  (`- Topic` channels), and four months of notes said otherwise.
  Two smaller things measured the same day: a `Requested format is not
  available` from `-f bestaudio[ext=m4a]/bestaudio` **is transient** — the same
  url succeeded on retry and again by hand, so do not add a format fallback for
  it; and dropping `-f` entirely gets a 15.8MB m4a where the format-selected
  download gets 5.0MB of the same 310s audio, i.e. a transcode from opus, not
  better quality. Keep the explicit format.
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

- **Do not bring decisions back to Art. Measure instead.** When a change could
  go two ways, run the honest experiment -- the approach and its counterweight,
  on a sample large enough to mean something -- and decide from the numbers.
  This has already overturned two confident instincts in one session:
  candidate-vs-candidate audio clustering lost to the existing scorer 5-6 on 15
  tracks, and multi-variant back-transliteration bought one track for eight
  times the queries. Both would have shipped on argument alone.
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
