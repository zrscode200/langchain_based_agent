import { textContent, type Thread, type Message } from './protocol.ts';

export function initialTitle(text: string): string {
  return Array.from(text.trim().replace(/\s+/g, ' ')).slice(0, 70).join('');
}

export function conversationTitle(thread?: Thread): string {
  return thread?.metadata?.title || initialTitle(textContent(thread?.values?.messages?.find((m: Message) => ['human', 'user'].includes(m.type || m.role || ''))?.content)) || 'New conversation';
}
