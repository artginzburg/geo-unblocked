# geo-unblocked — working notes

Read the README first for what the tool does. This file is the accumulated
knowledge about *how it behaves in practice*, most of it learned by getting it
wrong once. None of it is guessable from the code.

## The goal, restated

Not "download the songs." **Replace a dead library entry with a live one while the
library keeps believing it is the same track.** Play history and playlist membership
matter as much as the audio. A replacement that loses 24 plays is a failure even if
it sounds perfect.

## Verify against Music, never against the log

The single most expensive mistake in this project: reporting success from what the
script *intended* rather than what the library *holds*.

The log said `art=store` for tracks whose artwork was still a padded 480×270 video
frame. Music had silently kept the old file. Every claim about a track must be read
back out of Music — pull `raw data of artwork`, read `played count`, measure the
image — before it is stated.

Two separate counters in this project reported successes that never happened,
because they incremented on the *attempt* rather than the *result*. Count what
changed, not what was tried.

## How Music actually behaves on import

- **It merges.** Adding a file whose song already exists usually folds it into the
  existing entry, preserving play counts and playlist membership. This is the best
  outcome and it happens for roughly 4 out of 5 tracks.
- **A re-import does not replace.** If our file is already there, Music matches the
  new copy to it and **keeps the old one**. Re-running changes nothing unless the
  previous import is deleted first.
- **It leaves orphans.** Rather than overwriting, it writes `Track 1.m4a`,
  `Track 2.m4a` beside the original. These accumulate across runs and are invisible
  from the library.
- **Merged entries are indistinguishable from ours.** After a merge, our file *is*
  the user's entry. Deleting "our" tracks in bulk therefore destroys their record —
  this cost one track's 2017 add date and 3 plays before it was noticed. Never bulk
  delete without re-adding; inside the script it is safe because delete and add are
  adjacent.

## AppleScript traps

- **`date added` is read-only.** `set date added` returns `Unknown object type` by
  every route tried. Not a bug to work around — write it into the comment.
- **`current date` inside `tell application "Music"` is captured by Music** and
  fails the same way. Build dates outside the tell block.
- **Deleting while iterating invalidates indices.** Collect `database ID`s first,
  delete after.
- **Park the original *before* importing.** During a merge the dead entry briefly
  stops being findable, so a park that runs afterwards silently finds nothing.
- **Writable:** `played count`, `played date`, `skipped count`, `loved`, `rating`,
  `comment`, artwork via `set data of artwork 1`. **Not writable:** `date added`.
- **Copying a date object between tracks fails**; a constructed date works.

## Matching

Duration is the only trustworthy signal. Titles lie constantly — "(lyrics)",
"(Official Audio)", live rips and sped-up edits all read as the real thing. A live
rip of Akon scored respectably on text alone and was caught only by length.

The Apple export **transliterates Cyrillic artists** ("Akvarium", "Aleksey Vishnya"),
so artist-qualified queries miss entirely for Russian material. The bare-title query,
still duration-gated, is what recovers them — and Mail.Ru is often the only place
that material exists at all.

`- Topic` channels are label-delivered audio and outrank everything else.

## Fields, and which ones are real

Carried: play count, last played, skips, loved, rating, composer.

**BPM is not missing — it was never there.** Zero of the 220 geoblocked entries
and zero file tracks in the whole library have it set. Apple does not populate
the field and the export has no column for it. Do not go looking for a way to
carry it.

`grouping` is likewise empty everywhere. `composer` is the one that mattered:
17 of the 241 tracks have one, and it was being dropped until noticed.

## The export is a backup

`data/Geoblocked.txt` is not just an input list. It holds `Plays`, `Last Played`,
`Skips` and `Date Added` for every track, and it is the *only* surviving record once
a library entry is gone. It has already been used to restore history that was
destroyed. There is no `Loved` column, so loved can only ever come from the dead
entry — if that entry is gone, it is gone.

## Performance

Every search is a `yt-dlp` network round-trip. With three providers and four query
variants that is up to twelve calls per track. Providers must therefore be
consulted as **strict fallbacks** — stop the moment a match clears the auto-import
threshold. Per-track timings are recorded as `secs` in `data/state.json`.

## Working agreements with Art

- **Do not ask about running the full 241-track pass.** It happens when Art says so,
  and not before. The 30-track slice is the development set.
- The target is all 30 replaced, not "most of them."
- One change at a time; report a latent fragility once rather than pushing it.
- Commit locally so GPG signing applies. Never write files through the GitHub
  Contents API.
