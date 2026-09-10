import { messageReasoning, textContent, type Interrupt, type Json, type Message } from './protocol.ts';

export type Turn = { id: string; user?: Message; messages: Message[] };
export type DecisionReceipt = { id: string; afterMessage?: string; interrupts: Interrupt[]; responses: Json };
export type ToolState = 'requested' | 'awaiting_approval' | 'succeeded' | 'result' | 'failed' | 'cancelled' | 'unavailable';
export const isUser = (message: Message) => ['human', 'user', 'HumanMessage'].includes(message.type || message.role || '');
export const isTool = (message: Message) => ['tool', 'ToolMessage', 'ToolMessageChunk'].includes(message.type || message.role || '');

export function receiptAnchor(receipt: DecisionReceipt, messages: Message[]): string | undefined {
  const anchor = messages.find(m => m.id && m.id === receipt.afterMessage);
  if (!anchor) return undefined; // Its context may have been compacted; never attach it to a newer turn.
  if (isTool(anchor) && anchor.tool_call_id) {
    for (let i = messages.indexOf(anchor) - 1; i >= 0; i--) {
      if (isUser(messages[i])) break;
      if (messages[i].tool_calls?.some(call => call.id === anchor.tool_call_id)) return messages[i].id || anchor.id;
    }
  }
  return anchor.id;
}

export function conversationTurns(messages: Message[]): Turn[] {
  const turns: Turn[] = [];
  for (const [i, message] of messages.entries()) {
    if (isUser(message)) turns.push({ id: message.id || `user-${i}`, user: message, messages: [] });
    else {
      if (['system', 'SystemMessage'].includes(message.type || message.role || '')) continue;
      if (!turns.length) turns.push({ id: 'retained-context', messages: [] });
      turns[turns.length - 1].messages.push(message);
    }
  }
  return turns;
}

const stringArg = (args: Json, ...keys: string[]) => keys.map(key => args[key]).find(value => typeof value === 'string') || '';
export function describeTool(call: Json) {
  const args = call.args && typeof call.args === 'object' && !Array.isArray(call.args) ? call.args : {};
  const target = stringArg(args, 'file_path', 'path', 'filename');
  const command = stringArg(args, 'command', 'cmd');
  const name = String(call.name || 'Tool');
  if (['read_file', 'read_text_file'].includes(name)) return { label: 'Read file', target, kind: 'read', args };
  if (['write_file', 'write_text_file'].includes(name)) return { label: 'Write file', target, kind: 'write', args };
  if (['edit_file', 'replace_in_file'].includes(name)) return { label: 'Edit file', target, kind: 'edit', args };
  if (['execute', 'shell', 'run_command', 'execute_command', 'run_shell'].includes(name)) return { label: 'Run command', target: command, kind: 'command', args };
  if (['grep', 'glob', 'search_files', 'list_files', 'ls'].includes(name)) return { label: name === 'ls' || name === 'list_files' ? 'List files' : 'Search files', target: stringArg(args, 'pattern', 'query', 'path'), kind: 'search', args };
  if (['task', 'run_background_tasks'].includes(name)) return { label: 'Delegate work', target: stringArg(args, 'description', 'name'), kind: 'task', args };
  return { label: name.replaceAll('_', ' '), target, kind: 'generic', args };
}

export function toolOutput(call: Json, messages: Message[]) {
  return typeof call.id === 'string' && call.id ? messages.find(m => isTool(m) && m.tool_call_id === call.id) : undefined;
}
export function toolState(call: Json, messages: Message[], active: boolean, interrupts: Interrupt[]): ToolState {
  const output = toolOutput(call, messages);
  if (output) {
    if (output.status === 'error' || output.status === 'failed') return 'failed';
    if (output.status === 'cancelled') return 'cancelled';
    if (typeof output.artifact?.exit_code === 'number' && Number.isFinite(output.artifact.exit_code) && output.artifact.exit_code !== 0) return 'failed';
    return ['success', 'succeeded'].includes(output.status || '') ? 'succeeded' : 'result';
  }
  // Only link an approval to a tool when the backend supplies its exact ID.
  if (call.id && interrupts.some(i => i.value.action_requests?.some((a: Json) => (a.tool_call_id || a.id) === call.id))) return 'awaiting_approval';
  return active ? 'requested' : 'unavailable';
}
/** Streamed and saved human messages have different IDs; the assistant ID is stable. */
export function turnActivityKey(turn: Turn) {
  return 'work:' + (turn.messages.find(m => !isTool(m) && m.id)?.id || turn.id);
}
export function runDescription(messages: Message[], running: boolean, interrupts: Interrupt[], disconnected = false) {
  if (interrupts.length) return interrupts.some(i => i.value.type === 'ask_user') ? 'Waiting for your answer' : 'Waiting for your approval';
  if (disconnected) return 'Connection interrupted';
  if (!running) return '';
  const turn = conversationTurns(messages).at(-1);
  const last = turn?.messages.filter(m => !isTool(m)).at(-1);
  const pending = last?.tool_calls?.filter(call => !toolOutput(call, turn?.messages || [])) || [];
  if (pending.length) return pending.length === 1 ? `Requested: ${describeTool(pending[0]).label.toLowerCase()}` : `${pending.length} actions requested`;
  if (last && messageReasoning(last) && !textContent(last.content)) return 'Reasoning';
  if (last && textContent(last.content) && !isTool(turn?.messages.at(-1) || last)) return 'Writing a response';
  return 'Agent is working';
}
