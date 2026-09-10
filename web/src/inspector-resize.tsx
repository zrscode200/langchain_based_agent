import { useEffect, useRef, useState } from 'react';
import { constrainWidth, DEFAULT_INSPECTOR_WIDTH, INSPECTOR_WIDTH_KEY, inspectorLimits, needsArtifactOverlay, keyboardInspectorWidth, savedInspectorWidth } from './panel-size.ts';

export function useInspectorSize(sidebar: boolean) {
  const [preferred, setPreferred] = useState(() => {
    try { return savedInspectorWidth(localStorage.getItem(INSPECTOR_WIDTH_KEY)); }
    catch { return DEFAULT_INSPECTOR_WIDTH; }
  });
  const latest = useRef(preferred);
  const [viewport, setViewport] = useState(window.innerWidth);
  const [dragging, setDragging] = useState(false);
  useEffect(() => {
    const resize = () => setViewport(window.innerWidth);
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);
  const limits = inspectorLimits(viewport, sidebar);
  const width = constrainWidth(preferred, limits.min, limits.max);
  const change = (next: number) => {
    latest.current = constrainWidth(next, limits.min, limits.max);
    setPreferred(latest.current);
  };
  const save = () => { try { localStorage.setItem(INSPECTOR_WIDTH_KEY, String(latest.current)); } catch { /* Still usable when browser storage is unavailable. */ } };
  const reset = () => { latest.current = DEFAULT_INSPECTOR_WIDTH; setPreferred(DEFAULT_INSPECTOR_WIDTH); save(); };
  return { width, ...limits, change, save, reset, dragging, setDragging, overlayArtifact: needsArtifactOverlay(viewport, sidebar, width) };
}
type Sizing = ReturnType<typeof useInspectorSize>;
export function InspectorResizeHandle({ sizing }: { sizing: Sizing }) {
  const gesture = useRef<{ id: number; x: number; width: number } | null>(null);
  const finish = () => {
    if (!gesture.current) return;
    gesture.current = null; sizing.setDragging(false); sizing.save();
  };
  useEffect(() => () => sizing.setDragging(false), [sizing.setDragging]);
  return <div className="inspector-resize-handle" role="separator" tabIndex={0}
    aria-label="Resize right sidebar" aria-orientation="vertical" aria-controls="workspace-inspector"
    aria-valuemin={sizing.min} aria-valuemax={sizing.max} aria-valuenow={sizing.width} aria-valuetext={`${sizing.width} pixels`}
    title="Drag to resize. Use arrow keys to adjust, or double-click to reset."
    onPointerDown={event => {
      if (event.button !== 0 || !event.isPrimary) return;
      event.preventDefault(); event.currentTarget.focus(); event.currentTarget.setPointerCapture(event.pointerId);
      gesture.current = { id: event.pointerId, x: event.clientX, width: sizing.width };
      sizing.setDragging(true);
    }}
    onPointerMove={event => {
      const start = gesture.current;
      if (start?.id === event.pointerId) sizing.change(start.width + start.x - event.clientX);
    }}
    onPointerUp={event => {
      if (gesture.current?.id !== event.pointerId) return;
      finish();
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    }}
    onPointerCancel={finish} onLostPointerCapture={finish}
    onDoubleClick={() => sizing.reset()}
    onKeyDown={event => {
      const next = keyboardInspectorWidth(event.key, sizing.width, sizing.min, sizing.max, event.shiftKey);
      if (next === undefined) return;
      event.preventDefault(); sizing.change(next); sizing.save();
    }} />;
}
