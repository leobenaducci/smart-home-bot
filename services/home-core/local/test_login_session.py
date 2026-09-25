#!/usr/bin/env python3
"""What survives signing in, and who /debug/* is allowed to be.

Run: python local/test_login_session.py

Two things happen at the moment credentials are accepted, and both are quiet
when wrong.

**The session is discarded and rebuilt.** Anything that can set a cookie for
this host — a neighbouring app on another port, a plain-HTTP page on the LAN —
can plant a pre-auth session and wait for somebody to sign in on top of it.
The CSRF token has to go with it: a planted token is one the planter knows,
and knowing it is the whole of a CSRF attack. Carrying it across the rotation
would preserve the single value worth discarding, and nothing would look
wrong.

**But the language must not.** `lang` is a preference, not a credential.
Somebody who lands on `/login?lang=en`, reads the page in English and signs in
should not be answered in Spanish for having done so — and that is exactly
what a bare `session.clear()` does, in a house that ships seven locales.

`DEBUG_USER` is the other half. It is a **login id** — the number a person
types to sign in — and its default was `user1`, which is a *member* id and
matches no account in any household. With `?user=` removed at the same time,
holding the key bought a 404 about an id nobody had chosen.
"""
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-the-login-tests')
os.environ.setdefault('DEBUG_API_KEY', 'test-debug-key-for-the-login-tests')
os.environ['PROXY_SHARED_SECRET'] = 'testsecret'
# Set before app is imported: read at module scope, and this suite signs in.
os.environ.pop('DEBUG_USER', None)

# The whole directory, copied, and cwd moved into it *before* app is imported.
# `USERS_FILE` is the relative 'users.json', so on the machine that runs this
# stack an uncopied run would read — and a careless one write — the household's
# real accounts. See test_house_proxy.py, which does the same for the same
# reason.
_tmp = tempfile.mkdtemp(prefix='homecore-login-')
shutil.copytree(os.path.dirname(os.path.abspath(__file__)),
                os.path.join(_tmp, 'local'),
                ignore=shutil.ignore_patterns('backup_data', 'history',
                                              '__pycache__'))
# The catalogues, the way the deployer stages them beside the app. Without them
# `_translator` is None and `_pick_locale` returns 'es' on its first line -- so
# every language check below would pass while exercising nothing at all, which
# is how the first version of the locale tests here reported four greens for a
# code path it never reached.
_i18n_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'i18n')
if not os.path.isdir(_i18n_src):
    # Four levels from the *file*: local -> home-core -> services -> repo.
    # Three lands on `services/`, which has no i18n, so the copy silently did
    # not happen and `_translator` stayed None -- and every language check below
    # passed against `_pick_locale`'s first line instead of its logic.
    _i18n_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), 'i18n')
if os.path.isdir(_i18n_src):
    shutil.copytree(_i18n_src, os.path.join(_tmp, 'local', 'i18n'),
                    ignore=shutil.ignore_patterns('__pycache__'),
                    dirs_exist_ok=True)

os.chdir(os.path.join(_tmp, 'local'))
sys.path.insert(0, os.getcwd())
os.makedirs('backup_data', exist_ok=True)

try:
    import bcrypt
except ImportError as e:
    print(f'SKIP: {e} — run this where app.py can import.')
    raise SystemExit(0)

# A login id is a string of digits. Writing `user1` here would make this suite
# agree with the bug it exists to catch -- and writing a *real* one would put a
# household's login into a package meant to be redistributed, which is what
# deploy/sanitize.py exists to stop. Invented, and only its shape matters.
LOGIN = '999000111'
OTHER = '999000222'
PASSWORD = 'not-the-real-one'
_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
# Shapes taken from a real store: the login id is a string of digits, the
# nanobot id an int. A fixture that disagrees fails inside the route instead of
# at the check, which reads like a broken test rather than a broken app.
json.dump([{'username': LOGIN, 'hash': _HASH, 'nanobot_id': 1, 'name': 'Test'}],
          open('users.json', 'w'))

try:
    import app as A  # noqa: E402
except ImportError as e:
    print(f'SKIP: {e} — run this where app.py can import.')
    raise SystemExit(0)

failures = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


A.app.config['TESTING'] = True


def fresh_client():
    return A.app.test_client()


print('signing in throws the anonymous session away')
client = fresh_client()
# What an attacker who can set a cookie for this host would plant.
with client.session_transaction() as s:
    s['csrf_token'] = 'planted-token'
    s['lang'] = 'en'
    s['something_else'] = 'also planted'

r = client.post('/login', data={'username': LOGIN, 'password': PASSWORD})
check('the password is accepted', r.status_code in (302, 303), r.status_code)

with client.session_transaction() as s:
    signed_in = dict(s)

check('and the session is now that person', signed_in.get('user') == LOGIN,
      signed_in.get('user'))
check('the planted CSRF token did not survive',
      signed_in.get('csrf_token') not in (None, 'planted-token'),
      signed_in.get('csrf_token'))
check('nor did anything else that was planted',
      'something_else' not in signed_in, sorted(signed_in))
# The half that a bare clear() gets wrong.
check('but the language they chose did',
      signed_in.get('lang') == 'en', signed_in.get('lang'))

print('\nand a wrong password changes nothing')
client = fresh_client()
with client.session_transaction() as s:
    s['csrf_token'] = 'planted-token'
client.post('/login', data={'username': LOGIN, 'password': 'wrong'})
with client.session_transaction() as s:
    after = dict(s)
check('no session was handed out', 'user' not in after, sorted(after))

print('\nan already-signed-in session is not rebuilt under them')
client = fresh_client()
with client.session_transaction() as s:
    s['user'] = LOGIN
    s['csrf_token'] = 'issued-after-login'
client.get('/')
with client.session_transaction() as s:
    still = dict(s)
check('their token is left alone', still.get('csrf_token') == 'issued-after-login',
      still.get('csrf_token'))

# --- /debug/* acts as one configured account ---------------------------------
print('\n/debug/* says which id it wants rather than 404-ing about one nobody chose')


def debug_call(headers=None, query=''):
    return fresh_client().post('/debug/chat' + query,
                               json={'message': 'hola'},
                               headers=headers or {})


KEY = os.environ['DEBUG_API_KEY']

A.os.environ.pop('DEBUG_USER', None)
r = debug_call({'X-Debug-Key': KEY})
body = r.get_json() or {}
check('unset DEBUG_USER is a 503, not a 404', r.status_code == 503, r.status_code)
check('  and it names the id space', 'login id' in (body.get('error') or ''),
      body.get('error'))

# The old default. It is a member id, and it must not quietly resolve.
A.os.environ['DEBUG_USER'] = 'user1'
r = debug_call({'X-Debug-Key': KEY})
body = r.get_json() or {}
check("'user1' is refused, and said to be the wrong kind of id",
      r.status_code == 404 and 'login id' in (body.get('error') or ''),
      f'{r.status_code} {body.get("error")}')

A.os.environ['DEBUG_USER'] = LOGIN
r = debug_call({'X-Debug-Key': KEY})
check('a real login id gets past the auth check',
      r.status_code not in (401, 404, 503), r.status_code)

print('\nthe key is a header, never a query string')
A.os.environ['DEBUG_USER'] = LOGIN
r = debug_call(query=f'?key={KEY}')
check('a key in the URL does not authenticate', r.status_code == 401, r.status_code)
r = debug_call({'X-Debug-Key': 'wrong'})
check('nor does a wrong one in the header', r.status_code == 401, r.status_code)

# Every /debug route, not one of them. Two carried their own copy of the auth
# and kept `?key=` and `?user=` after the third stopped -- which is the whole
# reason there is one function now. A route added later that grows its own copy
# is what this section is here to catch.
print('\nand every /debug route goes through the same door')
A.os.environ['DEBUG_USER'] = LOGIN
for path, method in (('/debug/chat', 'post'), ('/debug/applog', 'get')):
    c = fresh_client()
    call = getattr(c, method)
    kw = {'json': {'message': 'hola'}} if method == 'post' else {}
    r = call(f'{path}?key={KEY}&user={LOGIN}', **kw)
    check(f'{path}: a key in the URL is not accepted', r.status_code == 401,
          r.status_code)
    r = call(path, headers={'X-Debug-Key': KEY}, **kw)
    check(f'{path}: the header key is', r.status_code != 401, r.status_code)

# `?user=` naming somebody else is the impersonation this closed. The log route
# is where it was reachable: hold the key, name anybody, read their phone's log.
print('\nnobody can be named in the URL any more')
A.os.environ['DEBUG_USER'] = LOGIN
json.dump([{'username': LOGIN, 'hash': _HASH, 'nanobot_id': 1, 'name': 'T'},
           {'username': OTHER, 'hash': _HASH, 'nanobot_id': 2,
            'name': 'Somebody else'}], open('users.json', 'w'))
os.makedirs(A.APPLOG_DIR, exist_ok=True)
open(A._applog_path(LOGIN), 'w').write('mine: the DEBUG_USER log\n')
open(A._applog_path(OTHER), 'w').write('theirs: somebody else entirely\n')
c = fresh_client()
r = c.get(f'/debug/applog?user={OTHER}', headers={'X-Debug-Key': KEY})
served = r.get_data(as_text=True)
check('the log served is DEBUG_USER\'s', 'mine:' in served, served[:60])
check('  and not the one named in the query', 'theirs:' not in served, served[:60])

# --- the security headers on an HTML page -------------------------------------
# The framing pair is the half that breaks a feature rather than protecting
# one: DENY and `frame-ancestors 'none'` refuse framing by *this* origin too,
# and chat.html frames its own /projects and /credentials for the Proyectos and
# Credenciales panels. That failure is silent -- an empty panel, nothing
# logged.
print('\nan HTML page carries its security headers')
_r = fresh_client().get('/login')
_csp = _r.headers.get('Content-Security-Policy', '')
check('a CSP is set', bool(_csp), _r.headers.get('Content-Security-Policy'))
check('same-origin framing is allowed, because this app frames itself',
      "frame-ancestors 'self'" in _csp, _csp)
check('  and the older header agrees with it',
      _r.headers.get('X-Frame-Options') == 'SAMEORIGIN',
      _r.headers.get('X-Frame-Options'))
check('images may still be data: and blob:',
      "img-src 'self' data: blob:" in _csp, _csp)
check('and audio blob:', "media-src 'self' blob:" in _csp, _csp)
# Inline is allowed on purpose for now; if that is ever tightened it has to be
# with nonces, not by deleting the word, or every page goes blank.
check("inline script is still allowed", "'unsafe-inline'" in _csp, _csp)


# --- the language a person reads the portal in --------------------------------
# `_pick_locale`'s docstring promised "then the member's saved preference" and
# there was no such step: `members[].locale` was set on the admin page, used by
# the deployer for that member's assistant, and never handed to this container
# at all. Everyone read the portal in the house language, and the only way to
# change it was to type `?lang=` by hand once and hope the session survived.

print("\nthe portal follows the language a member chose")
# Guard first: without the catalogues `_pick_locale` returns on its first line
# and every check below passes without reaching the logic.
check("  the catalogues are here at all", bool(A._translator),
      "no i18n/ in the test tree -- the checks below would prove nothing")
if A._translator:
    _avail = set(A._translator.available) & set(
        A._AVAILABLE_LOCALES or A._translator.available)
    check("  and English is one of them", "en" in _avail,
          f"{sorted(_avail)} -- if this is empty the tree is gone: these checks "
          f"have to run before the rmtree at the end of this file")

A._MEMBER_LOCALES.clear()
A._MEMBER_LOCALES.update({"900000111": "en", "900000222": "es"})

with A.app.test_request_context("/"):
    A.session["user"] = "900000111"
    check("  a member set to English gets English", A._pick_locale() == "en",
          A._pick_locale())

with A.app.test_request_context("/"):
    A.session["user"] = "900000222"
    check("  and one set to Spanish gets Spanish", A._pick_locale() == "es",
          A._pick_locale())

# Below the explicit choice, deliberately: somebody appending ?lang= is asking
# for this page in that language now, and a saved preference should not argue
# with a person looking at the screen.
with A.app.test_request_context("/?lang=es"):
    A.session["user"] = "900000111"
    check("  an explicit ?lang wins over the saved preference",
          A._pick_locale() == "es", A._pick_locale())

# And above the house default, which is the whole point.
with A.app.test_request_context("/"):
    A.session["user"] = "nobody-here"
    check("  somebody with no preference gets the house default",
          A._pick_locale() == os.environ.get("HOME_STACK_DEFAULT_LOCALE", "es"),
          A._pick_locale())

# A language the house does not ship is not honoured just because it was typed
# into a profile -- the catalogue would fall back to English anyway, and the
# page would disagree with itself about which language it is in.
A._MEMBER_LOCALES["900000333"] = "ja"
with A.app.test_request_context("/"):
    A.session["user"] = "900000333"
    check("  a language this house does not ship is ignored",
          A._pick_locale() != "ja", A._pick_locale())

shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f'{len(failures)} FAILED: ' + ', '.join(failures))
    raise SystemExit(1)
print('all checks passed')
