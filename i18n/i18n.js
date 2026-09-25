// Translation lookup for the browser dashboards (camera wall, MQTT monitor).
//
// Each app serves its own copy of the catalogues. There is no shared origin on
// purpose: these are different containers on different boxes, and fetching
// strings across them would make the camera wall depend on the hub being up to
// be legible.

(function (global) {
  'use strict';

  const state = { locale: 'en', catalogue: {}, fallback: {}, base: '/i18n' };

  async function load(base, locale) {
    state.base = base || state.base;
    const fetchOne = async (code) => {
      try {
        const res = await fetch(`${state.base}/${code}.json`, { cache: 'no-cache' });
        return res.ok ? await res.json() : {};
      } catch { return {}; }
    };
    // English always, as the fallback. A key with no translation renders the
    // English string rather than the key name.
    state.fallback = await fetchOne('en');
    state.locale = locale || 'en';
    state.catalogue = state.locale === 'en' ? state.fallback : await fetchOne(state.locale);
    return state.locale;
  }

  function t(key, params) {
    let text = state.catalogue[key] || state.fallback[key] || key;
    if (params) {
      for (const [name, value] of Object.entries(params)) {
        text = text.split('{' + name + '}').join(String(value));
      }
    }
    return text;
  }

  // Rewrites anything carrying data-i18n. Call once after load(), and again
  // after rendering new nodes.
  function apply(root) {
    (root || document).querySelectorAll('[data-i18n]').forEach((el) => {
      const key = el.getAttribute('data-i18n');
      const attr = el.getAttribute('data-i18n-attr');
      if (attr) el.setAttribute(attr, t(key));
      else el.textContent = t(key);
    });
  }

  global.i18n = { load, t, apply, get locale() { return state.locale; } };
})(window);
