/* Blocking, same-origin startup script: apply appearance before the app paints.
   Keep this independent of React so CSP needs no inline-script exception. */
(() => {
  if (window.agentTheme) return;
  const key = 'lc.workspace.appearance';
  const themes = {
    studio: { scheme: 'light', color: '#f6f8f7' },
    graphite: { scheme: 'dark', color: '#171b1d' },
    midnight: { scheme: 'dark', color: '#0c1321' },
    paper: { scheme: 'light', color: '#f3edde' },
  };
  const normalize = value => Object.hasOwn(themes, value) ? value : 'system';
  const listeners = new Set();
  let media;
  try { media = window.matchMedia('(prefers-color-scheme: dark)'); } catch { /* light fallback */ }
  let preference = 'system';
  try { preference = normalize(window.localStorage.getItem(key)); } catch { /* private browsing */ }
  let snapshot;
  function apply(value, persist = false) {
    preference = normalize(value);
    const theme = preference === 'system' ? (media?.matches ? 'graphite' : 'studio') : preference;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = themes[theme].scheme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = themes[theme].color;
    if (persist) {
      try { window.localStorage.setItem(key, preference); } catch { /* selection still works for this page */ }
    }
    if (snapshot?.preference === preference && snapshot.theme === theme) return;
    snapshot = Object.freeze({ preference, theme });
    listeners.forEach(listener => listener());
  }
  apply(preference);
  const changed = () => apply(preference);
  if (media?.addEventListener) media.addEventListener('change', changed);
  else if (media?.addListener) media.addListener(changed);
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
  window.agentTheme = Object.freeze({
    getSnapshot: () => snapshot,
    setPreference: value => apply(value, true),
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); },
  });
})();
