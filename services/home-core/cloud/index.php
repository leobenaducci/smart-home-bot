<?php
session_set_cookie_params(['lifetime' => 3600]);
ini_set('session.gc_maxlifetime', 3600);
session_start();

define('SRC',        __DIR__ . '/src');
define('USERS_FILE', __DIR__ . '/users.json');

$path = rtrim(parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH), '/') ?: '/';

if ($path === '/')                          { require SRC . '/pages/home.php'; }
elseif ($path === '/publico'
     || $path === '/publico/login')         { require SRC . '/pages/login.php'; }
elseif ($path === '/publico/contactos')     { require SRC . '/pages/contactos.php'; }
elseif (str_starts_with($path, '/privado'))   { require SRC . '/pages/privado.php'; }
else                                        { http_response_code(404); echo '404 Not Found'; }
