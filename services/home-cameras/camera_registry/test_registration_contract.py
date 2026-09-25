#!/usr/bin/env python3
"""The sample firmware and the server have to agree about registration.

Run: python test_registration_contract.py            (needs the registry up)

They did not. `/api/register` reads `ip_address` from the top level of the
body and answers 400 without it — and `esp32_sample.c++`, the file anyone
building a camera copies from, put `ip_address` inside `metadata`. Anything
built from that sample was refused before it could ever appear in the
registry, and the failure is invisible from the outside: the camera is on the
wifi, the registry is up and healthy, and the list is simply empty.

So this reads the **real sample file** rather than a copy of its payload. A
fixture would have drifted the same way the sample did.
"""

import json
import os
import re
import sys

import requests

BASE = os.environ.get('REGISTRY_URL', 'http://localhost:5001')
HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, 'esp32_sample.c++')
PROBE_IP = '203.0.113.77'          # TEST-NET-3, never a real camera here

failures = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def sample_top_level_keys():
    """The JSON keys the sample sends at the top level of its payload.

    Read out of the C++ by taking the register function's body and pulling the
    quoted keys that sit outside the nested `metadata` object.
    """
    src = open(SAMPLE, encoding='utf-8', errors='replace').read()
    m = re.search(r'jsonPayload\s*=(.*?);\s*\n', src, re.S)
    if not m:
        return None
    body = m.group(1)
    # Drop the metadata object, so its keys do not count as top level.
    body = re.sub(r'"\\"metadata\\":\s*\{".*?\+\s*"\}"', '', body, flags=re.S)
    return set(re.findall(r'\\"([a-z_]+)\\":', body))


try:
    requests.get(f'{BASE}/api/ping', timeout=5).raise_for_status()
except Exception as e:
    print(f'SKIP: no registry at {BASE} ({e})')
    raise SystemExit(0)

print('the sample sends what the server demands')
keys = sample_top_level_keys()
check('the payload could be read out of the sample', keys is not None, keys)
# The one the server refuses without. Named on its own because every other key
# here is optional and this one is the whole bug.
check('ip_address is at the top level, not buried in metadata',
      bool(keys) and 'ip_address' in keys, sorted(keys or []))

print('\nand the server means it')
r = requests.post(f'{BASE}/api/register', timeout=10, json={
    'camera_id': 'CAM-TEST-001', 'name': 'contract test',
    'metadata': {'ip_address': PROBE_IP}})        # the old, wrong shape
check('a body with ip_address only in metadata is refused', r.status_code == 400,
      r.status_code)
check('and says which field is missing', 'ip_address' in r.text.lower(), r.text[:120])

print('\na registration in the shape the sample now sends is accepted')
r = requests.post(f'{BASE}/api/register', timeout=10, json={
    'ip_address': PROBE_IP, 'camera_id': 'CAM-TEST-001', 'name': 'contract test',
    'port': 80, 'stream_type': 'mjpeg', 'stream_url': '/stream',
    'capabilities': ['mjpeg', 'web_server'],
    'metadata': {'resolution': '1920x1080', 'fps': 30, 'sensor': 'OV2640'}})
check('it is created', r.status_code == 201, f'{r.status_code} {r.text[:120]}')

listed = requests.get(f'{BASE}/api/list', timeout=10).json().get('cameras', [])
check('and it now appears in the list',
      any(c.get('ip_address') == PROBE_IP for c in listed),
      [c.get('ip_address') for c in listed])

print('\nthe heartbeat path the sample uses exists')
# It used to ping /api/<camera_id>/ping, and the registry keys everything by
# address — so every heartbeat was a 404 and the camera aged to "offline"
# while sitting there working.
r = requests.get(f'{BASE}/api/{PROBE_IP}/ping', timeout=10)
check('by address', r.status_code == 200, r.status_code)
r = requests.get(f'{BASE}/api/CAM-TEST-001/ping', timeout=10)
check('and by camera_id it is a 404, which is why the sample must not use it',
      r.status_code == 404, r.status_code)

print('\nthe root says what this server is')
# It was a bare 404, so anyone checking whether the registry was alive was
# told "nothing here" — which is what it looked like when a camera went
# missing and somebody came to see.
r = requests.get(f'{BASE}/', timeout=10)
check('it answers', r.status_code == 200, r.status_code)
body = r.json() if r.headers.get('Content-Type', '').startswith('application/json') else {}
check('and names itself', body.get('service') == 'camera-registry', body)
check('and counts what it holds', isinstance(body.get('cameras'), int), body)

# Tidy up, whatever happened above.
requests.delete(f'{BASE}/api/remove/{PROBE_IP}', timeout=10)
gone = requests.get(f'{BASE}/api/list', timeout=10).json().get('cameras', [])
check('the test camera is removed again',
      not any(c.get('ip_address') == PROBE_IP for c in gone),
      [c.get('ip_address') for c in gone])

print()
if failures:
    print(f'{len(failures)} FAILED: ' + ', '.join(failures))
    sys.exit(1)
print('all checks passed')
