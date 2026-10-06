import { useCallback, useEffect, useRef, useState } from 'react';

export const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8080';

const STORAGE_KEY = 'nnt.conversations';

export class UnauthorizedError extends Error {}

// Render's free tier sleeps when idle and takes up to ~a minute to wake, so timeouts are generous
export const REQUEST_TIMEOUT_MS = 90_000;
const REFRESH_WITHIN_MS = 10 * 60_000; // renew the token once it has less than this left

export function timeoutMessage() {
  return 'The server took too long to respond. It may be waking up from sleep, so try again in a moment.';
}

export function authHeaders() {
  const token = localStorage.getItem('token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Milliseconds since epoch when the stored token expires; 0 when missing or unreadable
export function tokenExpiry() {
  try {
    const token = localStorage.getItem('token');
    return JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))).exp * 1000;
  } catch {
    return 0;
  }
}

let refreshing = null;

// Renews the token if it is close to expiring. Throws UnauthorizedError once it has already expired.
export function ensureFreshToken() {
  const left = tokenExpiry() - Date.now();
  if (left <= 0) return Promise.reject(new UnauthorizedError());
  if (left > REFRESH_WITHIN_MS) return Promise.resolve();
  // Concurrent requests share one refresh call
  refreshing ??= fetch(`${API_URL}/api/auth/refresh`, {
    method: 'POST',
    headers: authHeaders(),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
    .then(async response => {
      if (response.status === 401) throw new UnauthorizedError();
      if (response.ok) localStorage.setItem('token', (await response.json()).access_token);
      // Other failures (network, cold start) are ignored; the current token is still valid
    })
    .catch(err => {
      if (err instanceof UnauthorizedError) throw err;
    })
    .finally(() => {
      refreshing = null;
    });
  return refreshing;
}

// fetch() against the API with a fresh token, a timeout, and 401 mapped to UnauthorizedError.
// `timeout` only bounds the wait for the response headers, so streamed bodies can run longer.
export async function apiFetch(path, { timeout = REQUEST_TIMEOUT_MS, signal, headers, ...options } = {}) {
  await ensureFreshToken();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new DOMException(timeoutMessage(), 'TimeoutError')), timeout);
  const onAbort = () => controller.abort(signal.reason);
  signal?.addEventListener('abort', onAbort, { once: true });
  if (signal?.aborted) onAbort();
  try {
    const response = await fetch(`${API_URL}${path}`, {
      ...options,
      headers: { ...authHeaders(), ...headers },
      signal: controller.signal,
    });
    if (response.status === 401) throw new UnauthorizedError();
    return response;
  } catch (err) {
    if (controller.signal.reason?.name === 'TimeoutError') throw new Error(timeoutMessage());
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export async function errorDetail(response) {
  try {
    const data = await response.json();
    if (typeof data.detail === 'string') return data.detail;
  } catch {
    // not JSON
  }
  return `Request failed (${response.status})`;
}

// What an @ in the composer can point at: { clients: [...], references: [...] }
export async function fetchMentions(query, kind, signal) {
  const response = await apiFetch(`/api/mentions?q=${encodeURIComponent(query)}&kind=${kind}`, { signal });
  if (!response.ok) throw new Error(await errorDetail(response));
  return response.json();
}

// Uploads one reference image; resolves to { url, width, height }.
export async function uploadImage(file) {
  const form = new FormData();
  form.append('file', file);
  const response = await apiFetch('/api/uploads/image', { method: 'POST', body: form, timeout: 180_000 });
  if (!response.ok) throw new Error(await errorDetail(response));
  return response.json();
}

function loadConversations() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)) || [];
    // A reload mid-stream would otherwise leave a message stuck "pending" forever
    return saved.map(c => ({
      ...c,
      messages: c.messages.map(m => (m.pending ? { ...m, pending: false, status: '' } : m)),
    }));
  } catch {
    return [];
  }
}

function newId() {
  return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
}

// Holds every conversation (persisted to localStorage) and streams replies from /api/chat.
// The backend keeps each conversation's real history (LangGraph thread), keyed by its id.
export function useConversations({ onUnauthorized } = {}) {
  const [conversations, setConversations] = useState(loadConversations);
  const [streamingId, setStreamingId] = useState(null);
  const abortRef = useRef(null);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
    } catch {
      // storage full or blocked; conversations still work for this session
    }
  }, [conversations]);

  const updateLastMessage = useCallback((convId, patch) => {
    setConversations(prev =>
      prev.map(c => {
        if (c.id !== convId) return c;
        const messages = [...c.messages];
        const last = messages[messages.length - 1];
        messages[messages.length - 1] = { ...last, ...(typeof patch === 'function' ? patch(last) : patch) };
        return { ...c, messages };
      }),
    );
  }, []);

  const sendMessage = useCallback(
    async (id, text, images = [], mentions = []) => {
      const userMsg = { role: 'user', content: text, images, mentions };
      const placeholder = { role: 'assistant', content: '', pending: true, status: '' };

      setConversations(prev => {
        const conv = prev.find(c => c.id === id);
        if (!conv) {
          const label = text || 'Reference images';
          const title = label.length > 48 ? label.slice(0, 48).trimEnd() + '…' : label;
          return [{ id, title, createdAt: Date.now(), messages: [userMsg, placeholder] }, ...prev];
        }
        // Move the active conversation to the top of Recents
        const rest = prev.filter(c => c.id !== id);
        return [{ ...conv, messages: [...conv.messages, userMsg, placeholder] }, ...rest];
      });

      const controller = new AbortController();
      abortRef.current = controller;
      setStreamingId(id);

      try {
        const response = await apiFetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: text, conversation_id: id, images: images.map(i => ({ url: i.url, kind: i.kind || 'reference' })), mentions }),
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(await errorDetail(response));

        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const events = buffer.split('\n\n');
          buffer = events.pop();

          for (const event of events) {
            for (const line of event.split('\n')) {
              if (!line.startsWith('data: ')) continue;
              let data;
              try {
                data = JSON.parse(line.slice(6));
              } catch {
                continue;
              }
              if (data.type === 'status') {
                updateLastMessage(id, { status: data.content });
              } else if (data.type === 'image') {
                const { url, width, height, prompt, label, check, error, index } = data;
                const image = { url, width, height, prompt, label, check, error, index };
                // Batch results arrive in finishing order; keep them in logo order
                updateLastMessage(id, last => ({
                  images: [...(last.images || []), image].sort((a, b) => (a.index ?? 0) - (b.index ?? 0)),
                }));
              } else if (data.type === 'reference') {
                // A design the agent just saved to the reference library
                const { id: refId, url, name, source_url, collection, width, height } = data;
                updateLastMessage(id, last => ({
                  references: [...(last.references || []), { id: refId, url, name, source_url, collection, width, height }],
                }));
              } else if (data.type === 'token') {
                updateLastMessage(id, last => ({ content: last.content + data.content, status: '' }));
              } else if (data.type === 'error') {
                updateLastMessage(id, { error: data.content });
              }
            }
          }
        }
      } catch (err) {
        if (err.name === 'AbortError') {
          updateLastMessage(id, { stopped: true });
        } else if (err instanceof UnauthorizedError) {
          updateLastMessage(id, { error: 'Your session expired. Please sign in again.' });
          onUnauthorized?.();
        } else if (err instanceof TypeError) {
          updateLastMessage(id, { error: 'Could not reach the AI server. Check your connection and try again.' });
        } else {
          updateLastMessage(id, { error: err.message });
        }
      } finally {
        updateLastMessage(id, { pending: false, status: '' });
        setStreamingId(null);
        abortRef.current = null;
      }
    },
    [updateLastMessage, onUnauthorized],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const deleteConversation = useCallback(id => {
    setConversations(prev => prev.filter(c => c.id !== id));
    // Also drop the server-side history and its long-term memories
    apiFetch(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
  }, []);

  return { conversations, streamingId, sendMessage, stop, deleteConversation, newId };
}
