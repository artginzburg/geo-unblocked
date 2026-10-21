#!/bin/zsh
# Weekly upkeep for geo-unblocked, run by launchd (see scripts/install-weekly.sh).
#
# Order matters, and each step is the one CLAUDE.md prescribes:
#   1. fold what only the library knows about our imports back into the index;
#   2. snapshot the whole library while every track is still alive;
#   3. pick up anything that died since last week;
#   4. restore it -- from the archive first, the network only for what is not there;
#   5. put back covers that went missing -- on playing tracks too, which step 3
#      cannot see (it looks at cloud status only);
#   6. keep a ~60KB fingerprint (the release's preview) of every track that
#      still plays and has none yet -- the whole library on the first run
#      (~7h of quiet requests), only the newly added ones after that;
#   7. optionally, archive full verified copies too (off unless asked for: ~45GB).
# Every step is safe to rerun; a failed step does not stop the ones after it.
set -u
cd "${0:A:h}/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p work/logs
LOG="work/logs/weekly-$(date +%F).log"
step() { echo "\n== $(date '+%H:%M:%S') $*"; python3 -u geo-unblock.py "$@" || echo "!! step failed: $*"; }

# Music must be running for every step, and must not take the screen: launched
# here hidden and in the background (-j, -g) rather than by the first AppleScript
# call, which could bring it to the front -- this job may fire when the Mac wakes,
# with someone at the keyboard, with someone at the keyboard. Quit again only if we opened it.
started_music=0
if ! pgrep -xq Music; then
  open -gj -a Music && started_music=1
  for i in {1..30}; do pgrep -xq Music && break; sleep 1; done
  sleep 5
fi
{
  echo "== geo-unblocked weekly, $(date)"
  step --snapshot
  step --catalog
  step --refresh-index
  step --start 0 --limit 100000
  step --artwork --apply
  step --fingerprint "${GEO_UNBLOCKED_FINGERPRINTS_PER_RUN:-100000}"
  if (( ${GEO_UNBLOCKED_MIRROR_PER_RUN:-0} > 0 )); then
    step --mirror "$GEO_UNBLOCKED_MIRROR_PER_RUN"
  fi
  step --report
} >> "$LOG" 2>&1
if (( started_music )); then
  osascript -e 'tell application "Music" to if player state is stopped then quit' >/dev/null 2>&1
fi
# Half a year of logs is plenty.
find work/logs -name 'weekly-*.log' -mtime +183 -delete
