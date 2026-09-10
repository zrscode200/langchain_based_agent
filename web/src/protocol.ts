export type Json = Record<string, any>;
export type Mode = 'manual' | 'auto' | 'yolo';
export type Project = { id: string; name: string; path: string };
export type Workspace = { schema_version: number; workspace_id: string; cwd: string; project_root: string | null; generation: number; resource_key: string; config_fingerprint: string };
export type Thread = { thread_id: string; metadata: Json; status: string; updated_at?: string; values?: Json };
export type Message = { id?: string; type?: string; role?: string; content: unknown; name?: string; status?: string; artifact?: Json; tool_call_id?: string; tool_calls?: Json[]; tool_call_chunks?: Json[]; usage_metadata?: Json; additional_kwargs?: Json };
export type Interrupt = { id: string; value: Json };
export type Snapshot = { values: Json; interrupts?: Interrupt[]; tasks?: Json[]; next?: string[]; checkpoint?: Json | null };
export type Task = { task_id: string; name: string; description: string; status: string; result?: string; interrupts: Interrupt[]; steerable: boolean; activity?: Json[]; steering?: Json[]; conversation?: { text: string; page: number; pages: number; limited: boolean; notice: string } };
export type Skill = { name: string; description: string; path: string; source?: string };
export type Catalog = { skills: Skill[]; tools: { name: string; description: string }[]; model?: string; instructions?: { path: string; content: string }[]; capabilities?: Json; notice?: string };

export function textContent(content: unknown): string {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return content == null ? '' : JSON.stringify(content, null, 2);
  return content.filter(b => b && (b.type === 'text' || b.type === 'output_text')).map(b => b.text || '').join('');
}
export function reasoningContent(content: unknown): string {
  if (!Array.isArray(content)) return '';
  return content.filter(b => ['reasoning', 'thinking', 'reasoning_content'].includes(b?.type)).map(b => {
    const direct = b.reasoning ?? b.thinking ?? b.text;
    if (typeof direct === 'string') return direct;
    return Array.isArray(b.summary) ? b.summary.filter((s: Json) => typeof s?.text === 'string').map((s: Json) => s.text).join('\n') : '';
  }).join('');
}
export function messageReasoning(message: Message): string {
  // Some adapters expose the same reasoning in both locations. Prefer blocks.
  return reasoningContent(message.content) || (typeof message.additional_kwargs?.reasoning_content === 'string' ? message.additional_kwargs.reasoning_content : '');
}
export function pendingInterrupts(state: Snapshot): Interrupt[] {
  const entries = [...(state.interrupts || []), ...(state.tasks || []).flatMap(t => t.interrupts || [])];
  return [...new Map(entries.filter(i => typeof i.id === 'string').map(i => [i.id, i])).values()];
}
export function allowedDecisions(value: Json, name: string): string[] {
  return value.review_configs?.find((r: Json) => r.action_name === name)?.allowed_decisions || [];
}
export function validAnswer(question: Json, answer: unknown): boolean {
  if (typeof answer !== 'string') return false;
  if (question.type === 'multi_select') {
    try {
      const values = JSON.parse(answer);
      return Array.isArray(values) && values.every(v => typeof v === 'string' && v.trim()) && (question.required === false || values.length > 0);
    } catch { return false; }
  }
  return question.required === false || Boolean(answer.trim());
}
export function mergeChunk(messages: Message[], chunk: Message): Message[] {
  if (!chunk.id) return messages; // An unidentified fragment cannot safely be replayed.
  const index = messages.findIndex(m => m.id === chunk.id);
  const old = index < 0 ? { content: '' } as Message : messages[index];
  const content = typeof old.content === 'string' && typeof chunk.content === 'string'
    ? old.content + chunk.content
    : [...(Array.isArray(old.content) ? old.content : [{ type: 'text', text: textContent(old.content) }]), ...(Array.isArray(chunk.content) ? chunk.content : [{ type: 'text', text: textContent(chunk.content) }])];
  const pieces = [...(old.tool_call_chunks || [])];
  for (const part of chunk.tool_call_chunks || []) {
    const at = pieces.findIndex(p => p.index === part.index);
    const previous = at < 0 ? {} : pieces[at];
    const merged = { ...previous, ...part, name: (previous.name || '') + (part.name || ''), id: previous.id || part.id, args: (previous.args || '') + (part.args || '') };
    if (at < 0) pieces.push(merged); else pieces[at] = merged;
  }
  const toolCalls = pieces.length ? pieces.map(p => {
    let args: unknown = p.args, partial = true;
    try { args = JSON.parse(p.args || '{}'); partial = false; } catch { /* display partial arguments until complete */ }
    return { id: p.id, name: p.name, args, partial };
  }) : chunk.tool_calls?.length ? chunk.tool_calls : old.tool_calls;
  const additional = { ...old.additional_kwargs, ...chunk.additional_kwargs };
  const delta = chunk.additional_kwargs?.reasoning_content;
  if (typeof delta === 'string') additional.reasoning_content = (typeof old.additional_kwargs?.reasoning_content === 'string' ? old.additional_kwargs.reasoning_content : '') + delta;
  const merged = { ...old, ...chunk, additional_kwargs: additional, content, tool_call_chunks: pieces, tool_calls: toolCalls };
  return index < 0 ? [...messages, merged] : messages.map((m, i) => i === index ? merged : m);
}

/** Node updates contain completed messages, including tool results, not token deltas. */
export function mergeUpdate(messages: Message[], update: unknown): Message[] {
  if (!update || typeof update !== 'object' || Array.isArray(update)) return messages;
  let result = messages;
  for (const node of Object.values(update)) {
    if (!node || typeof node !== 'object' || !Array.isArray((node as Json).messages)) continue;
    for (const message of (node as Json).messages as Message[]) {
      if (!message || typeof message.id !== 'string') continue;
      const at = result.findIndex(m => m.id === message.id);
      if (at < 0) result = [...result, message];
      else result = result.map((old, i) => i === at ? { ...old, ...message, ...(old.additional_kwargs || message.additional_kwargs ? { additional_kwargs: { ...old.additional_kwargs, ...message.additional_kwargs } } : {}) } : old);
    }
  }
  return result;
}

export type StreamEvent = { event: string; id: string; data: any };
/** Incremental SSE parser: CRLF, multiline data, chunk boundaries and UTF-8. */
export async function readSSE(response: Response, receive: (event: StreamEvent) => void) {
  if (!response.ok) throw new Error(await response.text());
  if (!response.body) throw new Error('The server returned an empty stream.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', data: string[] = [], event = 'message', id = '';
  const line = (value: string) => {
    if (!value) {
      if (data.length) { const raw = data.join('\n'); let parsed: unknown = raw; try { parsed = JSON.parse(raw); } catch { /* literal custom events */ } receive({ event, id, data: parsed }); }
      data = []; event = 'message'; return;
    }
    if (value.startsWith(':')) return;
    const colon = value.indexOf(':');
    const key = colon < 0 ? value : value.slice(0, colon);
    const val = colon < 0 ? '' : value.slice(colon + 1).replace(/^ /, '');
    if (key === 'data') data.push(val);
    if (key === 'event') event = val;
    if (key === 'id' && !val.includes('\0')) id = val;
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let newline: number;
      while ((newline = buffer.indexOf('\n')) >= 0) { line(buffer.slice(0, newline).replace(/\r$/, '')); buffer = buffer.slice(newline + 1); }
      if (done) break;
    }
    if (buffer) line(buffer.replace(/\r$/, ''));
    line('');
  } finally { reader.releaseLock(); }
}
