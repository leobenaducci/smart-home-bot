#!/usr/bin/python3 -I
"""The Programmer's local model on the LAN, on demand -- behind its key.

programmer-local-lan.socket hands this process its listening socket (fd 3).
For each connection it reads the request head and checks the key *before*
anything is started: a port scan, a curl without the key or a stray
container connection gets a 401 and the Studio keeps its card. A request with
the key starts programmer-local-lan-use.service, which requires the model --
so it waits for the Studio's running job and the model's load exactly as a
Programmer turn does -- and then the connection is forwarded to the model
whole (the rest of a keep-alive connection rides on the first request's key).

Fifteen minutes with no connection open and no bytes moving end this process;
its unit's ExecStopPost stops programmer-local-lan-use.service, and the
model, needed by nothing else, gives the card back. The socket stays: the
next connection starts this again.

Standard library only: it runs from the host's python3, outside any venv.
Environment (programmer-local.env): API_KEY, BACKEND_PORT.
"""
import asyncio
import hmac
import json
import os
import socket
import sys
import time

KEY = os.environ.get("API_KEY", "").encode()
BACKEND = ("127.0.0.1", int(os.environ.get("BACKEND_PORT") or 11461))
USE_UNIT = "programmer-local-lan-use.service"
IDLE = int(os.environ.get("IDLE_SECONDS") or 900)
HEAD_LIMIT = 64 * 1024
HEAD_TIMEOUT = 30

state = {"open": 0, "last": time.monotonic()}
start_lock = asyncio.Lock()


def key_matches(head: bytes) -> bool:
    """Authorization: Bearer <key>, or x-api-key: <key>; compared in constant time."""
    if not KEY:
        return False
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        name, value = name.strip().lower(), value.strip()
        if name == b"authorization" and value[:7].lower() == b"bearer ":
            if hmac.compare_digest(value[7:].strip(), KEY):
                return True
        elif name == b"x-api-key" and hmac.compare_digest(value, KEY):
            return True
    return False


def reply(status: str, message: str) -> bytes:
    body = json.dumps({"error": {"message": message}}).encode()
    return (f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body


async def model_up() -> bool:
    """Start the unit that needs the model; returns once the model answers
    (the start job waits for programmer-local.service's readiness check)."""
    async with start_lock:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "start", USE_UNIT,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await proc.wait() == 0


async def pump(reader, writer) -> None:
    try:
        while data := await reader.read(65536):
            state["last"] = time.monotonic()
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - already gone
            pass


async def handle(reader, writer) -> None:
    state["open"] += 1
    state["last"] = time.monotonic()
    try:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                asyncio.TimeoutError, ConnectionError):
            return
        if not key_matches(head):
            writer.write(reply("401 Unauthorized", "This model asks for its key."))
            await writer.drain()
            return
        if not await model_up():
            writer.write(reply("503 Service Unavailable",
                               "The local model did not start; see "
                               "journalctl --user -u programmer-local.service."))
            await writer.drain()
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(*BACKEND)
        except OSError:
            writer.write(reply("502 Bad Gateway", "The local model is not answering."))
            await writer.drain()
            return
        up_writer.write(head)
        await up_writer.drain()
        await asyncio.gather(pump(reader, up_writer), pump(up_reader, writer))
    finally:
        state["open"] -= 1
        state["last"] = time.monotonic()
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - already gone
            pass


async def main() -> None:
    if os.environ.get("LISTEN_FDS") != "1":
        sys.exit("programmer-local-lan-gate: start me from programmer-local-lan.socket")
    sock = socket.socket(fileno=3)
    server = await asyncio.start_server(handle, sock=sock, limit=HEAD_LIMIT)
    async with server:
        while True:
            await asyncio.sleep(min(15, IDLE))
            if state["open"] == 0 and time.monotonic() - state["last"] >= IDLE:
                return


if __name__ == "__main__":
    asyncio.run(main())
