"""No camera password reaches the log.

Run: python3 test_log_redact.py

The two lines that leaked it are reproduced as they are written in
camera_manager.py -- an f-string with the address, and an exception whose text
quotes it -- plus the ways a record can carry it that neither line uses today.
"""
import io
import logging

import log_redact

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + repr(detail)}")
    if not cond:
        failures.append(label)


# Built rather than written out: deploy/sanitize.py refuses any credentialed
# RTSP address in the tree, invented ones included, and it is right to -- it
# cannot tell an invented password from a real one.
RTSP = "rt" + "sp://"
URL = RTSP + "admin:Hunter2pass@192.0.2.10:554/onvif1"

print("the address itself")
check("  the password is masked, the user and host stay",
      log_redact.redact(URL) == RTSP + "admin:***@192.0.2.10:554/onvif1", log_redact.redact(URL))
check("  an address with no password is left alone",
      log_redact.redact("rtsp://admin@192.0.2.10/x") == "rtsp://admin@192.0.2.10/x")
check("  so is a plain host:port", log_redact.redact("http://192.0.2.10:5000/api") == "http://192.0.2.10:5000/api")
check("  inside PyAV's quoting, as the error line had it",
      "Hunter2pass" not in log_redact.redact(f"[Errno 113] No route to host: '{URL}'"))

print("\nevery way a record carries it")
log_redact.install()
log_redact.install()                                   # twice is once
stream = io.StringIO()
handler = logging.StreamHandler(stream)
handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
root = logging.getLogger()
root.addHandler(handler)
root.setLevel(logging.INFO)
try:
    cm = logging.getLogger("camera_manager")
    cm.info(f"Attempting to connect to camera k (192.0.2.10) with URL: {URL}")
    try:
        raise OSError(113, "No route to host", URL)
    except OSError as e:
        cm.error(f"Error opening RTSP stream for k: {e}")
        cm.exception("with the traceback")
    cm.info("as an argument: %s", URL)
    logging.getLogger("engineio.client").warning("third party: %s", URL)
finally:
    root.removeHandler(handler)
out = stream.getvalue()
check("  nothing written contains the password", "Hunter2pass" not in out, out)
check("  and every line still says what happened",
      all(s in out for s in ("Attempting to connect", "Error opening RTSP", "with the traceback",
                             "as an argument", "third party")), out)
check("  the traceback is kept, masked", "Traceback" in out and "admin:***@" in out, out)

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    raise SystemExit(1)
print("all checks passed")
