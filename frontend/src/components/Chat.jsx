import React, { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { Link } from 'react-router-dom';
import { ArrowUp, Square, Copy, Check, Sparkles, AlertCircle, ImagePlus, Loader2, X, Download, Images } from 'lucide-react';
import { uploadImage, UnauthorizedError } from '../conversations.js';
import { useMentions, MentionMenu, MentionChips, TextWithMentions } from './Mentions.jsx';

const SUGGESTIONS = [
  'Find 20 unique Dashain poster designs from Pinterest and Behance',
  'Ideas for a minimalist coffee shop logo',
  'What colors work best for a fitness brand?',
  'Describe a festive Instagram post layout',
];

const MAX_IMAGES = 6; // e.g. 5 logos + 1 style reference
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];

// Reference images picked in the composer. Each starts uploading as soon as it is
// added, so it is ready (has a URL) by the time the message is sent.
function useReferenceImages(onUnauthorized) {
  const [items, setItems] = useState([]);
  const itemsRef = useRef(items);
  itemsRef.current = items;

  useEffect(() => () => itemsRef.current.forEach(i => URL.revokeObjectURL(i.preview)), []);

  const patch = (id, change) => setItems(prev => prev.map(i => (i.id === id ? { ...i, ...change } : i)));

  const add = files => {
    const room = MAX_IMAGES - itemsRef.current.length;
    const picked = Array.from(files).filter(f => f.type.startsWith('image/')).slice(0, Math.max(room, 0));
    const added = picked.map(file => {
      const item = { id: crypto.randomUUID(), name: file.name, preview: URL.createObjectURL(file), status: 'uploading' };
      if (!IMAGE_TYPES.includes(file.type)) return { ...item, status: 'error', error: 'Use PNG, JPEG, WebP or GIF' };
      if (file.size > MAX_IMAGE_BYTES) return { ...item, status: 'error', error: 'Larger than 10 MB' };
      uploadImage(file)
        .then(res => patch(item.id, { status: 'done', url: res.url, width: res.width, height: res.height, kind: res.kind || 'reference' }))
        .catch(err => {
          if (err instanceof UnauthorizedError) onUnauthorized?.();
          patch(item.id, { status: 'error', error: err.message || 'Upload failed' });
        });
      return item;
    });
    setItems(prev => [...prev, ...added]);
  };

  const remove = id => {
    setItems(prev => {
      const gone = prev.find(i => i.id === id);
      if (gone) URL.revokeObjectURL(gone.preview);
      return prev.filter(i => i.id !== id);
    });
  };

  const clear = () => {
    itemsRef.current.forEach(i => URL.revokeObjectURL(i.preview));
    setItems([]);
  };

  const toggleKind = id =>
    setItems(prev => prev.map(i => (i.id === id ? { ...i, kind: i.kind === 'logo' ? 'reference' : 'logo' } : i)));

  return { items, add, remove, clear, toggleKind };
}

function ReferenceImages({ items, onRemove, onToggleKind }) {
  const logos = items.filter(i => i.kind === 'logo').length;
  const refs = items.filter(i => i.kind === 'reference').length;
  const summary = [logos && `${logos} logo${logos > 1 ? 's' : ''}`, refs && `${refs} reference${refs > 1 ? 's' : ''}`]
    .filter(Boolean)
    .join(' · ');
  return (
    <div className="ref-section">
      <div className="ref-header">
        <span>
          Images{summary && <span className="ref-summary"> · {summary}</span>}
          {logos >= 2 && <span className="ref-summary"> — one design per logo</span>}
        </span>
        <span className="ref-count">{items.length}/{MAX_IMAGES}</span>
      </div>
      <div className="ref-list">
        {items.map(item => (
          <div key={item.id} className={`ref-thumb ${item.status}`} title={item.error || item.name}>
            <img src={item.preview} alt={item.name} />
            {item.status === 'uploading' && <span className="ref-overlay"><Loader2 size={18} className="spin" /></span>}
            {item.status === 'error' && <span className="ref-overlay"><AlertCircle size={18} /></span>}
            <button type="button" className="ref-remove" onClick={() => onRemove(item.id)} aria-label={`Remove ${item.name}`}>
              <X size={12} strokeWidth={3} />
            </button>
            {item.status === 'done' && (
              <button
                type="button"
                className={`ref-kind ${item.kind}`}
                onClick={() => onToggleKind(item.id)}
                title={item.kind === 'logo' ? 'Placed as a logo. Click to use as a style reference instead.' : 'Used as a style reference. Click to mark as a logo.'}
              >
                {item.kind === 'logo' ? 'Logo' : 'Ref'}
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function Composer({ onSend, onStop, streaming, autoFocus, onUnauthorized }) {
  const [value, setValue] = useState('');
  const [dragging, setDragging] = useState(false);
  const ref = useRef(null);
  const fileRef = useRef(null);
  const images = useReferenceImages(onUnauthorized);
  const mentions = useMentions(value, setValue, ref, onUnauthorized);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }, [value]);

  useEffect(() => {
    if (autoFocus) ref.current?.focus();
  }, [autoFocus]);

  const uploading = images.items.some(i => i.status === 'uploading');
  const ready = images.items.filter(i => i.status === 'done');
  const full = images.items.length >= MAX_IMAGES;
  const canSend = (value.trim() || ready.length > 0) && !uploading && !streaming;

  const submit = () => {
    if (!canSend) return;
    onSend(
      value.trim(),
      ready.map(({ url, width, height, name, kind }) => ({ url, width, height, name, kind })),
      mentions.current.map(({ type, id, query, label }) => ({ type, id, query, label })),
    );
    setValue('');
    images.clear();
    mentions.reset();
  };

  const onPaste = e => {
    const files = Array.from(e.clipboardData?.files || []).filter(f => f.type.startsWith('image/'));
    if (files.length) {
      e.preventDefault();
      images.add(files);
    }
  };

  const onDrop = e => {
    e.preventDefault();
    setDragging(false);
    images.add(e.dataTransfer.files);
  };

  return (
    <div
      className={`composer${dragging ? ' dragging' : ''}`}
      onDragOver={e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true); } }}
      onDragLeave={e => { if (!e.currentTarget.contains(e.relatedTarget)) setDragging(false); }}
      onDrop={onDrop}
    >
      <MentionMenu mentions={mentions} />
      {images.items.length > 0 && <ReferenceImages items={images.items} onRemove={images.remove} onToggleKind={images.toggleKind} />}
      <MentionChips mentions={mentions} />
      <div className="composer-row">
        <input
          ref={fileRef}
          type="file"
          accept={IMAGE_TYPES.join(',')}
          multiple
          hidden
          onChange={e => { images.add(e.target.files); e.target.value = ''; }}
        />
        <button
          type="button"
          className="icon-btn attach-btn"
          onClick={() => fileRef.current?.click()}
          disabled={full}
          aria-label="Add logos or reference images"
          title={full ? `Up to ${MAX_IMAGES} images` : 'Add logos or reference images'}
        >
          <ImagePlus size={19} />
        </button>
        <textarea
          ref={ref}
          rows={1}
          value={value}
          placeholder={images.items.length ? 'Describe what to make with these references…' : 'Message NNT Studio… type @ for a client or references'}
          onChange={e => setValue(e.target.value)}
          onSelect={mentions.sync}
          onBlur={mentions.close}
          onPaste={onPaste}
          aria-autocomplete="list"
          onKeyDown={e => {
            if (mentions.onKeyDown(e)) return;
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-actions">
          {streaming ? (
            <button className="send-btn" onClick={onStop} aria-label="Stop generating" title="Stop">
              <Square size={14} fill="currentColor" />
            </button>
          ) : (
            <button
              className="send-btn"
              onClick={submit}
              disabled={!canSend}
              aria-label="Send message"
              title={uploading ? 'Waiting for images to upload' : 'Send'}
            >
              <ArrowUp size={18} strokeWidth={2.5} />
            </button>
          )}
        </div>
      </div>
      {dragging && <div className="drop-hint">Drop images to use as references</div>}
    </div>
  );
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard blocked; nothing useful to do
    }
  };
  return (
    <button className="icon-btn small" onClick={copy} aria-label="Copy" title={copied ? 'Copied' : 'Copy'}>
      {copied ? <Check size={15} /> : <Copy size={15} />}
    </button>
  );
}

function GeneratedImage({ image }) {
  if (image.error) {
    return (
      <figure className="gen-image failed">
        {image.label && <div className="gen-label">{image.label}</div>}
        <div className="gen-error"><AlertCircle size={16} /> {image.error}</div>
      </figure>
    );
  }
  // Cloudinary's fl_attachment flag makes the link download instead of opening the image
  const downloadUrl = image.url.replace('/image/upload/', '/image/upload/fl_attachment/');
  const flagged = image.check && image.check.passed === false;
  return (
    <figure className="gen-image">
      {image.label && <div className="gen-label">{image.label}</div>}
      <a href={image.url} target="_blank" rel="noreferrer">
        <img
          src={image.url}
          alt={image.prompt?.split('\n')[0] || 'Generated image'}
          width={image.width || undefined}
          height={image.height || undefined}
        />
      </a>
      <figcaption>
        {image.prompt && (
          <details className="gen-prompt">
            <summary>Prompt</summary>
            <pre>{image.prompt}</pre>
          </details>
        )}
        <a className="icon-btn small" href={downloadUrl} aria-label="Download image" title="Download">
          <Download size={15} />
        </a>
      </figcaption>
      {flagged && (
        <div className="gen-warning" role="note">
          <AlertCircle size={14} />
          <span>Check before using: {image.check.issues.join('; ')}</span>
        </div>
      )}
    </figure>
  );
}

// Links inside the app (e.g. "/dashboard/references") navigate in place; others open a new tab
const markdownComponents = {
  a: ({ href = '', children }) => href.startsWith('/')
    ? <Link to={href}>{children}</Link>
    : <a href={href} target="_blank" rel="noreferrer">{children}</a>,
};

const thumb = url => url.replace('/image/upload/', '/image/upload/c_fill,w_240,h_240,f_auto,q_auto/');

function hostLabel(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

// Designs collected from the web into the reference library, shown as they're saved
function CollectedReferences({ refs, pending }) {
  const collection = refs[0]?.collection;
  return (
    <div className="collected">
      <div className="collected-head">
        <span><Images size={15} /> {pending ? 'Saving' : 'Saved'} {refs.length} design{refs.length === 1 ? '' : 's'} to References</span>
        {!pending && (
          <Link to={`/dashboard/references${collection ? `?collection=${encodeURIComponent(collection)}` : ''}`} className="collected-open">
            Open References
          </Link>
        )}
      </div>
      <div className="collected-grid">
        {refs.map(r => (
          <a key={r.id} href={r.source_url || r.url} target="_blank" rel="noreferrer" className="collected-item"
            title={`${r.name}${r.source_url ? `\nSource: ${hostLabel(r.source_url)}` : ''}`}>
            <img src={thumb(r.url)} alt={r.name} loading="lazy" />
          </a>
        ))}
      </div>
    </div>
  );
}

function Message({ msg }) {
  if (msg.role === 'user') {
    return (
      <div className="msg-row user">
        <div className="user-content">
          {msg.images?.length > 0 && (
            <div className="user-images">
              {msg.images.map(img => (
                <a key={img.url} href={img.url} target="_blank" rel="noreferrer" title={img.name || 'Reference image'} className="user-image">
                  <img src={img.url} alt={img.name || 'Reference image'} loading="lazy" />
                  {img.kind === 'logo' && <span className="user-image-kind">Logo</span>}
                </a>
              ))}
            </div>
          )}
          {msg.content && <div className="user-bubble"><TextWithMentions text={msg.content} mentions={msg.mentions} /></div>}
        </div>
      </div>
    );
  }

  const waiting = msg.pending && !msg.content && !msg.images?.length && !msg.references?.length;
  // During a batch, keep showing progress for the logos that are still being made
  const batchStatus = msg.pending && !waiting && msg.status;
  return (
    <div className="msg-row assistant">
      <span className="assistant-mark"><Sparkles size={14} /></span>
      <div className="assistant-body">
        {waiting && (
          <div className="status-line">
            {msg.status ? <span className="shimmer">{msg.status}</span> : <span className="typing"><i /><i /><i /></span>}
          </div>
        )}
        {msg.images?.length > 0 && (
          <div className={msg.images.length > 1 ? 'gen-grid' : undefined}>
            {msg.images.map((img, i) => <GeneratedImage key={img.url || `failed-${i}`} image={img} />)}
          </div>
        )}
        {msg.references?.length > 0 && <CollectedReferences refs={msg.references} pending={msg.pending} />}
        {batchStatus && <div className="status-line"><span className="shimmer">{msg.status}</span></div>}
        {msg.content && (
          <div className="markdown">
            <ReactMarkdown components={markdownComponents}>{msg.content}</ReactMarkdown>
          </div>
        )}
        {msg.error && (
          <div className="error-line"><AlertCircle size={15} /> {msg.error}</div>
        )}
        {msg.stopped && !msg.content && <div className="muted-line">Stopped.</div>}
        {!msg.pending && msg.content && (
          <div className="msg-actions">
            <CopyButton text={msg.content} />
          </div>
        )}
      </div>
    </div>
  );
}

export default function Chat({ conversation, onSend, onStop, streaming, onUnauthorized }) {
  const messages = conversation?.messages || [];
  const endRef = useRef(null);
  const last = messages[messages.length - 1];

  useEffect(() => {
    // Smooth scrolling doesn't run in a background tab, and image generation is slow
    // enough that people switch away, so jump instantly when the tab is hidden.
    endRef.current?.scrollIntoView({ behavior: document.hidden ? 'auto' : 'smooth', block: 'end' });
  }, [messages.length, last?.content, last?.status, last?.images?.length]);

  if (messages.length === 0) {
    return (
      <div className="chat-empty">
        <h1 className="greeting">What can I help you create?</h1>
        <Composer onSend={onSend} onStop={onStop} streaming={streaming} onUnauthorized={onUnauthorized} autoFocus />
        <div className="suggestions">
          {SUGGESTIONS.map(s => (
            <button key={s} className="suggestion" onClick={() => onSend(s)} disabled={streaming}>
              {s}
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="chat">
      <div className="chat-scroll">
        <div className="chat-column">
          {messages.map((m, i) => (
            <Message key={i} msg={m} />
          ))}
          <div ref={endRef} />
        </div>
      </div>
      <div className="chat-bottom">
        <Composer onSend={onSend} onStop={onStop} streaming={streaming} onUnauthorized={onUnauthorized} autoFocus />
        <p className="disclaimer">NNT Studio can make mistakes. Check important info.</p>
      </div>
    </div>
  );
}
