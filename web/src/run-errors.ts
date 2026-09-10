/** An error event is a server-reported run failure, not a lost connection. */
export class AgentRunError extends Error {}

export function runFailure(error: Error, submitted: boolean) {
  if (error instanceof AgentRunError) {
    const detail = error.message === 'An internal error occurred' ? 'The agent run failed on the server.' : error.message;
    return { message: detail + ' Refresh to inspect the conversation. Your message will not be resent automatically.', disconnected: false };
  }
  return { message: error.message + (submitted ? ' Reconnect to inspect the existing run; your message will not be resent.' : ''), disconnected: true };
}
