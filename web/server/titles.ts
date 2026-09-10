import { initialTitle } from '../src/conversations.ts';
import type { Json } from '../src/protocol.ts';

type Backend = (route: string, method?: string, body?: unknown) => Promise<Json>;

// Serialize title updates from this web adapter so an accepted turn cannot
// overwrite a simultaneous manual rename from another browser tab.
export function conversationTitles(backend: Backend) {
  const pending = new Map<string, Promise<void>>();
  function serial<T>(thread: string, action: () => Promise<T>): Promise<T> {
    const result = (pending.get(thread) || Promise.resolve()).then(action);
    const settled = result.then(() => {}, () => {});
    pending.set(thread, settled);
    void settled.then(() => { if (pending.get(thread) === settled) pending.delete(thread); });
    return result;
  }
  return {
    read(thread: string) {
      return serial(thread, () => backend(`/threads/${thread}`));
    },
    rename(thread: string, title: string) {
      return serial(thread, () => backend(`/threads/${thread}`, 'PATCH', { metadata: { title: title.trim(), title_source: 'manual' } }));
    },
    initialize(thread: string, text: string) {
      return serial(thread, async () => {
        const row = await backend(`/threads/${thread}`);
        if (row.metadata?.title) return row;
        return backend(`/threads/${thread}`, 'PATCH', { metadata: { title: initialTitle(text), title_source: 'initial' } });
      });
    },
  };
}
