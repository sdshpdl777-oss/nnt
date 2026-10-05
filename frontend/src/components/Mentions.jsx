import React, { useEffect, useRef, useState } from 'react';
import { Building2, Images, Loader2, X } from 'lucide-react';
import { fetchMentions, UnauthorizedError } from '../conversations.js';

// "@" at the start or after a space, up to the caret: "make a post for @Himali Br|"
const TRIGGER = /(^|\s)@([^@\n]{0,40})$/;

const keyOf = m => `${m.type}:${m.type === 'client' ? m.id : m.query}`;

// @-mentions in the composer: a suggestion menu while typing "@…", and the clients /
// reference topics picked so far. Picked mentions are sent only if their "@label" is still in the text.
export function useMentions(value, setValue, textareaRef, onUnauthorized) {
  const [picked, setPicked] = useState([]);
  const [menu, setMenu] = useState(null);  // { start, query }
  const [results, setResults] = useState({ query: null, items: [] });
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(0);
  const dismissedAt = useRef(null);  // menu closed with Escape at this "@" position
  const pickedRef = useRef(picked);
  pickedRef.current = picked;

  // Re-check the trigger whenever the text or caret moves
  const sync = () => {
    const el = textareaRef.current;
    if (!el) return;
    const before = el.value.slice(0, el.selectionStart);
    const m = TRIGGER.exec(before);
    if (!m) {
      dismissedAt.current = null;
      setMenu(null);
      return;
    }
    const start = before.length - m[2].length - 1;
    if (dismissedAt.current === start) return;
    // Typing on after a picked mention ("@Himali Brew make it…") isn't a new mention
    if (pickedRef.current.some(p => m[2].toLowerCase().startsWith(`${p.label.toLowerCase()} `))) {
      setMenu(null);
      return;
    }
    setMenu(prev => (prev && prev.start === start && prev.query === m[2] ? prev : { start, query: m[2] }));
  };

  useEffect(sync, [value]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch suggestions, debounced; reference topics need a short lookup on the server
  useEffect(() => {
    if (!menu) return undefined;
    const controller = new AbortController();
    setLoading(true);
    const timer = setTimeout(async () => {
      const q = menu.query.trim();
      const load = kind => fetchMentions(q, kind, controller.signal).catch(err => {
        if (err instanceof UnauthorizedError) onUnauthorized?.();
        return null;
      });
      // Clients come back at once; reference topics need a short lookup, so they're added when ready
      let clients = [];
      const fast = load('clients').then(data => {
        if (controller.signal.aborted) return;
        clients = data?.clients || [];
        setResults({ query: menu.query, items: clients });
        setActive(0);
      });
      const slow = load('references');
      const [, data] = await Promise.all([fast, slow]);
      if (controller.signal.aborted) return;
      const items = [...clients, ...(data?.references || [])];
      setResults({ query: menu.query, items });
      setLoading(false);
      // Spaces are allowed for names like "Himali Brew", but a space with no matches ends the mention
      if (!items.length && /\s/.test(menu.query)) setMenu(null);
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [menu?.start, menu?.query]);  // eslint-disable-line react-hooks/exhaustive-deps

  const pick = item => {
    if (!menu) return;
    const el = textareaRef.current;
    const caret = el ? el.selectionStart : value.length;
    const insert = `@${item.label} `;
    const next = value.slice(0, menu.start) + insert + value.slice(caret);
    setValue(next);
    setPicked(prev => [...prev.filter(m => keyOf(m) !== keyOf(item)), item]);
    setMenu(null);
    requestAnimationFrame(() => {
      el?.focus();
      const pos = menu.start + insert.length;
      el?.setSelectionRange(pos, pos);
    });
  };

  const items = results.items;
  const open = Boolean(menu) && (items.length > 0 || loading);

  // Returns true when the key was used by the menu
  const onKeyDown = e => {
    if (!open || !items.length) {
      if (open && e.key === 'Escape') { dismissedAt.current = menu.start; setMenu(null); return true; }
      return false;
    }
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(i => (i + 1) % items.length); return true; }
    if (e.key === 'ArrowUp') { e.preventDefault(); setActive(i => (i - 1 + items.length) % items.length); return true; }
    if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') { e.preventDefault(); pick(items[active]); return true; }
    if (e.key === 'Escape') { e.preventDefault(); dismissedAt.current = menu.start; setMenu(null); return true; }
    return false;
  };

  const current = picked.filter(m => value.includes(`@${m.label}`));
  const remove = m => {
    setPicked(prev => prev.filter(x => keyOf(x) !== keyOf(m)));
    setValue(v => v.replace(`@${m.label} `, '').replace(`@${m.label}`, ''));
  };
  const reset = () => { setPicked([]); setMenu(null); dismissedAt.current = null; };

  const close = () => setMenu(null);

  return { open, items, loading, active, setActive, pick, onKeyDown, sync, close, current, remove, reset };
}

function MentionItem({ item, active, onPick, onHover }) {
  return (
    <li role="option" aria-selected={active} className={`mention-item${active ? ' active' : ''}`}
      onMouseDown={e => { e.preventDefault(); onPick(item); }} onMouseEnter={onHover}>
      {item.type === 'client' ? (
        <span className="mention-icon">{item.logo ? <img src={item.logo} alt="" /> : <Building2 size={15} />}</span>
      ) : (
        <span className="mention-thumbs">
          {item.thumbs.slice(0, 3).map(t => <img key={t} src={t} alt="" />)}
        </span>
      )}
      <span className="mention-text">
        <span className="mention-label">{item.label}</span>
        <span className="mention-meta">
          {item.type === 'client'
            ? `Client${item.has_kit ? ' · brand kit' : ''}`
            : `${item.count} reference${item.count === 1 ? '' : 's'}`}
        </span>
      </span>
    </li>
  );
}

export function MentionMenu({ mentions }) {
  const { open, items, loading, active, setActive, pick } = mentions;
  if (!open) return null;
  const clients = items.filter(i => i.type === 'client');
  const refs = items.filter(i => i.type === 'references');
  const row = item => {
    const index = items.indexOf(item);
    return <MentionItem key={`${item.type}:${item.id ?? item.query}`} item={item} active={index === active} onPick={pick} onHover={() => setActive(index)} />;
  };
  return (
    <div className="mention-menu" role="listbox" aria-label="Mention a client or references">
      {clients.length > 0 && <div className="mention-group">Clients</div>}
      {clients.map(row)}
      {refs.length > 0 && <div className="mention-group">References</div>}
      {refs.map(row)}
      {loading && !items.length && <div className="mention-loading"><Loader2 size={14} className="spin" /> Searching…</div>}
      {loading && items.length > 0 && <div className="mention-loading small"><Loader2 size={12} className="spin" /> Looking for matching references…</div>}
    </div>
  );
}

export function MentionChips({ mentions }) {
  if (!mentions.current.length) return null;
  return (
    <div className="mention-chips">
      {mentions.current.map(m => (
        <span key={keyOf(m)} className={`mention-chip ${m.type}`}>
          {m.type === 'client' ? <Building2 size={13} /> : <Images size={13} />}
          <span>{m.label}</span>
          <span className="mention-chip-meta">
            {m.type === 'client' ? (m.has_kit ? 'brand kit' : 'client') : `${m.count} ref${m.count === 1 ? '' : 's'}`}
          </span>
          <button type="button" aria-label={`Remove ${m.label}`} onClick={() => mentions.remove(m)}><X size={12} strokeWidth={3} /></button>
        </span>
      ))}
    </div>
  );
}

// A sent message's text with its @mentions highlighted
export function TextWithMentions({ text, mentions }) {
  const labels = (mentions || []).map(m => `@${m.label}`).sort((a, b) => b.length - a.length);
  if (!labels.length) return text;
  const pattern = new RegExp(`(${labels.map(l => l.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'g');
  return text.split(pattern).map((part, i) => (labels.includes(part) ? <span key={i} className="mention-tag">{part}</span> : part));
}
