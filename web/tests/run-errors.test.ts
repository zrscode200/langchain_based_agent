import test from 'node:test';
import assert from 'node:assert/strict';
import { AgentRunError, runFailure } from '../src/run-errors.ts';

test('server failure does not claim a disconnected server or suggest resending', () => {
  const failure = runFailure(new AgentRunError('An internal error occurred'), true);
  assert.equal(failure.disconnected, false);
  assert.match(failure.message, /failed on the server/);
  assert.doesNotMatch(failure.message, /Reconnect/);
  assert.match(failure.message, /will not be resent automatically/);
});
test('transport loss retains reconnect guidance only after submission', () => {
  const error = new Error('Failed to fetch');
  assert.equal(runFailure(error, true).disconnected, true);
  assert.match(runFailure(error, true).message, /Reconnect/);
  assert.equal(runFailure(error, false).message, 'Failed to fetch');
});
