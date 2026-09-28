<?php
// Where the portal actually lives, supplied by the deployer from
// `dns.portal` and `services.home-core.port`. This line used to be a literal
// `https://hub.home:8443` inside the redirect below, so moving the
// portal's port sent every visitor to a closed port -- from the page whose
// whole job is the way in.
$portalUrl = getenv('HOME_STACK_PORTAL_URL')
    ?: (getenv('HOME_STACK_DOMAIN')
        ? 'https://hub.' . getenv('HOME_STACK_DOMAIN') . ':21001'
        : '');   // no domain configured: no link is better than a wrong one
?>
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Conectando…</title>
  <style>
    body {
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: radial-gradient(ellipse at 60% 30%, #f5e6c8 0%, #ecdbc0 40%, #e0cba8 100%);
      font-family: system-ui, -apple-system, sans-serif; color: #9E7A5A;
      flex-direction: column; gap: 16px;
    }
    .spinner {
      width: 32px; height: 32px;
      border: 3px solid #d9b98a; border-top-color: #D4845A;
      border-radius: 50%; animation: spin .8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    p { font-size: .9rem; }
  </style>
</head>
<body>
  <div class="spinner"></div>
  <p>Detectando red…</p>
  <script>
    // One address for everyone. hub is the only tailnet node and it
    // advertises subnet routes, so hub.home resolves on the LAN and over
    // Tailscale alike. This used to probe a *.ts.net URL first and fall back
    // to the LAN — two round trips to choose between two names for the same
    // machine, and the ts.net one stopped being needed the moment the subnet
    // routes went in.
    // An empty URL is not a redirect: `location.replace('')` reloads this
    // page, so a container that was never told where the portal is spins
    // forever on "Detectando red…" instead of saying what is missing.
    var portal = <?= json_encode($portalUrl) ?>;
    if (portal) {
      location.replace(portal);
    } else {
      document.querySelector('.spinner').remove();
      document.querySelector('p').textContent =
        'No hay una dirección configurada para el portal (dns.portal / site.domain).';
    }
  </script>
</body>
</html>
