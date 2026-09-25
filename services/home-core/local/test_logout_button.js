/* The logout control is browser-only: verified against the real markup and the
   real script, not a paraphrase of either. */
const fs = require('fs'), path = require('path');
const HTML = fs.readFileSync(path.join(__dirname, 'templates', 'chat.html'), 'utf8');
let fail = 0;
const check = (l, c, d) => { console.log(`  ${c ? 'PASS' : 'FAIL'}  ${l}` + (c ? '' : `  <- ${d}`)); if (!c) fail++; };

check('the button is marked browser-only',
      /id="logout-btn"[^>]*data-browser-only|data-browser-only[^>]*id="logout-btn"/.test(HTML));
check('so is the separator above it',
      /<div class="app-sep" data-browser-only><\/div>\s*\n\s*<button class="app-item danger" id="logout-btn"/.test(HTML));
check('the app removes them rather than hiding them',
      /if \(window\.AndroidApp\)[\s\S]{0,320}data-browser-only[\s\S]{0,120}\.remove\(\)/.test(HTML));
check('and the handler tolerates the button being gone',
      /var logoutBtn = document\.getElementById\('logout-btn'\);\s*\n\s*if \(logoutBtn\)/.test(HTML),
      'an unguarded addEventListener would throw and kill the rest of the script');
// The removal must run before anything looks the element up.
const iRemove = HTML.indexOf('if (window.AndroidApp) {');
const iHandler = HTML.indexOf("var logoutBtn = document.getElementById('logout-btn')");
check('removal comes before the lookup', iRemove > 0 && iRemove < iHandler, [iRemove, iHandler]);
console.log(fail ? `\n${fail} FAILED` : '\nall checks passed');
process.exit(fail ? 1 : 0);
