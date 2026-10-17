"""Soulseek for geo-unblock.py, run inside its own venv (aioslsk needs Python
3.11+, the system python3 is 3.9). geo-unblock.py calls it as a subprocess and
reads one JSON document from stdout.

    slsk.py search WAIT_SECONDS QUERY     -> [{"user", "file", "size", "dur", "bitrate", ...}]
    slsk.py get USER REMOTE_PATH OUT_DIR TIMEOUT -> {"path": ...} or {"error": ...}

Credentials: two lines, name then password, in ~/.config/geo-unblocked/soulseek
(or $GEO_UNBLOCKED_SLSK_CREDENTIALS). Soulseek creates an account on the first
login with an unused name, so that first login is the user's, in a real client.
They are read here and never printed.

Exit codes: 0 answered, 3 login refused, 4 no credentials. A peer that refuses or
times out is an ordinary answer ({"error": ...}), not an exit code."""
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

logging.disable(logging.CRITICAL)   # aioslsk reports every unreachable peer

from aioslsk.client import SoulSeekClient                       # noqa: E402
from aioslsk.protocol.primitives import AttributeKey            # noqa: E402
from aioslsk.settings import CredentialsSettings, Settings      # noqa: E402
from aioslsk.transfer.state import TransferState                # noqa: E402

CREDENTIALS = Path(os.environ.get("GEO_UNBLOCKED_SLSK_CREDENTIALS",
                                  "~/.config/geo-unblocked/soulseek")).expanduser()
LOSSLESS = (".flac", ".wav", ".alac", ".ape", ".aiff", ".aif")


def settings(download_dir):
    try:
        user, pw = CREDENTIALS.read_text().splitlines()[:2]
    except (OSError, ValueError):
        print(json.dumps({"error": f"no credentials in {CREDENTIALS}"}))
        sys.exit(4)
    s = Settings(credentials=CredentialsSettings(username=user.strip(), password=pw.strip()))
    # Never touch the user's router: UPnP would open a port mapping on it that
    # outlives this process. Without it, peers behind NAT that cannot reach us
    # are still reachable the other way round, which is all a downloader needs.
    s.network.upnp.enabled = False
    s.shares.scan_on_start = False
    s.shares.download = str(download_dir)
    return s


async def session(download_dir):
    c = SoulSeekClient(settings(download_dir))
    await c.start()
    try:
        await c.login()
    except Exception as ex:
        await c.stop()
        print(json.dumps({"error": f"login refused: {str(ex)[:120]}"}))
        sys.exit(3)
    return c


def rank(r):
    """Best first: a free slot and a short queue decide whether the file arrives
    at all, then lossless, then bitrate."""
    lossless = r["file"].lower().endswith(LOSSLESS)
    return (not r["free"], min(r["queue"], 50), not lossless, -(r["bitrate"] or 0), -r["speed"])


async def search(wait, query):
    c = await session(tempfile.gettempdir())
    req = await c.searches.search(query)
    await asyncio.sleep(wait)
    out = []
    for res in req.results:
        for it in res.shared_items:
            a = it.get_attribute_map()
            out.append({"user": res.username, "free": res.has_free_slots,
                        "queue": res.queue_size, "speed": res.avg_speed,
                        "file": it.filename, "size": it.filesize,
                        "dur": a.get(AttributeKey.DURATION),
                        "bitrate": a.get(AttributeKey.BITRATE)})
    await c.stop()
    out.sort(key=rank)
    print(json.dumps(out, ensure_ascii=False))


async def get(user, remote, out_dir, timeout):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    c = await session(out_dir)
    t = await c.transfers.download(user, remote)
    end = asyncio.get_running_loop().time() + timeout
    state = None
    while asyncio.get_running_loop().time() < end:
        await asyncio.sleep(1)
        state = t.state.VALUE
        if state == TransferState.COMPLETE:
            break
        if state in (TransferState.FAILED, TransferState.ABORTED):
            break
    if state == TransferState.COMPLETE and t.local_path and Path(t.local_path).is_file():
        print(json.dumps({"path": t.local_path}))
    else:
        if state != TransferState.COMPLETE:
            await c.transfers.abort(t)
        reason = getattr(t, "fail_reason", None) or getattr(t, "abort_reason", None)
        name = getattr(state, "name", str(state))
        print(json.dumps({"error": f"{name.lower()}"
                                   + (f": {reason}" if reason else "")
                                   + (" (timeout)" if state not in (TransferState.FAILED,
                                                                   TransferState.ABORTED,
                                                                   TransferState.COMPLETE) else "")}))
        # A partial file must not be mistaken for a download by anyone.
        if t.local_path:
            Path(t.local_path).unlink(missing_ok=True)
    await c.stop()


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "search":
        asyncio.run(search(float(args[0]), args[1]))
    elif cmd == "get":
        asyncio.run(get(args[0], args[1], args[2], float(args[3])))
    else:
        sys.exit(f"unknown command {cmd}")
