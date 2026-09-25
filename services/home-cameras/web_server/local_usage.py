"""Tell HomeCore what the vision model did, so the usage page can say so.

The clip reviewer is the highest-volume user of this household's GPU -- every
recorded clip is three frames through a vision model -- and it was the one
piece of Alfred's work that appeared nowhere on the usage page, because it
costs no money. What it costs is the card, which on a single 12 GB box is the
scarcer of the two.

The synchronous twin of the voice gateway's copy, and a separate copy for the
same reason: different containers, different build contexts, no shared origin
to fetch from. Fire-and-forget on its own thread; a review must never be
slower, and must never fail, because the thing counting it is down.
"""

import hashlib
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Empty means "do not report", which is the default and not a failure.
HOMECORE_URL = os.environ.get('HOMECORE_URL', '').rstrip('/')
SERVICE_TOKEN = os.environ.get('USAGE_SERVICE_TOKEN', '')
_TIMEOUT_S = 10
# Bounded and non-blocking. The reviewer runs on a worker that must not stall:
# a full queue drops the measurement, which is the correct trade against
# holding up a clip.
_QUEUE_MAX = 500
_FLUSH_SECONDS = 15

_q: "queue.Queue[dict]" = queue.Queue(maxsize=_QUEUE_MAX)
_started = False
_lock = threading.Lock()


def enabled():
    return bool(HOMECORE_URL and SERVICE_TOKEN)


def record(kind, engine, model, ms, units=0, ok=True, route=''):
    """One completed call. Safe from anywhere; never raises.

    `route` is which internal path asked for it, so the usage page can tell
    the camera wall's reviews apart from a person showing Alfred a photo --
    the same model today, and two different decisions about it tomorrow.
    """
    if not enabled():
        return
    try:
        _q.put_nowait({'kind': kind, 'engine': engine or '', 'model': model or '',
                       'route': route or '', 'ms': int(ms),
                       'units': int(units), 'ok': bool(ok)})
        _ensure_worker()
    except queue.Full:
        pass
    except Exception:
        logger.debug('could not record a local usage row', exc_info=True)


def _ensure_worker():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_flush_loop, daemon=True,
                     name='local-usage').start()


def _flush_loop():
    while True:
        time.sleep(_FLUSH_SECONDS)
        batch = []
        try:
            while True:
                batch.append(_q.get_nowait())
        except queue.Empty:
            pass
        if not batch:
            continue
        try:
            _post(batch)
        except Exception as e:
            # Dropped rather than retried: a queue that survives an outage
            # replays yesterday's timings into a page about this morning.
            logger.debug('usage flush failed, dropped %d rows: %s', len(batch), e)


def _post(records):
    body = json.dumps({'records': records}).encode()
    req = urllib.request.Request(
        f'{HOMECORE_URL}/stats/api/local', data=body,
        headers={'Content-Type': 'application/json',
                 'X-Service-Token': SERVICE_TOKEN})
    # HomeCore serves its own self-signed cert on the home network, same as
    # every other call to it from this stack.
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=ctx) as r:
        r.read(1024)
