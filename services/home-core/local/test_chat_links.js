/* What chat.html turns Alfred's markdown into.
 *
 * Run: node local/test_chat_links.js   (extracts the page's own script — no
 * copy of the regexes lives here, so this cannot pass against a stale idea of
 * what the page does).
 *
 * Every one of these was a link that rendered perfectly and then failed, which
 * is the failure mode this whole area keeps producing: the mistake is invisible
 * to Alfred and lands on whoever clicks.
 */
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, 'templates', 'chat.html'), 'utf8');
let script = html.split('<script>')[2].split('</script>')[0];
script = script.replace(/\{\{[\s\S]*?\}\}/g, '["user1","user2","user3","user4","user5","familia"]')
               .replace(/\{%[\s\S]*?%\}/g, '');

/* sanitizeHref resolves anything it does not recognise against the page's own
   URL, so there has to be one. Without it every such case came back null and
   the "left alone" checks below passed for the wrong reason. */
global.window = { location: { href: 'http://localhost/chat' } };

/* The three pieces under test, lifted from the page as it is served. */
const grab = (re) => { const m = script.match(re); if (!m) throw new Error('not found: ' + re); return m[0]; };
eval(grab(/var SHARE_ROOTS = [^\n]+/));
eval(grab(/function workspacePathToUrl\(path\)[\s\S]*?\n  \}/));
eval(grab(/function sanitizeHref\(url\)[\s\S]*?\n  \}/));
eval(grab(/var INLINE_RE = [^\n]+/));

let bad = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) bad++;
  console.log((ok ? '  PASS  ' : '  FAIL  ') + label + (ok ? '' : '\n           got  ' + JSON.stringify(got)
              + '\n           want ' + JSON.stringify(want)));
}

/* The link target the page would use, for one markdown link. */
function targetOf(md) {
  INLINE_RE.lastIndex = 0;
  const m = INLINE_RE.exec(md);
  if (!m) return null;
  return m[5] !== undefined ? m[5] : m[6];
}

console.log('a link target survives whatever the file is called');
check('plain', targetOf('[informe.md](download:user1/alfred/informe.md)'),
      'download:user1/alfred/informe.md');
check('with spaces', targetOf('[a](download:user1/alfred/Informe anual — 2026.pdf)'),
      'download:user1/alfred/Informe anual — 2026.pdf');
// The one that was broken: a bare target ends at the first ")", so this used to
// come back as "…/Factura (1" with ".pdf)" left over as text.
check('with parentheses, in the angle form',
      targetOf('[Factura (1).pdf](<download:user1/alfred/Factura (1).pdf>)'),
      'download:user1/alfred/Factura (1).pdf');
check('with brackets', targetOf('[n](<download:user1/alfred/notas [casa].md>)'),
      'download:user1/alfred/notas [casa].md');

console.log('\nand an image target too');
function imgOf(md) {
  INLINE_RE.lastIndex = 0;
  const m = INLINE_RE.exec(md);
  if (!m) return null;
  return m[2] !== undefined ? m[2] : m[3];
}
check('plain', imgOf('![f](download:media/foto.jpg)'), 'download:media/foto.jpg');
check('angle form', imgOf('![f](<download:user1/alfred/Foto (2).png>)'),
      'download:user1/alfred/Foto (2).png');

console.log('\nbold and code still parse — they moved along the group list');
INLINE_RE.lastIndex = 0;
check('bold', INLINE_RE.exec('**hola**')[7], 'hola');
INLINE_RE.lastIndex = 0;
check('code', INLINE_RE.exec('`x = 1`')[8], 'x = 1');

console.log('\nwhere a target points');
check('a share path', sanitizeHref('download:user1/alfred/informe.md'),
      '/chat/download/user1/alfred/informe.md');
check('a workspace path', sanitizeHref('download:media/foto.jpg'),
      '/chat/download/media/foto.jpg');
// Skills document the prefix and the model drops it; media/ has always been
// repaired here, and durable files live on the share now.
check('a bare share path is repaired', sanitizeHref('user1/alfred/informe.pdf'),
      '/chat/download/user1/alfred/informe.pdf');
check('a bare media path still is', sanitizeHref('media/doc_41_thumb.jpg'),
      '/chat/download/media/doc_41_thumb.jpg');
check('but an ordinary relative link is left alone',
      sanitizeHref('docs/readme.md'), 'http://localhost/docs/readme.md');
check('and a folder that is nobody\'s is left alone',
      sanitizeHref('otra/cosa.txt'), 'http://localhost/otra/cosa.txt');

console.log('\nand what a target may never point at');
check('javascript:', sanitizeHref('javascript:alert(1)'), null);
check('a walk out of the share', sanitizeHref('download:user1/../../etc/passwd'), null);
check('an empty segment', sanitizeHref('download:user1//alfred/x.md'), null);

console.log();
// (exit moved to the end: the download checks below are async)

/* ── A download button for a file that never existed ──────────────────────
 *
 * Alfred answered «¿cómo cambio el color?» with a 📥 informe.md and then said
 * he would look at the project first. The report was never written: he had
 * emitted a `download:` link for it, and the page rendered a button that was
 * indistinguishable from a working one until you tapped it and got a 404.
 *
 * Images have always said so — they fire `error` and are replaced by a chip.
 * A link cannot, so it is checked when somebody actually taps it.
 */
eval(grab(/function markDownloadBroken\(a, path\)[\s\S]*?\n  \}/));

function fakeAnchor(href) {
  const listeners = {};
  const a = {
    href, dataset: {}, parentNode: null, clicked: 0,
    addEventListener: (ev, fn) => { listeners[ev] = fn; },
    click: () => { a.clicked += 1; },
  };
  a.fire = (ev) => listeners[ev]({ preventDefault: () => { a.prevented = true; } });
  return a;
}

(async () => {
  global.document = {
    createElement: () => ({ className: '', textContent: '', title: '' }),
  };

  // A file that is not there: the button is replaced, and nothing navigates.
  let replaced = null;
  global.fetch = async () => ({ ok: false });
  let a = fakeAnchor('http://localhost/files/download?p=informe.md');
  a.parentNode = { replaceChild: (chip) => { replaced = chip; } };
  markDownloadBroken(a, 'informe.md');
  a.fire('click');
  await new Promise((r) => setTimeout(r, 0));
  check('a missing file replaces the button', replaced !== null, true);
  check('  and says which one', /informe\.md/.test(replaced.textContent), true);
  check('  and does not navigate', a.clicked, 0);

  // A file that is there: the click goes through, once, and is not re-checked.
  global.fetch = async () => ({ ok: true });
  a = fakeAnchor('http://localhost/files/download?p=real.md');
  a.parentNode = { replaceChild: () => { throw new Error('should not replace'); } };
  markDownloadBroken(a, 'real.md');
  a.fire('click');
  await new Promise((r) => setTimeout(r, 0));
  check('a real file downloads', a.clicked, 1);
  check('  and is not probed twice', a.dataset.checked, '1');

  // The probe itself failing is not evidence the file is missing.
  global.fetch = async () => { throw new Error('offline'); };
  a = fakeAnchor('http://localhost/files/download?p=quizas.md');
  a.parentNode = { replaceChild: () => { throw new Error('should not replace'); } };
  markDownloadBroken(a, 'quizas.md');
  a.fire('click');
  await new Promise((r) => setTimeout(r, 0));
  check('a failed probe still lets the download through', a.clicked, 1);

  process.exit(bad ? 1 : 0);
})();
