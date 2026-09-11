import { useId, useSyncExternalStore } from 'react';
import { Check, Monitor } from 'lucide-react';

export const themes = [
  { id: 'studio', name: 'Studio Light', description: 'Soft white · restrained teal' },
  { id: 'graphite', name: 'Graphite', description: 'Charcoal · muted teal' },
  { id: 'midnight', name: 'Midnight', description: 'Deep navy · soft blue' },
  { id: 'paper', name: 'Paper', description: 'Warm ivory · olive' },
] as const;
export type Theme = typeof themes[number]['id'];
type Preference = Theme | 'system';
type Appearance = { preference: Preference; theme: Theme };
declare global {
  interface Window {
    agentTheme?: {
      getSnapshot: () => Appearance;
      setPreference: (value: Preference) => void;
      subscribe: (listener: () => void) => () => void;
    };
  }
}
const fallback: Appearance = { preference: 'system', theme: 'studio' };
const snapshot = () => typeof window === 'undefined' ? fallback : window.agentTheme?.getSnapshot() || fallback;
const subscribe = (listener: () => void) => typeof window === 'undefined' ? () => {} : window.agentTheme?.subscribe(listener) || (() => {});

export function AppearanceSettings() {
  const current = useSyncExternalStore(subscribe, snapshot, () => fallback);
  const name = useId();
  const select = (value: Preference) => window.agentTheme?.setPreference(value);
  const activeName = themes.find(theme => theme.id === current.theme)!.name;
  return <fieldset className="appearance-settings">
    <legend className="eyebrow">Appearance</legend>
    <p className="field-hint">Choose how your workspace looks. Saved in this browser for all projects.</p>
    <label className={'theme-system' + (current.preference === 'system' ? ' selected' : '')}>
      <input type="radio" name={name} value="system" checked={current.preference === 'system'} onChange={() => select('system')} />
      <Monitor size={18} /><span><strong>System</strong><small>{current.preference === 'system' ? `Follow your device · ${activeName}` : 'Follow your device’s light or dark appearance'}</small></span>
    </label>
    <div className="theme-options">
      {themes.map(theme => <label key={theme.id} className={'theme-option' + (current.preference === theme.id ? ' selected' : '')}>
        <input type="radio" name={name} value={theme.id} checked={current.preference === theme.id} onChange={() => select(theme.id)} />
        <span className="theme-swatch" data-theme={theme.id} aria-hidden="true">
          <span className="swatch-sidebar"><i /><i /><i /></span>
          <span className="swatch-chat"><i className="swatch-user" /><i /><i /><span><b /><b /></span></span>
          {current.preference === theme.id && <span className="swatch-check"><Check size={12} /></span>}
        </span>
        <span className="theme-label"><strong>{theme.name}</strong><small>{theme.description}</small></span>
      </label>)}
    </div>
  </fieldset>;
}
