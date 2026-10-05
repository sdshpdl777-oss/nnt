import React, { useEffect, useState } from 'react';
import { Globe, Mail, MapPin, Pencil, Phone, Plus, RefreshCw, Sparkles, Trash2, X } from 'lucide-react';
import { extractBrandKit, saveBrandKit, deleteBrandKit, thumbnailUrl } from '../clients.js';

const ROLES = ['primary', 'secondary', 'accent', 'background', 'text'];
const EMPTY_KIT = {
  brand_name: '', tagline: '', industry: '', primary_logo: null, alt_logo: null, colors: [],
  heading_font: '', body_font: '', visual_style: '', voice: '',
  footer: { phone: '', email: '', website: '', address: '', socials: [], extra: '' },
  dos: [], donts: [],
};

const fileId = handle => Number((handle || '').split(':')[1]);
const dateText = iso => new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });

// The client's brand kit: extracted from its brand files (and website), reviewed and edited by the user.
// The chat agent applies it automatically when designing for this client.
export function BrandKitPanel({ client, call, onChange }) {
  const kit = client.brand_kit;
  const brandFiles = client.files.filter(f => f.category === 'brand');
  const images = brandFiles.filter(f => thumbnailUrl(f) && !/\.pdf$/i.test(f.url));
  const [website, setWebsite] = useState(kit?.website || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState(null);  // 'reextract' | 'remove'

  useEffect(() => { setWebsite(kit?.website || ''); }, [kit?.website]);

  const extract = async () => {
    setConfirm(null);
    setError('');
    setBusy(true);
    const saved = await call(() => extractBrandKit(client.id, website.trim()), setError);
    setBusy(false);
    if (saved) {
      onChange(saved);
      setEditing(false);
    }
  };

  const remove = async () => {
    setConfirm(null);
    const ok = await call(() => deleteBrandKit(client.id), setError);
    if (ok !== undefined) onChange(null);
  };

  const newFiles = kit ? brandFiles.filter(f => !kit.sources.includes(`brand:${f.id}`)).length : 0;
  const canExtract = brandFiles.length > 0 || website.trim();

  if (busy) {
    return (
      <section className="kit-panel kit-busy" aria-live="polite">
        <Sparkles size={20} className="kit-spin" />
        <div>
          <strong>Extracting brand details…</strong>
          <p className="muted small">Reading the logo, colors, fonts and contact details{website.trim() ? ' from the brand files and website' : ''}. This takes about 20–40 seconds.</p>
        </div>
      </section>
    );
  }

  if (!kit) {
    return (
      <section className="kit-panel kit-empty">
        <div className="kit-empty-head">
          <Sparkles size={20} />
          <div>
            <strong>Brand kit</strong>
            <p className="muted small">
              Extract the logo, colors, fonts and footer details (phone, email, website, address, socials) from this client's brand files,
              so the agent applies them automatically when it designs for {client.name}.
            </p>
          </div>
        </div>
        <form className="kit-extract-form" onSubmit={e => { e.preventDefault(); if (canExtract) extract(); }}>
          <label className="kit-site">
            <Globe size={15} />
            <input value={website} onChange={e => setWebsite(e.target.value)} placeholder="Client website (optional) — for contact details" inputMode="url" aria-label="Client website" />
          </label>
          <button type="submit" className="btn btn-primary" disabled={!canExtract}>
            <Sparkles size={15} /> Extract brand details
          </button>
        </form>
        {!brandFiles.length && <p className="muted small">Upload a logo or brand guide below first, or enter the website.</p>}
        {error && <p className="form-error">{error}</p>}
      </section>
    );
  }

  if (editing) {
    return (
      <KitEditor kit={kit} images={images} website={website} call={call} clientId={client.id}
        onCancel={() => setEditing(false)}
        onSaved={saved => { onChange(saved); setEditing(false); }} />
    );
  }

  const d = kit.data;
  const logo = images.find(f => f.id === fileId(d.primary_logo));
  const altLogo = images.find(f => f.id === fileId(d.alt_logo));
  const footer = d.footer || {};
  const hasFooter = footer.phone || footer.email || footer.website || footer.address || footer.socials?.length || footer.extra;

  return (
    <section className="kit-panel">
      <div className="kit-head">
        <div>
          <strong>Brand kit</strong>
          <span className="muted small"> · extracted {dateText(kit.extracted_at)}{kit.edited ? ' · edited' : ''}</span>
        </div>
        {confirm ? (
          <div className="confirm-row">
            <span className="small">{confirm === 'remove' ? 'Remove this brand kit?' : 'Re-extracting replaces your edits.'}</span>
            <button className={`btn btn-xs ${confirm === 'remove' ? 'btn-danger' : 'btn-primary'}`} onClick={confirm === 'remove' ? remove : extract}>
              {confirm === 'remove' ? 'Remove' : 'Re-extract'}
            </button>
            <button className="btn btn-ghost btn-xs" onClick={() => setConfirm(null)}>Cancel</button>
          </div>
        ) : (
          <div className="kit-actions">
            <button className="btn btn-ghost btn-xs" onClick={() => setEditing(true)}><Pencil size={13} /> Edit</button>
            <button className="btn btn-ghost btn-xs" onClick={() => (kit.edited ? setConfirm('reextract') : extract())}><RefreshCw size={13} /> Re-extract</button>
            <button className="icon-btn small" aria-label="Remove brand kit" title="Remove brand kit" onClick={() => setConfirm('remove')}><Trash2 size={14} /></button>
          </div>
        )}
      </div>
      {newFiles > 0 && (
        <p className="kit-note small">{newFiles} brand file{newFiles === 1 ? ' was' : 's were'} added since this kit was extracted. Re-extract to include {newFiles === 1 ? 'it' : 'them'}.</p>
      )}
      {error && <p className="form-error">{error}</p>}

      <div className="kit-grid">
        <div className="kit-block kit-identity">
          <div className="kit-logos">
            {logo ? <img src={thumbnailUrl(logo, 320)} alt={`${d.brand_name || client.name} logo`} /> : <span className="muted small">No logo picked</span>}
            {altLogo && <img src={thumbnailUrl(altLogo, 320)} alt="Logo for dark backgrounds" className="dark" />}
          </div>
          <div className="kit-name">
            <strong>{d.brand_name || client.name}</strong>
            {d.tagline && <span className="muted small">“{d.tagline}”</span>}
            {d.industry && <span className="muted small">{d.industry}</span>}
          </div>
        </div>

        <div className="kit-block">
          <h4>Colors</h4>
          {d.colors.length ? (
            <div className="kit-swatches">
              {d.colors.map((c, i) => (
                <button key={i} className="kit-swatch" title={`Copy ${c.hex}`} onClick={() => navigator.clipboard?.writeText(c.hex)}>
                  <span className="swatch" style={{ background: c.hex }} />
                  <span className="kit-swatch-text"><code>{c.hex}</code><span className="muted">{[c.role, c.name].filter(Boolean).join(' · ')}</span></span>
                </button>
              ))}
            </div>
          ) : <p className="muted small">None found</p>}
        </div>

        <div className="kit-block">
          <h4>Typography</h4>
          <dl className="kit-list">
            <dt>Headings</dt><dd>{d.heading_font || <span className="muted">—</span>}</dd>
            <dt>Body</dt><dd>{d.body_font || <span className="muted">—</span>}</dd>
          </dl>
        </div>

        <div className="kit-block">
          <h4>Footer details</h4>
          {hasFooter ? (
            <ul className="kit-contacts">
              {footer.phone && <li><Phone size={13} /> {footer.phone}</li>}
              {footer.email && <li><Mail size={13} /> {footer.email}</li>}
              {footer.website && <li><Globe size={13} /> {footer.website}</li>}
              {footer.address && <li><MapPin size={13} /> {footer.address}</li>}
              {footer.socials?.map((s, i) => <li key={i}><span className="kit-social">{s.platform}</span> {s.handle}</li>)}
              {footer.extra && <li className="muted">{footer.extra}</li>}
            </ul>
          ) : <p className="muted small">None found. Add them with Edit, or re-extract with the website.</p>}
        </div>

        {(d.visual_style || d.voice) && (
          <div className="kit-block wide">
            <h4>Style</h4>
            {d.visual_style && <p>{d.visual_style}</p>}
            {d.voice && <p className="muted small">Voice: {d.voice}</p>}
          </div>
        )}

        {(d.dos.length > 0 || d.donts.length > 0) && (
          <div className="kit-block wide">
            <h4>Brand rules</h4>
            <ul className="kit-rules">
              {d.dos.map((r, i) => <li key={`do${i}`} className="do">{r}</li>)}
              {d.donts.map((r, i) => <li key={`dont${i}`} className="dont">{r}</li>)}
            </ul>
          </div>
        )}
      </div>
      <p className="muted small kit-foot">Check the footer details before using them: they're printed on designs. The chat uses this kit whenever you design for {client.name}.</p>
    </section>
  );
}

function KitEditor({ kit, images, website: initialWebsite, call, clientId, onCancel, onSaved }) {
  const [d, setD] = useState(() => ({ ...EMPTY_KIT, ...kit.data, footer: { ...EMPTY_KIT.footer, ...kit.data.footer } }));
  const [website, setWebsite] = useState(initialWebsite);
  const [dos, setDos] = useState(kit.data.dos.join('\n'));
  const [donts, setDonts] = useState(kit.data.donts.join('\n'));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const set = (key, value) => setD(prev => ({ ...prev, [key]: value }));
  const setFooter = (key, value) => setD(prev => ({ ...prev, footer: { ...prev.footer, [key]: value } }));
  const setColor = (i, patch) => set('colors', d.colors.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const setSocial = (i, patch) => setFooter('socials', d.footer.socials.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const lines = text => text.split('\n').map(l => l.trim()).filter(Boolean);

  const save = async e => {
    e.preventDefault();
    setSaving(true);
    setError('');
    const data = { ...d, dos: lines(dos), donts: lines(donts) };
    const saved = await call(() => saveBrandKit(clientId, data, website.trim()), setError);
    setSaving(false);
    if (saved) onSaved(saved);
  };

  const text = (label, value, onChange, props = {}) => (
    <label className="kit-field">
      <span>{label}</span>
      <input className="field" value={value || ''} onChange={e => onChange(e.target.value)} {...props} />
    </label>
  );
  const logoSelect = (label, key) => (
    <label className="kit-field">
      <span>{label}</span>
      <select className="field" value={d[key] || ''} onChange={e => set(key, e.target.value || null)}>
        <option value="">None</option>
        {images.map(f => <option key={f.id} value={`brand:${f.id}`}>{f.name}</option>)}
      </select>
    </label>
  );

  return (
    <form className="kit-panel kit-editor" onSubmit={save}>
      <div className="kit-head">
        <strong>Edit brand kit</strong>
        <div className="kit-actions">
          <button type="button" className="btn btn-ghost btn-xs" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-primary btn-xs" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
        </div>
      </div>
      {error && <p className="form-error">{error}</p>}

      <fieldset>
        <legend>Identity</legend>
        <div className="kit-fields">
          {text('Brand name', d.brand_name, v => set('brand_name', v), { maxLength: 120 })}
          {text('Tagline', d.tagline, v => set('tagline', v), { maxLength: 200 })}
          {text('Industry', d.industry, v => set('industry', v), { maxLength: 120 })}
          {logoSelect('Primary logo', 'primary_logo')}
          {logoSelect('Logo for dark backgrounds', 'alt_logo')}
        </div>
      </fieldset>

      <fieldset>
        <legend>Colors</legend>
        {d.colors.map((c, i) => (
          <div key={i} className="kit-color-row">
            <input type="color" value={/^#[0-9a-f]{6}$/i.test(c.hex) ? c.hex : '#000000'} onChange={e => setColor(i, { hex: e.target.value.toUpperCase() })} aria-label="Pick color" />
            <input className="field hex" value={c.hex} onChange={e => setColor(i, { hex: e.target.value })} aria-label="Hex code" maxLength={7} />
            <select className="field" value={c.role || ''} onChange={e => setColor(i, { role: e.target.value || null })} aria-label="Role">
              <option value="">No role</option>
              {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            <input className="field" value={c.name || ''} onChange={e => setColor(i, { name: e.target.value })} placeholder="Name" aria-label="Color name" />
            <button type="button" className="icon-btn small" aria-label="Remove color" onClick={() => set('colors', d.colors.filter((_, j) => j !== i))}><X size={15} /></button>
          </div>
        ))}
        {d.colors.length < 10 && (
          <button type="button" className="btn btn-ghost btn-xs" onClick={() => set('colors', [...d.colors, { hex: '#000000', role: null, name: '' }])}><Plus size={13} /> Add color</button>
        )}
      </fieldset>

      <fieldset>
        <legend>Typography & style</legend>
        <div className="kit-fields">
          {text('Heading font', d.heading_font, v => set('heading_font', v), { placeholder: 'e.g. Poppins Bold, or "bold geometric sans"' })}
          {text('Body font', d.body_font, v => set('body_font', v))}
        </div>
        <label className="kit-field">
          <span>Visual style</span>
          <textarea className="field" rows={2} value={d.visual_style || ''} onChange={e => set('visual_style', e.target.value)} />
        </label>
        <label className="kit-field">
          <span>Voice</span>
          <input className="field" value={d.voice || ''} onChange={e => set('voice', e.target.value)} />
        </label>
      </fieldset>

      <fieldset>
        <legend>Footer details</legend>
        <p className="muted small">Printed exactly as written on designs that include a footer.</p>
        <div className="kit-fields">
          {text('Phone', d.footer.phone, v => setFooter('phone', v), { inputMode: 'tel' })}
          {text('Email', d.footer.email, v => setFooter('email', v), { inputMode: 'email' })}
          {text('Website', d.footer.website, v => setFooter('website', v), { inputMode: 'url' })}
          {text('Address', d.footer.address, v => setFooter('address', v))}
          {text('Other', d.footer.extra, v => setFooter('extra', v), { placeholder: 'e.g. opening hours' })}
        </div>
        {d.footer.socials.map((s, i) => (
          <div key={i} className="kit-social-row">
            <input className="field" value={s.platform} onChange={e => setSocial(i, { platform: e.target.value })} placeholder="Platform" aria-label="Platform" />
            <input className="field" value={s.handle} onChange={e => setSocial(i, { handle: e.target.value })} placeholder="@handle or link" aria-label="Handle" />
            <button type="button" className="icon-btn small" aria-label="Remove social" onClick={() => setFooter('socials', d.footer.socials.filter((_, j) => j !== i))}><X size={15} /></button>
          </div>
        ))}
        <button type="button" className="btn btn-ghost btn-xs" onClick={() => setFooter('socials', [...d.footer.socials, { platform: '', handle: '' }])}><Plus size={13} /> Add social</button>
      </fieldset>

      <fieldset>
        <legend>Brand rules</legend>
        <div className="kit-fields">
          <label className="kit-field">
            <span>Do (one per line)</span>
            <textarea className="field" rows={3} value={dos} onChange={e => setDos(e.target.value)} />
          </label>
          <label className="kit-field">
            <span>Don't (one per line)</span>
            <textarea className="field" rows={3} value={donts} onChange={e => setDonts(e.target.value)} />
          </label>
        </div>
        {text('Website (used when re-extracting)', website, setWebsite, { inputMode: 'url' })}
      </fieldset>
    </form>
  );
}
