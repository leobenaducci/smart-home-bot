"""The three /chat file routes, driven end to end against a fake share.

Run inside a container built from the deployed image — app.py imports Flask,
smbprotocol and the rest of it:

    docker run --rm --network none \
      -e SECRET_KEY=x -e DEBUG_API_KEY=x -e SMB_HOST=fake.home \
      -v $PWD/local:/app-src:ro -w /app local-web:latest \
      sh -c 'cp -r /app-src/. /app/; python test_alfred_files_routes.py'

test_alfred_files.py drives the access *decision* with no Flask at all; this
drives the routes — routing, JSON shape, status codes, headers, and the Spanish
a person actually gets back. The share is faked in-process, so this never
touches the real one; `--network none` is belt and braces.

Alfred's files moved out of his container and into `<su-carpeta>/alfred/` on the
family share, which is what the panel lists and what `download:` links resolve
against. The cases worth having are the ones that were wrong at some point: an
empty folder read as a 500, an accented filename in a latin-1 header, an image
forced to download, and a delete button that must not reach past the folder it
is drawn in.
"""
import errno
import io
import os
import shutil
import sys
import tempfile
import types

# Set, not defaulted: `_rel()` below strips exactly `\\fake.home\share`,
# so a real SMB_HOST inherited from the environment -- which is every run
# inside the deployed container that forgets the `-e` -- leaves every path in
# the fake share unstripped and fails the whole suite for no reason.
os.environ['SMB_HOST'] = 'fake.home'
os.environ['SMB_SHARE'] = 'share'
# app.py refuses to start without these -- deliberately, there is no insecure
# default -- so an import without them raises at module level and the whole
# suite dies before its first check. That is what it had been doing.
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-the-files-tests')
os.environ.setdefault('DEBUG_API_KEY', 'test-debug-key-for-the-files-tests')
# Who lives here and which of them are adults, supplied the way the deployer
# supplies it. Without HOMECORE_ADMIN_MEMBERS `ADVANCED_USERS` is empty and the
# house has no adults in it -- which is what made the "even for an admin" check
# below decorative: a plain user is refused `alfred-app/` by the ordinary
# your-folder-or-familia rule, so that check went on passing with the
# FILES_HIDDEN guard deleted from app.py. Only an admin reaches far enough to
# test it. (test_family_admin.py carries the same note for the same reason.)
os.environ.setdefault('HOMECORE_MEMBERS', 'user1,user2,user3,user4,user5')
os.environ.setdefault('HOMECORE_ADMIN_MEMBERS', 'user1')

# The whole directory, copied, and cwd moved into it *before* app is imported.
# `USERS_FILE` is the relative 'users.json' and the task DB is
# `backup_data/tasks.db`, both resolved against the working directory at import
# -- so run from a checkout this read the repository's own files and created
# `backup_data/` beside them. test_house_proxy.py does the same for the same
# reason, and the note in test_members.py says what it cost the one time a
# suite like this ran against real paths.
_tmp = tempfile.mkdtemp(prefix='homecore-files-')
shutil.copytree(os.path.dirname(os.path.abspath(__file__)),
                os.path.join(_tmp, 'local'),
                ignore=shutil.ignore_patterns('backup_data', 'history',
                                              '__pycache__'))
os.chdir(os.path.join(_tmp, 'local'))
os.makedirs('backup_data', exist_ok=True)
sys.path.insert(0, os.getcwd())

# The user store the share folders are derived from. `FILES_FOLDERS` is
# login id -> folder, built by `_refresh_people()` from users.json joined with
# HOMECORE_MEMBER_FOLDERS -- and with no store at all it is empty, so every
# route answered "Sin carpeta asignada en el compartido" and twenty-two checks
# read as broken routes rather than as a missing fixture.
#
# The login id, the member id and the folder are three different things and
# this fixture keeps them three different strings. Written as `user1` for all
# three, nothing below can tell them apart: `_alfred_share_dir()` could return
# f'{session["user"]}/alfred' -- keying the share folder on the login -- and
# every check in this file would still pass, while every real install answered
# "no files" to everybody. That is the confusion this codebase has paid most
# for. So an account carries a numeric login and names its member; the folder
# falls out of the member, and is the `user1/` in TREE below.
#
# user5 is the exception, on purpose: with no `member` at all `_member_of()`
# falls back to the login, which is the fresh-install case that fallback exists
# for. He is also the one nobody has ever filed anything for.
import json as _json  # noqa: E402
_ACCOUNTS = [{'username': f'99900011{n}', 'member': f'user{n}', 'nanobot_id': n}
             for n in range(1, 5)]
_ACCOUNTS.append({'username': 'user5', 'nanobot_id': 5})
with open('users.json', 'w', encoding='utf-8') as _fh:
    _json.dump(_ACCOUNTS, _fh)

# --- a fake share, installed before app.py imports smbclient ---------------
TREE = {}          # 'user1/alfred/informe.md' -> b'...'


def _rel(unc):
    return unc.replace('\\\\fake.home\\share', '').lstrip('\\').replace('\\', '/')


class _Entry:
    def __init__(self, name, isdir):
        self.name, self._isdir = name, isdir

    def is_dir(self):
        return self._isdir

    def stat(self):
        return types.SimpleNamespace(st_size=len(TREE.get(self.full, b'')), st_mtime=1_700_000_000)


DIRS = set()       # folders that exist with nothing in them


def scandir(unc):
    base = _rel(unc)
    prefix = base + '/' if base else ''
    if base and base not in DIRS and not any(p == base or p.startswith(prefix) for p in TREE):
        # What smbclient raises for a path that is not there: an OSError
        # carrying ENOENT, which is what the route distinguishes on.
        raise FileNotFoundError(errno.ENOENT, 'No such file or directory', unc)
    seen, out = set(), []
    for path in TREE:
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):].split('/')
        if rest[0] in seen:
            continue
        seen.add(rest[0])
        e = _Entry(rest[0], len(rest) > 1)
        e.full = prefix + rest[0]
        out.append(e)
    return out


def makedirs(unc, exist_ok=False):
    DIRS.add(_rel(unc))


def open_file(unc, mode='rb', **kw):
    data = TREE[_rel(unc)]
    return io.BytesIO(data) if 'b' in mode else io.StringIO(data.decode())


def remove(unc):
    del TREE[_rel(unc)]


fake = types.ModuleType('smbclient')
fake.scandir, fake.makedirs, fake.open_file, fake.remove = scandir, makedirs, open_file, remove
fake.ClientConfig = lambda **kw: None
fake.rmdir = lambda unc: None
fake.stat = lambda unc: types.SimpleNamespace(st_size=len(TREE[_rel(unc)]), st_mtime=1_700_000_000)
fakepath = types.ModuleType('smbclient.path')
fakepath.isfile = lambda unc: _rel(unc) in TREE
fakepath.isdir = lambda unc: any(p.startswith(_rel(unc) + '/') for p in TREE)
fakepath.exists = lambda unc: fakepath.isfile(unc) or fakepath.isdir(unc)
fake.path = fakepath
sys.modules['smbclient'] = fake
sys.modules['smbclient.path'] = fakepath

import app as A  # noqa: E402

# The schema. `init_shares_db()` and its siblings are called from app.py's
# `__main__` block, which an importing test never runs -- so the routes below
# opened a database with no tables in it and raised `no such table: shares`
# from inside a request. Created here, in the scratch tree cwd was moved to
# above, so nothing is written next to the checkout.
A.init_shares_db()

A.app.config['TESTING'] = True
# Login ids, not member ids and not folders -- see the users.json fixture
# above. user1's folder is `user1/`, and he is the admin.
USER1, USER3 = '999000111', '999000113'

failures = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def client_for(user):
    c = A.app.test_client()
    with c.session_transaction() as s:
        s['user'] = user
        s['csrf_token'] = 'tok'
    return c


TREE.update({
    'user1/alfred/evil.svg': b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>',
    'familia/evil.svg': b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>',
    'alfred-app/app-release.apk': b'PK fake apk',
    'user1/alfred/informe.md': b'# informe\n',
    'user1/alfred/documentos/Informe anual — 2026.pdf': b'%PDF-1.4 fake',
    'user1/alfred/foto.png': b'\x89PNG\r\n\x1a\n fake',
    'user1/documentos/contrato.pdf': b'privado',
    'user2/alfred/diario.md': b'privado',
})

user1, user3 = client_for(USER1), client_for(USER3)

print('the panel lists what Alfred filed, and only that')
r = user1.get('/chat/workspace')
data = r.get_json()
check('200', r.status_code == 200, r.status_code)
check('folder is user1/alfred', data.get('folder') == 'user1/alfred', data)
paths = sorted(f['path'] for f in data.get('files', []))
check('every file under it, and nothing else', paths == [
    'user1/alfred/documentos/Informe anual — 2026.pdf',
    'user1/alfred/evil.svg',
    'user1/alfred/foto.png',
    'user1/alfred/informe.md',
], paths)
check('nothing from the rest of the folder',
      all('/alfred/' in p for p in paths), paths)

r = user3.get('/chat/workspace')
check("user3's own folder, empty and not an error",
      r.status_code == 200 and r.get_json()['files'] == [], r.get_json())

# Nobody has filed anything for Noa and the folder was never created — the
# first time he opens the panel there is nothing there at all.
#
# `makedirs` has to fail for this to be the case it names. The route creates
# the folder before listing it, and the fake `makedirs` registers it in DIRS —
# so `scandir` found it, returned [], and this check passed with the route's
# whole ENOENT branch deleted. A share the app may not write to is exactly
# where the first-run 500 came from, and it is what makes `scandir` raise.
DIRS.clear()
_makedirs = fake.makedirs
fake.makedirs = lambda unc, exist_ok=False: (_ for _ in ()).throw(
    PermissionError(errno.EACCES, 'read-only share', unc))
r = client_for('user5').get('/chat/workspace')
fake.makedirs = _makedirs
check('a folder that does not exist yet reads as empty, not as a 500',
      r.status_code == 200 and r.get_json()['files'] == [], r.get_json())

_scandir = fake.scandir
fake.scandir = lambda unc: (_ for _ in ()).throw(OSError(errno.EHOSTUNREACH, 'share is down'))
r = user1.get('/chat/workspace')
check('but a share that is down is an error, not an empty panel',
      r.status_code == 500, (r.status_code, r.get_json()))
fake.scandir = _scandir

print('\nand the links in it open')
r = user1.get('/chat/download/user1/alfred/informe.md')
check('200 with the bytes', r.status_code == 200 and r.data == b'# informe\n', r.status_code)
check('as an attachment named for the file',
      'attachment' in r.headers.get('Content-Disposition', '')
      and 'informe.md' in r.headers['Content-Disposition'],
      r.headers.get('Content-Disposition'))

r = user1.get('/chat/download/user1/alfred/documentos/Informe anual — 2026.pdf')
check('an accented name survives the header',
      r.status_code == 200 and "filename*=UTF-8''" in r.headers.get('Content-Disposition', ''),
      r.headers.get('Content-Disposition'))

r = user1.get('/chat/download/user1/alfred/foto.png')
check('an image is shown, not saved',
      r.status_code == 200 and r.headers['Content-Disposition'].startswith('inline'),
      r.headers.get('Content-Disposition'))

r = user1.get('/chat/download/user1/alfred/no-existe.md')
check('a missing file is a 404, not a 403', r.status_code == 404, r.status_code)

print('\nan SVG is a document, not a picture, so it is never rendered here')
# It can carry <script>, this origin holds the session, and `familia/` is
# writable by the whole family — so an inline SVG is a way to run script as
# whoever clicks the link. A prompt-injected Alfred writes wherever his user
# can, which is the same reach.
for who, path in ((user1, 'user1/alfred/evil.svg'), (user3, 'familia/evil.svg')):
    r = who.get('/chat/download/' + path)
    check(f'{path} downloads instead of rendering',
          r.status_code == 200 and r.headers['Content-Disposition'].startswith('attachment'),
          r.headers.get('Content-Disposition'))
    check('and carries a CSP that would neuter it anyway',
          'sandbox' in r.headers.get('Content-Security-Policy', ''),
          r.headers.get('Content-Security-Policy'))

print('\nthe download button means save, whatever the file is')
r = user1.get('/chat/download/user1/alfred/foto.png?dl=1')
check('an image asked for with ?dl=1 is an attachment',
      r.status_code == 200 and r.headers['Content-Disposition'].startswith('attachment'),
      r.headers.get('Content-Disposition'))

print('\nthe hidden deploy folder is not reachable by naming it')
r = user1.get('/chat/download/alfred-app/app-release.apk')
check('even for an admin', r.status_code == 403, r.status_code)

print("\nsomebody else's is refused, in words the reader can act on")
r = user3.get('/chat/download/user2/alfred/diario.md')
body = r.get_data(as_text=True)
check('403', r.status_code == 403, r.status_code)
check('it names the path that was refused', 'user2/alfred/diario.md' in body, body[:200])
check('and where the file should have gone', 'user3/alfred' in body, body[:300])

print('\ndeleting is confined to the alfred folder')
r = user1.delete('/chat/workspace/file', json={'path': 'user1/documentos/contrato.pdf'},
               headers={'X-CSRF-Token': 'tok'})
check('a file outside it is refused', r.status_code == 403, r.status_code)
check('and still there', 'user1/documentos/contrato.pdf' in TREE)
r = user1.delete('/chat/workspace/file', json={'path': 'user2/alfred/diario.md'},
               headers={'X-CSRF-Token': 'tok'})
check("nor anybody else's", r.status_code == 403 and 'user2/alfred/diario.md' in TREE, r.status_code)
r = user1.delete('/chat/workspace/file', json={'path': 'user1/alfred/informe.md'},
               headers={'X-CSRF-Token': 'tok'})
check('its own is deleted', r.status_code == 200 and 'user1/alfred/informe.md' not in TREE,
      r.status_code)
r = user1.delete('/chat/workspace/file', json={'path': 'user1/alfred/informe.md'},
               headers={'X-CSRF-Token': 'tok'})
check('deleting it twice is a 404', r.status_code == 404, r.status_code)
r = user1.delete('/chat/workspace/file', json={'path': 'user1/alfred'},
               headers={'X-CSRF-Token': 'tok'})
check('the folder itself cannot be deleted', r.status_code == 403, r.status_code)
for bad in ({'path': 123}, {'path': ['a']}, {}, {'path': None}):
    r = user1.delete('/chat/workspace/file', json=bad, headers={'X-CSRF-Token': 'tok'})
    check(f'a malformed body is a 400, not a 500: {bad}', r.status_code == 400, r.status_code)
r = user1.delete('/chat/workspace/file', json={'path': 'user1/alfred/x\x00.md'},
               headers={'X-CSRF-Token': 'tok'})
check('and a control character never reaches the SMB layer', r.status_code == 400, r.status_code)

print('\nand media/ still goes to the workspace it always did')
called = {}


class _Resp:
    ok, headers, content = True, {'Content-Type': 'image/jpeg'}, b'jpegbytes'


def fake_get(url, **kw):
    called['url'] = url
    return _Resp()


A.requests.get = fake_get
A.find_user = lambda u: {'username': u, 'nanobot_id': 1}
r = user1.get('/chat/download/media/cam_patio_1785208081.jpg')
check('proxied to nanobot, not the share',
      r.status_code == 200 and 'workspace/files/media/' in called.get('url', ''),
      called.get('url'))
r = user1.get('/chat/download/media/../memory/MEMORY.md')
check('and the workspace guard still holds', r.status_code in (403, 404), r.status_code)

print()
if failures:
    print(f'{len(failures)} FAILED: {failures}')
    sys.exit(1)
print('all checks passed')
