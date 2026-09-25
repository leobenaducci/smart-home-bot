<?php
// Same catalogues as the rest of the house.
if (session_status() === PHP_SESSION_NONE) session_start();
require_once __DIR__ . '/../../i18n/i18n.php';
$lang = home_stack_pick_locale();

if (isset($_SESSION['user'])) {
    header('Location: /publico/contactos');
    exit;
}

$error = null;

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $username = str_replace(['.', '-'], '', trim($_POST['username'] ?? ''));
    $password = $_POST['password'] ?? '';

    $users = file_exists(USERS_FILE) ? json_decode(file_get_contents(USERS_FILE), true) : [];
    $match = null;
    foreach ($users as $u) {
        if ($u['username'] === $username && password_verify($password, $u['hash'])) {
            $match = $u;
            break;
        }
    }

    if ($match) {
        session_regenerate_id(true);
        $_SESSION['user'] = $username;
        header('Location: /publico/contactos');
        exit;
    }

    $error = 'Usuario o contraseña incorrectos.';
}
?>
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Ingresar — Sitio Publico</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      background: radial-gradient(ellipse at 60% 30%, #f5e6c8 0%, #ecdbc0 40%, #e0cba8 100%);
      font-family: system-ui, -apple-system, sans-serif; color: #3D2B1F;
    }
    .card {
      background: rgba(255,248,235,.93); border: 1.5px solid #d9b98a; border-radius: 20px;
      padding: 44px 48px 48px; width: 90vw; max-width: 380px;
      box-shadow: 0 8px 40px rgba(92,61,46,.18), 0 2px 8px rgba(92,61,46,.10);
    }
    .back-link { display: inline-flex; align-items: center; gap: 6px; color: #9E7A5A; text-decoration: none; font-size: .85rem; margin-bottom: 28px; transition: color .15s; }
    .back-link:hover { color: #5C3D2E; }
    h1 { font-family: Georgia,serif; font-size: 1.6rem; color: #5C3D2E; margin-bottom: 6px; }
    .sub { font-size: .88rem; color: #9E7A5A; margin-bottom: 32px; }
    label { display: block; font-size: .82rem; font-weight: 600; color: #7A5030; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
    input {
      display: block; width: 100%; padding: 11px 14px;
      border: 1.5px solid #d9b98a; border-radius: 10px; background: #fffaf2;
      font-size: 1rem; color: #3D2B1F; outline: none; transition: border-color .2s; margin-bottom: 20px;
    }
    input:focus { border-color: #D4845A; background: #fff; }
    .btn {
      width: 100%; padding: 13px; border: none; border-radius: 999px;
      background: linear-gradient(135deg,#D4845A,#B8622E); color: #fff;
      font-size: 1rem; font-weight: 600; cursor: pointer; letter-spacing: .03em;
      box-shadow: 0 4px 16px rgba(180,98,46,.30); transition: transform .15s, box-shadow .15s; margin-top: 6px;
    }
    .btn:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(180,98,46,.38); }
    .btn:active { transform: translateY(0); }
    .error { background: #fce8e8; border: 1px solid #e8a0a0; color: #8B2020; border-radius: 8px; padding: 10px 14px; font-size: .88rem; margin-top: 16px; }
  </style>
</head>
<body>
  <div class="card">
    <a class="back-link" href="/">&#8592; Inicio</a>
    <h1>Bienvenido</h1>
    <p class="sub">Ingresa tus credenciales para continuar</p>

    <form method="POST" action="/publico/login" novalidate>
      <label for="username"><?= te('common.username', [], $lang) ?></label>
      <input id="username" name="username" type="text" autocomplete="username"
             placeholder="tu usuario" value="<?= htmlspecialchars($_POST['username'] ?? '') ?>" required>

      <label for="password"><?= te('common.password', [], $lang) ?></label>
      <input id="password" name="password" type="password" autocomplete="current-password" placeholder="••••••••" required>

      <button class="btn" type="submit">Ingresar</button>

      <?php if ($error): ?>
        <div class="error"><?= htmlspecialchars($error) ?></div>
      <?php endif; ?>
    </form>
  </div>
</body>
</html>
