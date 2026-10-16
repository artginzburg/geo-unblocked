#!/bin/zsh
# Nightly upkeep for geo-unblocked, run by launchd (see scripts/install-nightly.sh).
#
# Order matters, and each step is the one CLAUDE.md prescribes:
#   1. fold what only the library knows about our imports back into the index;
#   2. snapshot the whole library while every track is still alive;
#   3. pick up anything that died since yesterday;
#   4. restore it -- from the archive first, the network only for what is not there;
#   5. keep a ~60KB fingerprint (the release's preview) of tracks that still
#      play, so a dead one can be found and verified automatically later;
#   6. optionally, archive full verified copies too (off unless asked for: ~45GB).
# Every step is safe to rerun; a failed step does not stop the ones after it.
set -u
cd "${0:A:h}/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p work/logs
LOG="work/logs/nightly-$(date +%F).log"
step() { echo "\n== $(date '+%H:%M:%S') $*"; python3 -u geo-unblock.py "$@" || echo "!! step failed: $*"; }
{
  echo "== geo-unblocked nightly, $(date)"
  step --snapshot
  step --catalog
  step --refresh-index
  step --start 0 --limit 100000
  step --fingerprint "${GEO_UNBLOCKED_FINGERPRINTS_PER_NIGHT:-1500}"
  if (( ${GEO_UNBLOCKED_MIRROR_PER_NIGHT:-0} > 0 )); then
    step --mirror "$GEO_UNBLOCKED_MIRROR_PER_NIGHT"
  fi
  step --report
} >> "$LOG" 2>&1
# A month of logs is plenty.
find work/logs -name 'nightly-*.log' -mtime +31 -delete
