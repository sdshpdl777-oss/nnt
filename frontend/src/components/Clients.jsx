import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ArrowLeft, ExternalLink, FileText, FolderOpen, Link2, Pencil, Plus, Search, Trash2, Upload, X, Check } from 'lucide-react';
import { UnauthorizedError } from '../conversations.js';
import {
  CATEGORIES, listClients, getClient, createClient, renameClient, deleteClient,
  uploadClientFile, deleteClientFile, setFileKind, listReferences, uploadReference, addReferenceFromUrl, deleteReference, REFERENCE_ACCEPT,
  thumbnailUrl, formatBytes, extension,
} from '../clients.js';
import { BrandKitPanel } from './BrandKit.jsx';

// Runs an API call, sending the user to sign-in on 401 and returning other errors as text
function useApi(onUnauthorized) {
  return useCallback(async (fn, onError) => {
    try {
      return await fn();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized?.();
      else onError?.(err instanceof TypeError ? 'Could not reach the server. Check that the backend is running.' : err.message);
      return undefined;
    }
  }, [onUnauthorized]);
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// ---------- All clients ----------

export function ClientsPage({ onUnauthorized }) {
  const call = useApi(onUnauthorized);
  const navigate = useNavigate();
  const [clients, setClients] = useState(null);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    call(listClients, setError).then(data => data && setClients(data));
  }, [call]);

  const handleCreate = async e => {
    e.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    setError('');
    const client = await call(() => createClient(name), setError);
    setSaving(false);
    if (client) navigate(`/dashboard/clients/${client.id}`);
  };

  const shown = (clients || []).filter(c => c.name.toLowerCase().includes(query.trim().toLowerCase()));

  return (
    <div className="page">
      <div className="page-head">
        <h2>Clients</h2>
        {!creating && (
          <button className="btn btn-primary" onClick={() => { setCreating(true); setError(''); }}>
            <Plus size={16} /> Create client
          </button>
        )}
      </div>

      {creating && (
        <form className="inline-form" onSubmit={handleCreate}>
          <input className="field" autoFocus placeholder="Client name, e.g. Himalayan Coffee" value={name} maxLength={120}
            onChange={e => setName(e.target.value)} onKeyDown={e => e.key === 'Escape' && setCreating(false)} />
          <button type="submit" className="btn btn-primary" disabled={saving || !name.trim()}>{saving ? 'Creating…' : 'Create'}</button>
          <button type="button" className="btn btn-ghost" onClick={() => { setCreating(false); setName(''); }}>Cancel</button>
        </form>
      )}
      {error && <p className="form-error page-error">{error}</p>}

      {clients === null ? (
        !error && <p className="muted">Loading clients…</p>
      ) : clients.length === 0 ? (
        <div className="empty-card">
          <FolderOpen size={28} />
          <p>No clients yet. Create one to keep its master files and brand assets together.</p>
        </div>
      ) : (
        <>
          {clients.length > 6 && (
            <label className="search-field">
              <Search size={16} />
              <input placeholder="Search clients" value={query} onChange={e => setQuery(e.target.value)} />
            </label>
          )}
          <div className="client-grid">
            {shown.map(c => (
              <Link key={c.id} to={`/dashboard/clients/${c.id}`} className="client-card">
                <div className="client-cover">
                  {c.cover_url
                    ? <img src={c.cover_url.replace('/image/upload/', '/image/upload/c_limit,w_320,f_auto,q_auto/')} alt="" />
                    : <span className="client-initial">{c.name.slice(0, 1).toUpperCase()}</span>}
                </div>
                <div className="client-card-body">
                  <h3>{c.name}</h3>
                  <p className="muted small">
                    {plural(c.counts.master, 'master file')} · {plural(c.counts.brand, 'brand asset')}
                  </p>
                </div>
              </Link>
            ))}
            {shown.length === 0 && <p className="muted">No clients match “{query}”.</p>}
          </div>
        </>
      )}
    </div>
  );
}

// ---------- One client ----------

export function ClientPage({ onUnauthorized }) {
  const { clientId } = useParams();
  const call = useApi(onUnauthorized);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = CATEGORIES.some(c => c.key === params.get('tab')) ? params.get('tab') : 'master';
  const [client, setClient] = useState(null);
  const [error, setError] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setClient(null);
    call(() => getClient(clientId), setError).then(data => data && setClient(data));
  }, [call, clientId]);

  if (!client) {
    return (
      <div className="page">
        <BackLink />
        {error ? <p className="form-error">{error}</p> : <p className="muted">Loading…</p>}
      </div>
    );
  }

  const handleRename = async e => {
    e.preventDefault();
    const updated = await call(() => renameClient(client.id, newName), setError);
    if (updated) {
      setClient(c => ({ ...c, name: updated.name }));
      setRenaming(false);
      setError('');
    }
  };

  const handleDelete = async () => {
    const ok = await call(() => deleteClient(client.id), setError);
    if (ok !== undefined) navigate('/dashboard/clients');
  };

  const addFile = file => setClient(c => ({
    ...c,
    files: [file, ...c.files],
    counts: { ...c.counts, [file.category]: c.counts[file.category] + 1 },
  }));
  const removeFile = file => setClient(c => ({
    ...c,
    files: c.files.filter(f => f.id !== file.id),
    counts: { ...c.counts, [file.category]: c.counts[file.category] - 1 },
  }));

  const total = client.files.length;

  return (
    <div className="page">
      <BackLink />
      <div className="page-head">
        {renaming ? (
          <form className="inline-form grow" onSubmit={handleRename}>
            <input className="field" autoFocus value={newName} maxLength={120} onChange={e => setNewName(e.target.value)}
              onKeyDown={e => e.key === 'Escape' && setRenaming(false)} />
            <button type="submit" className="icon-btn" aria-label="Save name" title="Save"><Check size={18} /></button>
            <button type="button" className="icon-btn" aria-label="Cancel rename" title="Cancel" onClick={() => setRenaming(false)}><X size={18} /></button>
          </form>
        ) : (
          <div className="title-row">
            <h2>{client.name}</h2>
            <button className="icon-btn small" aria-label="Rename client" title="Rename"
              onClick={() => { setNewName(client.name); setRenaming(true); }}>
              <Pencil size={15} />
            </button>
          </div>
        )}
        {confirmDelete ? (
          <div className="confirm-row">
            <span className="small">Delete {client.name}{total ? ` and its ${plural(total, 'file')}` : ''}?</span>
            <button className="btn btn-danger" onClick={handleDelete}>Delete</button>
            <button className="btn btn-ghost" onClick={() => setConfirmDelete(false)}>Cancel</button>
          </div>
        ) : (
          <button className="btn btn-ghost" onClick={() => setConfirmDelete(true)}><Trash2 size={15} /> Delete client</button>
        )}
      </div>
      {error && <p className="form-error page-error">{error}</p>}

      <div className="tabs" role="tablist">
        {CATEGORIES.map(c => (
          <button key={c.key} role="tab" aria-selected={tab === c.key} className={`tab ${tab === c.key ? 'active' : ''}`}
            onClick={() => setParams({ tab: c.key }, { replace: true })}>
            {c.label} <span className="tab-count">{client.counts[c.key]}</span>
          </button>
        ))}
      </div>

      {tab === 'brand' && (
        <BrandKitPanel client={client} call={call} onChange={kit => setClient(c => ({ ...c, brand_kit: kit }))} />
      )}

      <FileSection
        key={tab}
        {...CATEGORIES.find(c => c.key === tab)}
        files={client.files.filter(f => f.category === tab)}
        call={call}
        upload={f => uploadClientFile(client.id, tab, f)}
        remove={f => deleteClientFile(client.id, f.id)}
        toggleKind={tab === 'brand' ? f => setFileKind(client.id, f.id, f.kind === 'logo' ? 'asset' : 'logo') : undefined}
        onChanged={changed => setClient(c => ({ ...c, files: c.files.map(f => (f.id === changed.id ? changed : f)) }))}
        onAdded={addFile}
        onRemoved={removeFile}
      />
    </div>
  );
}

const BackLink = () => (
  <Link to="/dashboard/clients" className="back-link"><ArrowLeft size={15} /> All clients</Link>
);

// A link dropped from another browser tab, if the drop carried one
function droppedUrl(dataTransfer) {
  const raw = dataTransfer.getData('text/uri-list') || dataTransfer.getData('text/plain') || '';
  const url = raw.split('\n').map(l => l.trim()).find(l => l && !l.startsWith('#'));
  return url && /^https?:\/\//i.test(url) ? url : null;
}

// Upload dropzone plus a grid of files; `upload`/`remove` do the actual API calls.
// With `addUrl`, files can also be added from a link (pasted, or dragged in from another tab).
function FileSection({ label, hint, accept, files, call, upload: uploadOne, addUrl, remove, toggleKind, onAdded, onRemoved, onChanged }) {
  const inputRef = useRef(null);
  const [uploading, setUploading] = useState([]);  // names of files in flight
  const [errors, setErrors] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [link, setLink] = useState('');

  const importUrl = async url => {
    setErrors([]);
    setUploading(u => [...u, url]);
    const saved = await call(() => addUrl(url), msg => setErrors(e => [...e, msg]));
    if (saved) onAdded(saved);
    setUploading(u => u.filter(x => x !== url));
    return saved;
  };

  const handleLink = async e => {
    e.preventDefault();
    let url = link.trim();
    if (!url) return;
    if (!/^https?:\/\//i.test(url)) url = `https://${url}`;
    if (/\s/.test(url) || !url.slice(8).includes('.')) {
      setErrors(['Enter a web address, like https://example.com/image.jpg']);
      return;
    }
    if (await importUrl(url)) setLink('');
  };

  const upload = async fileList => {
    const picked = Array.from(fileList || []);
    if (!picked.length) return;
    setErrors([]);
    setUploading(u => [...u, ...picked.map(f => f.name)]);
    await Promise.all(picked.map(async f => {
      const saved = await call(() => uploadOne(f), msg => setErrors(e => [...e, `${f.name}: ${msg}`]));
      if (saved) onAdded(saved);
      setUploading(u => { const i = u.indexOf(f.name); return i < 0 ? u : [...u.slice(0, i), ...u.slice(i + 1)]; });
    }));
  };

  const handleDrop = e => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) {
      upload(e.dataTransfer.files);
      return;
    }
    const url = addUrl && droppedUrl(e.dataTransfer);
    if (url) importUrl(url);
  };

  return (
    <section>
      <div
        className={`dropzone ${dragging ? 'dragging' : ''}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        role="button"
        tabIndex={0}
        onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && inputRef.current?.click()}
      >
        <Upload size={20} />
        <div>
          <strong>Upload {label.toLowerCase()}</strong>
          <p className="muted small">
            {hint} Drop files{addUrl ? ' or images from another tab' : ''} here, or click to browse.
          </p>
        </div>
        <input ref={inputRef} type="file" multiple hidden accept={accept}
          onChange={e => { upload(e.target.files); e.target.value = ''; }} />
      </div>

      {addUrl && (
        <form className="link-form" onSubmit={handleLink}>
          <Link2 size={16} />
          <input value={link} onChange={e => setLink(e.target.value)} placeholder="Or paste an image or web page link"
            aria-label="Image or web page link" inputMode="url" />
          <button type="submit" className="btn btn-primary btn-xs" disabled={!link.trim()}>Add</button>
        </form>
      )}

      {uploading.length > 0 && <p className="muted small">Uploading {plural(uploading.length, 'file')}…</p>}
      {errors.map(msg => <p key={msg} className="form-error">{msg}</p>)}

      {files.length === 0 && uploading.length === 0 ? (
        <p className="muted section-empty-text">No {label.toLowerCase()} yet.</p>
      ) : (
        <div className="file-grid">
          {files.map(f => (
            <FileCard key={f.id} file={f}
              onToggleKind={toggleKind && (f.kind === 'logo' || f.kind === 'asset') ? async () => {
                const updated = await call(() => toggleKind(f), msg => setErrors([msg]));
                if (updated) onChanged(updated);
              } : undefined}
              onDelete={async () => {
              const ok = await call(() => remove(f), msg => setErrors([msg]));
              if (ok !== undefined) onRemoved(f);
            }} />
          ))}
        </div>
      )}
    </section>
  );
}

function sourceLabel(url) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, '');
    if (host.endsWith('pinimg.com') || host.includes('pinterest.')) return 'Pinterest';
    if (host.endsWith('behance.net')) return 'Behance';
    if (host.endsWith('dribbble.com')) return 'Dribbble';
    return host;
  } catch {
    return 'Source';
  }
}

function FileCard({ file, onDelete, onToggleKind }) {
  const [confirming, setConfirming] = useState(false);
  const thumb = thumbnailUrl(file);
  return (
    <div className={`file-card${file.category === 'brand' ? ' brand' : ''}`}>
      <a href={file.url} target="_blank" rel="noreferrer" className="file-preview" title={`Open ${file.name}`}>
        {thumb
          ? <img src={thumb} alt={file.name} loading="lazy" />
          : <span className="file-icon"><FileText size={26} /><span>{extension(file.name)}</span></span>}
      </a>
      {onToggleKind && (
        <button className={`kind-badge ${file.kind}`} onClick={onToggleKind}
          title={file.kind === 'logo' ? 'The agent uses this as the client\'s logo. Click to mark as a brand asset.' : 'Click to mark this as the client\'s logo'}>
          {file.kind === 'logo' ? 'Logo' : 'Asset'}
        </button>
      )}
      <div className="file-meta">
        <span className="file-name" title={file.name}>{file.name}</span>
        <span className="muted small">
          {formatBytes(file.bytes)}{file.width && file.height ? ` · ${file.width}×${file.height}` : ''}
        </span>
        {file.source_url && (
          <a href={file.source_url} target="_blank" rel="noreferrer" className="file-source" title={file.source_url}>
            <ExternalLink size={11} /> {sourceLabel(file.source_url)}
          </a>
        )}
      </div>
      {onDelete && (
        confirming ? (
          <div className="file-confirm">
            <button className="btn btn-danger btn-xs" onClick={onDelete}>Delete</button>
            <button className="btn btn-ghost btn-xs" onClick={() => setConfirming(false)}>Keep</button>
          </div>
        ) : (
          <button className="file-delete icon-btn small" aria-label={`Delete ${file.name}`} title="Delete" onClick={() => setConfirming(true)}>
            <Trash2 size={14} />
          </button>
        )
      )}
    </div>
  );
}

// ---------- Shared reference library ----------

const UPLOADS = '__uploads';

export function ReferencesPage({ onUnauthorized }) {
  const call = useApi(onUnauthorized);
  const [refs, setRefs] = useState(null);
  const [error, setError] = useState('');

  const [params, setParams] = useSearchParams();
  const filter = params.get('collection');  // a collection name, UPLOADS, or null for all

  useEffect(() => {
    call(listReferences, setError).then(data => data && setRefs(data));
  }, [call]);

  // Collections the agent gathered from the web, newest first, plus the user's own uploads
  const groups = [];
  for (const r of refs || []) {
    const key = r.collection || UPLOADS;
    const g = groups.find(x => x.key === key);
    if (g) g.count += 1;
    else groups.push({ key, label: r.collection || 'Uploaded', count: 1 });
  }
  const shown = (refs || []).filter(r => !filter || (r.collection || UPLOADS) === filter);
  const pick = key => setParams(key ? { collection: key } : {}, { replace: true });

  return (
    <div className="page">
      <div className="page-head">
        <h2>References</h2>
      </div>
      <p className="muted page-lead">
        One shared library of reference designs, available for every client. Ask the chat to “find 20 Dashain poster designs” to fill it from Pinterest, Behance and Dribbble.
      </p>
      {error && <p className="form-error page-error">{error}</p>}

      {refs === null ? (
        !error && <p className="muted">Loading references…</p>
      ) : (
        <>
        {groups.length > 1 || (groups.length === 1 && groups[0].key !== UPLOADS) ? (
          <div className="chips" role="group" aria-label="Filter references">
            <button className={`chip ${!filter ? 'active' : ''}`} onClick={() => pick(null)}>
              All <span className="tab-count">{refs.length}</span>
            </button>
            {groups.map(g => (
              <button key={g.key} className={`chip ${filter === g.key ? 'active' : ''}`} onClick={() => pick(g.key)} title={g.label}>
                {g.label} <span className="tab-count">{g.count}</span>
              </button>
            ))}
          </div>
        ) : null}
        <FileSection
          label="References"
          hint="Designs to use as style references for any client."
          accept={REFERENCE_ACCEPT}
          files={shown}
          call={call}
          upload={uploadReference}
          addUrl={addReferenceFromUrl}
          remove={r => deleteReference(r.id)}
          onAdded={r => setRefs(prev => [r, ...prev])}
          onRemoved={r => setRefs(prev => prev.filter(x => x.id !== r.id))}
        />
        </>
      )}
    </div>
  );
}
