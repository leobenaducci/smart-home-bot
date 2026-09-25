"""The WhatsApp store: what is kept, who may read it, and what may be answered.

Run: python local/test_whatsapp_store.py   (needs Flask; skips loudly without it)

nanobot's WhatsApp channel is linked to one person's account and POSTs every
message here. This is the other half of the division the phone-notification
relay already uses: the channel is transport, HomeCore is the store AND the
permission gate. nanobot decides nothing about what it may answer — it asks,
here, on every message.

The two flags are deliberately asymmetric and this pins that:

- `can_read` defaults ON, because linking the account is the consent and the
  point is that "¿cuánto era la cuenta que mandó Jana?" has an answer. Muting a
  chat stops it being kept at all.
- `reply_mode` defaults 'off' for every chat, forever, because answering is the
  half that sends something out of the house under the user's own name.

The dedupe matters more here than it looks: Baileys replays on reconnect and
WhatsApp redelivers, so the same message arrives repeatedly and a store that
counted each one would answer "¿qué dijo?" with an echo.
"""
import json
import os
import shutil
import sys
import tempfile
import time

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-wa-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

USER1 = "user1"
USER2 = "user2"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2},'
            ' {"username": "%s", "nanobot_id": 3}]' % (USER1, USER2))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_wa_db()
A.app.config['TESTING'] = True
client = A.app.test_client()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


# Proxy auth, exactly as nanobot calls these — the channel POSTs the ingest and
# the skill GETs the queries, both with X-Proxy-*. Not a session cookie: a POST
# on a browser session is CSRF-checked and correctly 403s, which is a property
# worth keeping rather than working around.
_WHO = {'u': USER1}


def as_user(u):
    _WHO['u'] = u


def _h():
    return {'X-Proxy-Secret': 'p' * 32, 'X-Proxy-User': _WHO['u']}


NOW = int(time.time())


def send(chat_id, text, sender="56999", name="", is_group=False, wa_id=None, ts=None):
    return client.post('/chat/whatsapp/message', headers=_h(), json={
        'chat_id': chat_id, 'text': text, 'sender': sender, 'name': name,
        'is_group': is_group, 'message_id': wa_id or f'wa-{abs(hash((chat_id, text)))}',
        'ts': ts or NOW,
    }).get_json()


def search(q, **kw):
    qs = '&'.join([f'q={q}'] + [f'{k}={v}' for k, v in kw.items()])
    return client.get(f'/chat/whatsapp/search?{qs}', headers=_h()).get_json()

# --- Everything is kept, and the chat learns its own name ----------------------
print("\nwhat arrives is kept")
r = send('56911@s.whatsapp.net', 'Jana 2: 17500 carne', name='Jana 2')
check("a message is stored", r.get('stored') is True and r.get('ok'), r)
check("and reading is on by default — the link is the consent",
      r.get('can_read') is True, r)
check("but answering is off, for every chat, from the start",
      r.get('reply_mode') == 'off', r)

send('56911@s.whatsapp.net', 'Jana 2: 21000 super', name='Jana 2')
send('120363@g.us', 'Ana: reunión el jueves', name='Colegio 5°B', is_group=True)
send('56922@s.whatsapp.net', 'Sam: ya voy saliendo', name='Sam')

chats = client.get('/chat/whatsapp/chats', headers=_h()).get_json()['chats']
check("each chat is learned once", len(chats) == 3, [c['name'] for c in chats])
check("a group is marked as one",
      any(c['is_group'] and c['name'] == 'Colegio 5°B' for c in chats), chats)
check("and the message count is per chat",
      next(c['seen_count'] for c in chats if c['name'] == 'Jana 2') == 2, chats)


# --- Finding it again ----------------------------------------------------------
print("\nfinding it again")
r = search('jana')
check("by the sender's name", len(r['entries']) == 2, r)
check("newest first", r['entries'][0]['text'].endswith('21000 super'), r['entries'][0])
check("by a word in the message", len(search('carne')['entries']) == 1)
check("by the chat's name — the handle a group actually has",
      len(search('colegio')['entries']) == 1, search('colegio'))
check("no match is an empty list, not an error", search('pantufla')['entries'] == [])
check("an empty query is refused",
      client.get('/chat/whatsapp/search?q=', headers=_h()).status_code == 400)

send('56933@s.whatsapp.net', '100% seguro', name='Promos')
check("a bare % is text, not a wildcard", len(search('%25')['entries']) == 1,
      search('%25'))

r = client.get('/chat/whatsapp/recent?chat=Colegio 5°B', headers=_h()).get_json()
check("one conversation can be read on its own",
      len(r['entries']) == 1 and 'reunión' in r['entries'][0]['text'], r)


# --- Muting a chat stops it being kept -----------------------------------------
print("\nmuting a chat")
r = client.post('/chat/whatsapp/chats', headers=_h(),
                json={'chat_id': '120363@g.us', 'can_read': False}).get_json()
check("can_read can be turned off", r.get('can_read') is False, r)
r = send('120363@g.us', 'Ana: y también el viernes', name='Colegio 5°B', is_group=True)
check("and then nothing from it is stored", r.get('stored') is False, r)
check("the older message is still there — muting is not deleting",
      len(search('reunión')['entries']) == 1)
check("an unknown chat cannot be configured",
      client.post('/chat/whatsapp/chats', headers=_h(),
                  json={'chat_id': 'nope', 'can_read': True}).status_code == 404)
check("reply_mode only accepts the three it means",
      client.post('/chat/whatsapp/chats', headers=_h(),
                  json={'chat_id': '56911@s.whatsapp.net',
                        'reply_mode': 'siempre'}).status_code == 400)
r = client.post('/chat/whatsapp/chats', headers=_h(),
                json={'chat_id': '56911@s.whatsapp.net', 'reply_mode': 'ask'}).get_json()
check("and 'ask' is settable per chat", r.get('reply_mode') == 'ask', r)
check("which the ingest answer then reports back to the channel",
      send('56911@s.whatsapp.net', 'otra cosa', name='Jana 2').get('reply_mode') == 'ask')


# --- "ask" has to be able to say yes -------------------------------------------
print("\n'preguntar' means the user can actually say yes")


def outbound(chat_id, text='hola'):
    return client.post('/chat/whatsapp/outbound', headers=_h(),
                       json={'chat_id': chat_id, 'text': text}).get_json()


def approve(chat_id):
    return client.post('/chat/whatsapp/approve', headers=_h(),
                       json={'chat_id': chat_id})


JANA = '56911@s.whatsapp.net'          # left in reply_mode 'ask' above
check("with no approval, nothing goes out", outbound(JANA).get('allowed') is False,
      outbound(JANA))
r = approve(JANA)
check("the user can approve one reply", r.status_code == 200 and r.get_json()['ok'],
      r.get_json())
check("and then it is allowed", outbound(JANA).get('allowed') is True)
check("but only once — the approval is consumed by the send",
      outbound(JANA).get('allowed') is False, outbound(JANA))

send('56955@s.whatsapp.net', 'buenas vecino', name='Vecino')
OFF = '56955@s.whatsapp.net'
check("a chat set to 'off' cannot be approved at all",
      approve(OFF).status_code == 409, approve(OFF).get_json())
check("and still sends nothing", outbound(OFF).get('allowed') is False)
check("an unknown chat cannot be approved either",
      approve('nope@s.whatsapp.net').status_code == 404)

client.post('/chat/whatsapp/chats', headers=_h(),
            json={'chat_id': OFF, 'reply_mode': 'auto'})
check("'auto' needs no approval", outbound(OFF).get('allowed') is True)
check("and keeps sending", outbound(OFF).get('allowed') is True)


# --- The owner's own question is always answered --------------------------------
print("\nanswering the owner does not depend on reply_mode")


def outbound_owner(chat_id, text='la respuesta'):
    return client.post('/chat/whatsapp/outbound', headers=_h(),
                       json={'chat_id': chat_id, 'text': text,
                             'owner_asked': True}).get_json()


OFFCHAT = '56911@s.whatsapp.net'
client.post('/chat/whatsapp/chats', headers=_h(),
            json={'chat_id': OFFCHAT, 'reply_mode': 'off'})
check("with reply_mode off, an unsolicited message is refused",
      outbound(OFFCHAT).get('allowed') is False, outbound(OFFCHAT))
check("but the answer to something he asked goes out",
      outbound_owner(OFFCHAT).get('allowed') is True, outbound_owner(OFFCHAT))
check("and it is not an approval being spent",
      outbound_owner(OFFCHAT).get('allowed') is True)
check("the unsolicited case is still refused afterwards",
      outbound(OFFCHAT).get('allowed') is False)


# --- The same message twice ----------------------------------------------------
print("\nBaileys replays; the store does not")
first = send('56944@s.whatsapp.net', 'hola', wa_id='dup-1')
again = send('56944@s.whatsapp.net', 'hola', wa_id='dup-1')
check("the first is stored", first.get('stored') is True)
check("the replay is not", again.get('stored') is False, again)
check("and it appears once", len(search('hola')['entries']) == 1)

a = send('56944@s.whatsapp.net', 'sin id', wa_id='')
b = send('56944@s.whatsapp.net', 'sin id tambien', wa_id='')
check("two messages the bridge could not identify do not collide",
      a.get('stored') and b.get('stored'), (a, b))


# --- One person's WhatsApp is not another's ------------------------------------
print("\nwhose account it is")
as_user(USER2)
check("Sam sees none of Alex's chats",
      client.get('/chat/whatsapp/chats', headers=_h()).get_json()['chats'] == [])
check("nor his messages", search('jana')['entries'] == [])
r = client.post('/chat/whatsapp/chats', headers=_h(),
                json={'chat_id': '56911@s.whatsapp.net', 'can_read': False}).get_json()
check("nor can she mute one of them",
      r.get('error') is not None, r)
as_user(USER1)
check("and Alex's is untouched", len(search('jana')['entries']) == 3, search('jana'))

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
