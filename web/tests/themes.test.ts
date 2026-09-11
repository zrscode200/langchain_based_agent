import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { AppearanceSettings, themes } from '../src/appearance.tsx';

const script = readFileSync(new URL('../public/theme-init.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../src/themes.css', import.meta.url), 'utf8');
const key = 'lc.workspace.appearance';

function start({ preference, dark = false, storageBlocked = false, mediaBlocked = false, legacy = false }: {
  preference?: string; dark?: boolean; storageBlocked?: boolean; mediaBlocked?: boolean; legacy?: boolean;
} = {}) {
  const saved = new Map<string, string>(preference ? [[key, preference]] : []);
  const events: Record<string, (value: any) => void> = {};
  let mediaChanged = () => {};
  const media: any = { matches: dark };
  if (legacy) media.addListener = (fn: () => void) => { mediaChanged = fn; };
  else media.addEventListener = (_: string, fn: () => void) => { mediaChanged = fn; };
  const root = { dataset: {} as Record<string, string>, style: { colorScheme: '' } };
  const meta = { content: '' };
  const browser: any = {
    matchMedia: () => { if (mediaBlocked) throw new Error('unavailable'); return media; },
    localStorage: {
      getItem: (k: string) => { if (storageBlocked) throw new Error('blocked'); return saved.get(k) || null; },
      setItem: (k: string, v: string) => { if (storageBlocked) throw new Error('blocked'); saved.set(k, v); },
    },
    addEventListener: (name: string, fn: (event: any) => void) => { events[name] = fn; },
  };
  const context = vm.createContext({ window: browser, document: { documentElement: root, querySelector: () => meta } });
  vm.runInContext(script, context);
  return { store: browser.agentTheme, saved, root, meta,
    system: (dark: boolean) => { media.matches = dark; mediaChanged(); },
    storage: (value: string | null, changedKey: string | null = key) => events.storage({ key: changedKey, newValue: value }),
    again: () => vm.runInContext(script, context) };
}

test('System applies before React and follows OS changes without persisting a fixed choice', () => {
  const app = start({ dark: true });
  assert.equal(app.root.dataset.theme, 'graphite');
  assert.equal(app.root.style.colorScheme, 'dark');
  assert.equal(app.meta.content, '#171b1d');
  assert.equal(app.store.getSnapshot().preference, 'system');
  let notified = 0;
  const unsubscribe = app.store.subscribe(() => notified++);
  app.system(false);
  assert.equal(app.root.dataset.theme, 'studio');
  assert.equal(app.root.style.colorScheme, 'light');
  assert.equal(notified, 1);
  assert.equal(app.saved.has(key), false);
  const snapshot = app.store.getSnapshot();
  app.system(false);
  assert.equal(app.store.getSnapshot(), snapshot);
  unsubscribe(); app.system(true);
  assert.equal(notified, 1);
});

test('each explicit theme survives reload and ignores OS changes', () => {
  for (const { id } of themes) {
    const app = start();
    app.store.setPreference(id);
    assert.equal(app.saved.get(key), id);
    app.system(true); app.system(false);
    assert.equal(app.root.dataset.theme, id);
    assert.equal(start({ preference: app.saved.get(key), dark: true }).root.dataset.theme, id);
    app.store.setPreference('system');
    app.system(true);
    assert.equal(app.root.dataset.theme, 'graphite');
  }
});

test('cross-tab selection and clearing synchronize without rewriting storage', () => {
  const app = start({ preference: 'paper', dark: true });
  app.storage('midnight');
  assert.equal(app.root.dataset.theme, 'midnight');
  assert.equal(app.saved.get(key), 'paper');
  app.storage('studio', 'unrelated-key');
  assert.equal(app.root.dataset.theme, 'midnight');
  app.storage(null, null);
  assert.equal(app.root.dataset.theme, 'graphite');
  assert.equal(app.store.getSnapshot().preference, 'system');
});

test('invalid preferences and restricted browser APIs degrade safely', () => {
  for (const preference of ['unknown', '__proto__', 'constructor']) {
    assert.equal(start({ preference, dark: true }).root.dataset.theme, 'graphite');
  }
  const app = start({ storageBlocked: true, mediaBlocked: true });
  assert.equal(app.root.dataset.theme, 'studio');
  app.store.setPreference('paper');
  assert.equal(app.root.dataset.theme, 'paper');
  const legacy = start({ legacy: true });
  legacy.system(true);
  assert.equal(legacy.root.dataset.theme, 'graphite');
  const store = legacy.store;
  legacy.again();
  assert.equal(legacy.store, store);
});

test('appearance offers one native radio group with labelled palette previews', () => {
  const html = renderToStaticMarkup(createElement(AppearanceSettings));
  assert.equal((html.match(/type="radio"/g) || []).length, 5);
  assert.equal(new Set([...html.matchAll(/name="([^"]+)"/g)].map(m => m[1])).size, 1);
  assert.match(html, /<legend[^>]*>Appearance<\/legend>/);
  assert.match(html, /Saved in this browser/);
  for (const theme of themes) assert.ok(html.includes(theme.name));
});

const palettes = Object.fromEntries([...css.matchAll(/\[data-theme="([^"]+)"\]\s*\{([^}]+)\}/g)]
  .map(m => [m[1], Object.fromEntries([...m[2].matchAll(/--([\w-]+):\s*(#[a-f0-9]+);/g)].map(c => [c[1], c[2]]))]));
function contrast(a: string, b: string) {
  const luminance = (hex: string) => {
    const rgb = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map(n => n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4);
    return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
  };
  const x = luminance(a), y = luminance(b);
  return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
}
test('all palettes define the same roles and maintain readable text and controls', () => {
  const roles = Object.keys(palettes.studio).sort();
  assert.deepEqual(Object.keys(palettes), themes.map(theme => theme.id));
  for (const [name, colors] of Object.entries(palettes)) {
    assert.deepEqual(Object.keys(colors).sort(), roles, name);
    const check = (fg: string, bg: string, minimum = 4.5) => {
      const ratio = contrast(colors[fg], colors[bg]);
      assert.ok(ratio >= minimum, `${name}: ${fg} on ${bg} = ${ratio.toFixed(2)}, needs ${minimum}`);
    };
    for (const bg of ['page', 'surface', 'surface-raised', 'surface-subtle', 'sidebar', 'surface-hover', 'accent-soft']) {
      for (const fg of ['ink', 'secondary', 'muted']) check(fg, bg);
    }
    for (const state of ['success', 'warning', 'danger', 'info']) {
      check(state, state + '-bg'); check(state, state + '-soft'); check(state, 'surface');
    }
    check('accent', 'surface'); check('on-accent', 'accent'); check('on-accent', 'accent-hover');
    check('code-ink', 'code-bg'); check('control-border', 'surface', 3); check('accent', 'surface-subtle', 3);
  }
});

test('actual button hover and resize focus styles stay readable in every palette', () => {
  const source = ['styles.css', 'tasks.css'].map(name => readFileSync(new URL('../src/' + name, import.meta.url), 'utf8')).join('\n');
  const rules = [...source.matchAll(/([^{}]+)\{([^{}]+)\}/g)];
  const role = (selector: string, property: string) => {
    const rule = rules.find(m => m[1].trim().split(',').includes(selector));
    assert.ok(rule, `missing selector: ${selector}`);
    const declaration = rule[2].split(';').find(d => d.trim().startsWith(property + ':'));
    const variable = declaration?.match(/var\(--([\w-]+)\)/)?.[1];
    assert.ok(variable, `missing themed ${property}: ${selector}`);
    return variable === 'focus' ? css.match(/--focus:\s*var\(--([\w-]+)\)/)![1] : variable;
  };
  const pairs = [
    ['ink', role('.button:hover:not(:disabled)', 'background'), 4.5],
    [role('.icon-button:hover:not(:disabled)', 'color'), role('.icon-button:hover:not(:disabled)', 'background'), 3],
    [role('.button.primary', 'color'), role('.button.primary:hover:not(:disabled)', 'background'), 4.5],
    [role('.send-button', 'color'), role('.send-button:hover:not(:disabled)', 'background'), 4.5],
    [role('.attention-count', 'color'), role('.attention-count', 'background'), 4.5],
    [role('.task-review', 'color'), role('button.task-review:hover:not(:disabled)', 'background'), 4.5],
    [role('.inspector-resize-handle:focus-visible:after', 'background'), 'surface', 3],
    [role('.inspector-resize-handle:focus-visible', 'box-shadow'), 'surface-subtle', 3],
  ] as const;
  for (const [name, colors] of Object.entries(palettes)) {
    for (const [fg, bg, minimum] of pairs) {
      const ratio = contrast(colors[fg], colors[bg]);
      assert.ok(ratio >= minimum, `${name}: ${fg} on ${bg} = ${ratio.toFixed(2)}, needs ${minimum}`);
    }
  }
});

test('component colors use defined theme roles and startup respects CSP', () => {
  const roles = new Set([...css.matchAll(/--([\w-]+):/g)].map(m => m[1]));
  for (const name of ['styles.css', 'chat.css', 'tasks.css', 'preview.css', 'appearance.css']) {
    const source = readFileSync(new URL('../src/' + name, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /#[0-9a-f]{3,8}\b|:\s*white\b/i, name);
    for (const variable of source.matchAll(/var\(--([\w-]+)\)/g)) assert.ok(roles.has(variable[1]), `${name}: ${variable[1]}`);
  }
  for (const name of ['index.html', 'chat-preview.html']) {
    const html = readFileSync(new URL('../' + name, import.meta.url), 'utf8');
    assert.ok(html.indexOf('src="/theme-init.js"') < html.indexOf('<body>'));
    assert.match(html, /href="\/src\/themes.css"/);
    assert.doesNotMatch(html, /<script[^>]*(?:async|defer)[^>]*theme-init/);
  }
});
