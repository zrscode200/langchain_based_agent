import type { Message } from './protocol.ts';

export function mergeTaskMessages(previous: Message[], incoming: Message[], start = 0) {
  const records = new Map(previous.map(m => [m.id, m]));
  for (const message of incoming) {
    if (!message.id || !message._transcript || message._transcript.order < start) continue;
    const old = records.get(message.id);
    if (!old || (old._transcript?.revision || 0) < message._transcript.revision) records.set(message.id, message);
  }
  return [...records.values()].sort((a, b) => (a._transcript?.order || 0) - (b._transcript?.order || 0));
}

// A history fetch must observe the server after the previous live cursor update.
export class TaskReadQueue {
  pending = 0;
  private tail: Promise<void> = Promise.resolve();
  async read<T>(operation: () => Promise<T>): Promise<T> {
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise(resolve => { release = resolve; });
    this.pending++;
    await previous;
    try { return await operation(); }
    finally { this.pending--; release(); }
  }
}
