<?php
// Translation lookup for the entry page.
//
// Reads the same flat catalogues the Flask apps and the browser dashboards do.
// English is the fallback, for the same reason: an untranslated word beats a
// key name on a screen someone is looking at.

function home_stack_locale_dir(): string {
    return getenv('HOME_STACK_I18N_DIR') ?: __DIR__;
}

function home_stack_catalogue(string $locale): array {
    static $cache = [];
    if (isset($cache[$locale])) return $cache[$locale];
    $path = home_stack_locale_dir() . "/{$locale}.json";
    $cache[$locale] = is_readable($path)
        ? (json_decode(file_get_contents($path), true) ?: [])
        : [];
    return $cache[$locale];
}

function home_stack_available_locales(): array {
    $out = [];
    foreach (glob(home_stack_locale_dir() . '/*.json') as $path) {
        $out[] = basename($path, '.json');
    }
    sort($out);
    return $out;
}

// Order of preference: an explicit ?lang=, then the session, then what the
// browser asks for, then the house default. A panel on a wall never sends
// Accept-Language worth trusting, which is why the configured default wins
// over nothing rather than over everything.
function home_stack_pick_locale(): string {
    $available = home_stack_available_locales();
    $default = getenv('HOME_STACK_DEFAULT_LOCALE') ?: 'en';

    if (isset($_GET['lang']) && in_array($_GET['lang'], $available, true)) {
        if (session_status() === PHP_SESSION_ACTIVE) $_SESSION['lang'] = $_GET['lang'];
        return $_GET['lang'];
    }
    if (isset($_SESSION['lang']) && in_array($_SESSION['lang'], $available, true)) {
        return $_SESSION['lang'];
    }
    foreach (explode(',', $_SERVER['HTTP_ACCEPT_LANGUAGE'] ?? '') as $part) {
        $code = strtolower(substr(trim(explode(';', $part)[0]), 0, 2));
        if (in_array($code, $available, true)) return $code;
    }
    return in_array($default, $available, true) ? $default : 'en';
}

function t(string $key, array $params = [], ?string $locale = null): string {
    $locale = $locale ?: home_stack_pick_locale();
    $text = home_stack_catalogue($locale)[$key]
        ?? home_stack_catalogue('en')[$key]
        ?? $key;
    foreach ($params as $name => $value) {
        $text = str_replace('{' . $name . '}', (string) $value, $text);
    }
    return $text;
}

// Escaped by default. Every call site on a page renders into HTML.
function te(string $key, array $params = [], ?string $locale = null): string {
    return htmlspecialchars(t($key, $params, $locale), ENT_QUOTES, 'UTF-8');
}
