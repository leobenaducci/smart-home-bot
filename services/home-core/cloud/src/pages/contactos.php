<?php
if (session_status() === PHP_SESSION_NONE) session_start();
require_once __DIR__ . '/../../i18n/i18n.php';
$lang = home_stack_pick_locale();
?>
<?php
if (!isset($_SESSION['user'])) {
    header('Location: /publico/login');
    exit;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && ($_POST['_action'] ?? '') === 'logout') {
    session_destroy();
    header('Location: /publico/login');
    exit;
}
?>
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title><?= te('entry.contacts', [], $lang) ?></title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      background: radial-gradient(ellipse at 60% 30%, #f5e6c8 0%, #ecdbc0 40%, #e0cba8 100%);
      font-family: system-ui, -apple-system, sans-serif; color: #3D2B1F;
    }
    header {
      background: rgba(255,248,235,.92); border-bottom: 1.5px solid #d9b98a;
      padding: 16px 32px; display: flex; align-items: center; justify-content: space-between;
      box-shadow: 0 2px 8px rgba(92,61,46,.10);
    }
    .header-left { display: flex; align-items: center; gap: 12px; }
    .header-left a { color: #9E7A5A; text-decoration: none; font-size: .85rem; transition: color .15s; }
    .header-left a:hover { color: #5C3D2E; }
    header h2 { font-family: Georgia,serif; font-size: 1.2rem; color: #5C3D2E; }
    .logout-btn {
      background: none; border: 1.5px solid #d9b98a; border-radius: 999px;
      padding: 7px 18px; font-size: .82rem; font-weight: 600; color: #8B5E3C;
      cursor: pointer; transition: background .15s, color .15s;
    }
    .logout-btn:hover { background: #8B5E3C; color: #fff; }
    main { max-width: 700px; margin: 48px auto; padding: 0 20px; }
    .page-title { font-family: Georgia,serif; font-size: 1.8rem; color: #5C3D2E; margin-bottom: 8px; }
    .page-sub { font-size: .9rem; color: #9E7A5A; margin-bottom: 36px; }
    .contacts-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px,1fr)); gap: 20px; }
    .contact-card {
      background: rgba(255,248,235,.93); border: 1.5px solid #d9b98a; border-radius: 16px;
      padding: 28px 28px 24px; box-shadow: 0 4px 20px rgba(92,61,46,.12); transition: transform .15s, box-shadow .15s;
    }
    .contact-card:hover { transform: translateY(-3px); box-shadow: 0 8px 28px rgba(92,61,46,.18); }
    .avatar {
      width: 52px; height: 52px; border-radius: 50%;
      background: linear-gradient(135deg,#D4845A,#8B5E3C);
      display: flex; align-items: center; justify-content: center;
      font-size: 1.4rem; color: #fff; font-weight: 700; margin-bottom: 16px; font-family: Georgia,serif;
    }
    .contact-name { font-family: Georgia,serif; font-size: 1.15rem; color: #5C3D2E; margin-bottom: 4px; }
    .contact-role { font-size: .82rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: #D4845A; margin-bottom: 14px; }
    .contact-info { list-style: none; display: flex; flex-direction: column; gap: 6px; }
    .contact-info li { display: flex; align-items: center; gap: 8px; font-size: .9rem; color: #6B4A30; }
    .contact-info .icon { width: 18px; text-align: center; opacity: .7; }
    .contact-info a { color: #B8622E; text-decoration: none; }
    .contact-info a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <header>
    <div class="header-left">
      <a href="/">&#8592; Inicio</a>
      <h2>Sitio Publico</h2>
    </div>
    <form method="POST" action="/publico/contactos">
      <input type="hidden" name="_action" value="logout">
      <button class="logout-btn" type="submit">Salir</button>
    </form>
  </header>

  <main>
    <h1 class="page-title"><?= te('entry.contacts', [], $lang) ?></h1>
    <p class="page-sub">Informacion de contacto</p>

    <div class="contacts-grid">
      <div class="contact-card">
        <div class="avatar">?</div>
        <div class="contact-name">Alex Doe</div>
        <div class="contact-role">Padre</div>
        <ul class="contact-info">
          <li><span class="icon">&#9993;</span> <a href="mailto:member-one@example.com">member-one@example.com</a></li>
          <li><span class="icon">&#9742;</span> <span>+56 9 0000 0001</span></li>
        </ul>
      </div>

      <div class="contact-card">
        <div class="avatar">?</div>
        <div class="contact-name">Sam Roe</div>
        <div class="contact-role">Madre</div>
        <ul class="contact-info">
          <li><span class="icon">&#9993;</span> <a href="mailto:member-two@example.com">member-two@example.com</a></li>
          <li><span class="icon">&#9742;</span> <span>+56 9 0000 0002</span></li>
        </ul>
      </div>
    </div>
  </main>
</body>
</html>
