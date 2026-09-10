import test from 'node:test';
import assert from 'node:assert/strict';
import { constrainWidth, DEFAULT_INSPECTOR_WIDTH, inspectorLimits, needsArtifactOverlay, keyboardInspectorWidth, savedInspectorWidth } from '../src/panel-size.ts';

test('desktop panel limits preserve at least 420px for chat with either sidebar state', () => {
  for (const viewport of [1001, 1100, 1250, 1251, 1440, 1599, 1600, 1920]) {
    for (const sidebar of [false, true]) {
      const { min, max } = inspectorLimits(viewport, sidebar);
      const left = !sidebar ? 0 : viewport >= 1600 ? 265 : viewport <= 1250 ? 220 : 246;
      assert.equal(min, 280);
      assert.ok(max >= min && max <= 680);
      assert.ok(viewport - left - max >= 420, `${viewport}px, sidebar ${sidebar}`);
      assert.equal(constrainWidth(9999, min, max), max);
      assert.equal(constrainWidth(-9999, min, max), min);
    }
  }
});
test('tablet panel leaves an exposed edge and mobile uses the full viewport', () => {
  for (const viewport of [701, 720, 900, 1000]) {
    const limits = inspectorLimits(viewport, true);
    assert.ok(limits.max <= viewport - 48);
    assert.deepEqual(limits, inspectorLimits(viewport, false));
  }
  for (const viewport of [320, 390, 700]) {
    const { min, max } = inspectorLimits(viewport, true);
    assert.equal(constrainWidth(600, min, max), viewport);
  }
});
test('responsive clamping preserves a saved preference for return to a wider window', () => {
  const preferred = savedInspectorWidth('620');
  for (const [viewport, expected] of [[1440, 620], [1100, 460], [390, 390], [1440, 620]]) {
    const { min, max } = inspectorLimits(viewport, true);
    assert.equal(constrainWidth(preferred, min, max), expected);
  }
});
test('missing or corrupt browser preferences fall back to a usable width', () => {
  for (const value of [null, '', ' ', 'NaN', 'Infinity', 'wide', '0', '-300', '279', '681']) {
    assert.equal(savedInspectorWidth(value), DEFAULT_INSPECTOR_WIDTH);
  }
  for (const value of ['280', '510', '680']) assert.equal(savedInspectorWidth(value), Number(value));
  assert.equal(constrainWidth(Number.NaN, 280, 680), DEFAULT_INSPECTOR_WIDTH);
});
test('keyboard moves the right panel edge in the pressed direction and respects bounds', () => {
  assert.equal(keyboardInspectorWidth('ArrowLeft', 400, 280, 600), 420);
  assert.equal(keyboardInspectorWidth('ArrowRight', 400, 280, 600), 380);
  assert.equal(keyboardInspectorWidth('ArrowLeft', 400, 280, 600, true), 450);
  assert.equal(keyboardInspectorWidth('ArrowRight', 400, 280, 600, true), 350);
  assert.equal(keyboardInspectorWidth('ArrowLeft', 590, 280, 600), 600);
  assert.equal(keyboardInspectorWidth('ArrowRight', 290, 280, 600), 280);
  assert.equal(keyboardInspectorWidth('Home', 400, 280, 600), 280);
  assert.equal(keyboardInspectorWidth('End', 400, 280, 600), 600);
  assert.equal(keyboardInspectorWidth('Tab', 400, 280, 600), undefined);
});

test('file preview switches to an overlay before it can squeeze the chat below 420px', () => {
  assert.equal(needsArtifactOverlay(1280, true, 614), true);
  assert.equal(needsArtifactOverlay(1440, true, 620), true);
  assert.equal(needsArtifactOverlay(1440, false, 598), false);
  assert.equal(needsArtifactOverlay(1440, false, 599), true);
  assert.equal(needsArtifactOverlay(1920, true, 680), false);
  // Smaller screens already have a full overlay layout in their media rules.
  assert.equal(needsArtifactOverlay(1000, true, 620), false);
});
