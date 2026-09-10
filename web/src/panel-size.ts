export const DEFAULT_INSPECTOR_WIDTH = 342;
export const INSPECTOR_WIDTH_KEY = 'lc.workspace.inspectorWidth';

function navigationWidth(viewport: number, sidebar: boolean) {
  return !sidebar ? 0 : viewport >= 1600 ? 265 : viewport <= 1250 ? 220 : 246;
}
export function needsArtifactOverlay(viewport: number, sidebar: boolean, inspectorWidth: number) {
  // Chat and file preview share the center equally; each needs at least 420px, plus the divider.
  return viewport > 1000 && viewport - navigationWidth(viewport, sidebar) - inspectorWidth < 842;
}
export function inspectorLimits(viewport: number, sidebar: boolean) {
  if (viewport <= 700) return { min: viewport, max: viewport };
  const sidebarWidth = navigationWidth(viewport, sidebar);
  const available = viewport <= 1000 ? viewport - 48 : viewport - sidebarWidth - 420;
  return { min: 280, max: Math.max(280, Math.min(680, available)) };
}
export function constrainWidth(width: number, min: number, max: number) {
  return Math.round(Math.max(min, Math.min(max, Number.isFinite(width) ? width : DEFAULT_INSPECTOR_WIDTH)));
}
export function savedInspectorWidth(value: string | null) {
  if (!value?.trim()) return DEFAULT_INSPECTOR_WIDTH;
  const width = Number(value);
  return Number.isFinite(width) && width >= 280 && width <= 680 ? width : DEFAULT_INSPECTOR_WIDTH;
}
export function keyboardInspectorWidth(key: string, width: number, min: number, max: number, shift = false): number | undefined {
  const step = shift ? 50 : 20;
  if (key === 'ArrowLeft') return constrainWidth(width + step, min, max);
  if (key === 'ArrowRight') return constrainWidth(width - step, min, max);
  if (key === 'Home') return min;
  if (key === 'End') return max;
  return undefined;
}
