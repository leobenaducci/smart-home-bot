/* HomeCore's CSRF token, on every request of ours that changes something.
 *
 * Served through HomeCore, these pages are same-origin with it, so HomeCore
 * refuses any POST/PUT/DELETE that does not carry its CSRF token — and answers
 * 403 with an HTML error page, which every fetch here then tries to read as
 * JSON ("unexpected character at line 1 column 1"). Deleting a clip, keeping
 * one, saving settings: all of it.
 *
 * One wrapper around fetch rather than a change at forty call sites, and one
 * file rather than a copy in every template, because the interesting parts
 * below are the two failure modes and neither is obvious enough to want six
 * chances to get wrong. It does nothing at all on the LAN, where there is no
 * HomeCore session to protect: `base` is empty and we never patch anything.
 *
 * Loaded as `{{ base }}/static/js/csrf.js` from the <head> of each page, with
 * the mount in a data attribute. A classic script runs before every inline
 * script after it, so the patch is installed before any call site can fire.
 *
 * NOT covered, deliberately, because a fetch wrapper cannot be: the socket.io
 * transport (XHR), which is how the dashboard removes a camera and add_camera
 * adds one. Those are already unreachable through the proxy for their own
 * reasons — `io()` asks HomeCore's root for /socket.io/, which it does not
 * serve and could not upgrade — and both templates say so where they use it.
 */
(function () {
  var script = document.currentScript;
  var base = (script && script.dataset && script.dataset.base) || '';
  if (!base) return;

  var original = window.fetch;
  var pending = null;

  /* `ok` is not enough. With no HomeCore session, /_csrf is `login_required`,
     which *redirects* — and fetch follows it, so what comes back is HomeCore's
     login page: status 200, text/html, r.ok true. Taking that as success means
     r.json() throws, the catch below swallows it, and the real request goes out
     with no token to be 403'd — the exact symptom this file exists to remove,
     with nothing left to say why. The finance dashboard's isJson() is the same
     guard, written after the same bug. */
  function isJson(r) {
    return !r.redirected &&
      (r.headers.get('Content-Type') || '').indexOf('application/json') !== -1;
  }

  /* The promise is cached, not the token: a page that fires four requests
     before the first answer comes back should ask once, not four times. A
     failure is not cached at all, so the next attempt retries. */
  function token(refresh) {
    if (refresh) pending = null;
    pending = pending || original(base + '/_csrf', { credentials: 'same-origin' })
      .then(function (r) { return r.ok && isJson(r) ? r.json() : null; })
      .then(function (d) { return (d && d.csrf) || null; })
      .catch(function () { return null; });
    return pending.then(function (t) {
      if (!t) pending = null;
      return t;
    });
  }

  function send(input, init, t) {
    if (!t) return original(input, init);
    /* A Request carries its own headers, and passing init.headers alongside
       one *replaces* them wholesale — Content-Type included. Clone it and set
       the header on the copy instead. Nothing here builds Requests today; this
       is so that stays true rather than becoming a silent 415. */
    if (typeof Request !== 'undefined' && input instanceof Request) {
      var req = new Request(input, init);
      req.headers.set('X-CSRF-Token', t);
      return original(req);
    }
    /* A copy, not init itself: a caller that reuses one options object across
       calls should not find its plain headers quietly swapped for a Headers. */
    var headers = new Headers(init.headers);
    headers.set('X-CSRF-Token', t);
    var copy = {};
    for (var k in init) if (Object.prototype.hasOwnProperty.call(init, k)) copy[k] = init[k];
    copy.headers = headers;
    return original(input, copy);
  }

  window.fetch = function (input, init) {
    init = init || {};
    var method = init.method ||
      (typeof Request !== 'undefined' && input instanceof Request ? input.method : 'GET');
    if (/^(GET|HEAD)$/i.test(method)) return original(input, init);

    return token(false).then(function (t) {
      return send(input, init, t).then(function (r) {
        /* The session was replaced while this page stayed open — somebody
           logged in again in another tab, or HomeCore restarted — so the token
           we cached is stale and every button silently fails until a reload.
           Fetch a fresh one and send it again. Once only: a second 403 is a
           real refusal, not a stale token. */
        if (r.status !== 403 || !t) return r;
        return token(true).then(function (fresh) {
          return fresh && fresh !== t ? send(input, init, fresh) : r;
        });
      });
    });
  };
})();
