<?php
// The entry page. Reads the same translation catalogues the portal and the
// dashboards do -- see i18n/README.md for why they are one flat JSON per
// locale rather than gettext.
if (session_status() === PHP_SESSION_NONE) session_start();
require_once __DIR__ . '/../../i18n/i18n.php';
$lang = home_stack_pick_locale();
$site = getenv('HOME_STACK_SITE_NAME') ?: t('entry.title', [], $lang);
// Where the private site actually lives. One name for both networks: point it
// at the hub, and let DNS decide whether that resolves on the LAN or over the
// VPN. A literal here meant the page probed an address only one network could
// reach, and reported the site down on the other.
$portalUrl = getenv('HOME_STACK_PORTAL_URL')
    ?: (getenv('HOME_STACK_DOMAIN')
        ? 'https://hub.' . getenv('HOME_STACK_DOMAIN') . ':21001'
        : '');   // no domain configured: no link is better than a wrong one
?>
<!DOCTYPE html>
<html lang="<?= htmlspecialchars($lang, ENT_QUOTES, 'UTF-8') ?>">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title><?= htmlspecialchars($site, ENT_QUOTES, 'UTF-8') ?></title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: radial-gradient(ellipse at 60% 30%, #f5e6c8 0%, #ecdbc0 40%, #e0cba8 100%);
      font-family: system-ui, -apple-system, sans-serif;
    }
    .card {
      background: rgba(255,248,235,.92); border: 1.5px solid #d9b98a; border-radius: 24px;
      padding: 48px 56px 52px; text-align: center;
      box-shadow: 0 8px 40px rgba(92,61,46,.18), 0 2px 8px rgba(92,61,46,.10);
      max-width: 420px; width: 90vw;
    }
    .house-wrap { margin-bottom: 28px; }
    h1 { font-family: Georgia,serif; font-size: 2rem; color: #5C3D2E; letter-spacing: .02em; margin-bottom: 6px; }
    .subtitle { font-size: .95rem; color: #9E7A5A; margin-bottom: 36px; }
    .btn-group { display: flex; flex-direction: column; gap: 14px; }
    .btn {
      display: inline-block; padding: 14px 36px; border-radius: 999px;
      font-size: 1.05rem; font-weight: 600; text-decoration: none;
      transition: transform .15s, box-shadow .15s; letter-spacing: .03em;
    }
    .btn-primary  { background: linear-gradient(135deg,#D4845A,#B8622E); color:#fff; box-shadow: 0 4px 16px rgba(180,98,46,.35); }
    .btn-secondary{ background: linear-gradient(135deg,#8B5E3C,#6B4226); color:#fff; box-shadow: 0 4px 16px rgba(107,66,38,.30); }
    .btn:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(92,61,46,.35); }
    .btn:active { transform: translateY(0); }
    .btn-secondary.disabled {
      background: linear-gradient(135deg,#b0a090,#9a8878); box-shadow: none;
      cursor: not-allowed; pointer-events: none; opacity: .6;
    }
    .btn-status {
      font-size: .72rem; color: #9E7A5A; margin-top: -6px; min-height: 1em;
      transition: color .3s;
    }
    .btn-status.online { color: #6a9e5a; }
    .langs { margin-top: 28px; display: flex; flex-wrap: wrap; gap: 2px;
             justify-content: center; }
    .langs a { font-size: .78rem; padding: 4px 9px; border-radius: 999px;
               text-decoration: none; color: #9E7A5A; }
    .langs a:hover { background: rgba(154,122,90,.12); }
    .langs a[aria-current="true"] { color: #5C3D2E; font-weight: 700;
                                    background: rgba(154,122,90,.18); }
  </style>
</head>
<body>
  <div class="card">
    <div class="house-wrap">
      <svg width="140" height="130" viewBox="0 0 140 130" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="95" y="20" width="14" height="28" rx="2" fill="#7A4F33"/>
        <rect x="93" y="17" width="18" height="6" rx="2" fill="#9B6644"/>
        <polygon points="10,72 70,10 130,72" fill="#B8622E"/>
        <polygon points="10,72 70,10 14,72" fill="rgba(0,0,0,0.07)"/>
        <rect x="20" y="70" width="100" height="58" rx="2" fill="#F0D8B0"/>
        <rect x="54" y="96" width="32" height="32" rx="4" fill="#8B5E3C"/>
        <circle cx="81" cy="113" r="3" fill="#D4A464"/>
        <path d="M54 104 Q70 90 86 104" fill="#7A4F33" opacity="0.4"/>
        <rect x="28" y="82" width="22" height="20" rx="3" fill="#BFD9E8"/>
        <rect x="28" y="82" width="22" height="20" rx="3" fill="none" stroke="#9B6644" stroke-width="1.5"/>
        <line x1="39" y1="82" x2="39" y2="102" stroke="#9B6644" stroke-width="1"/>
        <line x1="28" y1="92" x2="50" y2="92" stroke="#9B6644" stroke-width="1"/>
        <rect x="90" y="82" width="22" height="20" rx="3" fill="#BFD9E8"/>
        <rect x="90" y="82" width="22" height="20" rx="3" fill="none" stroke="#9B6644" stroke-width="1.5"/>
        <line x1="101" y1="82" x2="101" y2="102" stroke="#9B6644" stroke-width="1"/>
        <line x1="90" y1="92" x2="112" y2="92" stroke="#9B6644" stroke-width="1"/>
        <rect x="14" y="126" width="112" height="4" rx="2" fill="#C4995A" opacity="0.5"/>
        <circle cx="100" cy="13" r="4" fill="#D9C8B0" opacity="0.6"/>
        <circle cx="104" cy="8" r="3.5" fill="#D9C8B0" opacity="0.45"/>
        <circle cx="107" cy="4" r="3" fill="#D9C8B0" opacity="0.3"/>
      </svg>
    </div>
    <h1><?= htmlspecialchars($site, ENT_QUOTES, 'UTF-8') ?></h1>
    <p class="subtitle"><?= te('entry.subtitle', [], $lang) ?></p>
    <div class="btn-group">
      <a class="btn btn-primary" href="/publico/login"><?= te('entry.public_site', [], $lang) ?></a>
      <a class="btn btn-secondary" id="btn-privado" href="/privado"><?= te('entry.private_site', [], $lang) ?></a>
      <div class="btn-status" id="privado-status"><?= te('entry.checking', [], $lang) ?></div>
    </div>

    <div class="langs">
      <?php foreach (home_stack_available_locales() as $code): ?>
        <a href="?lang=<?= urlencode($code) ?>"
           aria-current="<?= $code === $lang ? 'true' : 'false' ?>"
           title="<?= htmlspecialchars(home_stack_catalogue($code)['_meta.name'] ?? $code, ENT_QUOTES, 'UTF-8') ?>"
        ><?= htmlspecialchars($code, ENT_QUOTES, 'UTF-8') ?></a>
      <?php endforeach; ?>
    </div>
  </div>

  <script>
    const btn    = document.getElementById('btn-privado');
    const status = document.getElementById('privado-status');

    async function checkStatus() {
      // One name for both networks, resolved by your DNS rather than listed
      // here. Probing several addresses for the same machine only bought a
      // wasted 2s timeout per name that did not answer.
      const urls = [
        <?= json_encode($portalUrl . '/ping') ?>,
      ];
      for (const url of urls) {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 2000);
        try {
          const res = await fetch(url, { signal: ctrl.signal });
          clearTimeout(timer);
          if (res.ok) {
            status.textContent = '<?= te('common.online', [], $lang) ?>';
            status.classList.add('online');
            return;
          }
        } catch {
          clearTimeout(timer);
        }
      }
      status.textContent = '<?= te('entry.unreachable', [], $lang) ?>';
      status.classList.remove('online');
    }

    checkStatus();
    setInterval(checkStatus, 5000);
  </script>
</body>
</html>
