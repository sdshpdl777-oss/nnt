import { API_URL, UnauthorizedError, authHeaders, errorDetail } from './conversations.js';

export const CATEGORIES = [
  { key: 'master', label: 'Master files', hint: 'Source files — PSD, AI, PDF, ZIP, fonts, final exports.', accept: undefined },
  { key: 'brand', label: 'Brands', hint: 'Logos, brand kits and brand-guideline PDFs.', accept: 'image/*,application/pdf' },
];

export const REFERENCE_ACCEPT = 'image/png,image/jpeg,image/webp,image/gif';

async function request(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: { ...(options.json ? { 'Content-Type': 'application/json' } : {}), ...authHeaders(), ...options.headers },
    body: options.json ? JSON.stringify(options.json) : options.body,
  });
  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok) throw new Error(await errorDetail(response));
  return response.status === 204 ? null : response.json();
}

export const listClients = () => request('/api/clients');
export const getClient = id => request(`/api/clients/${id}`);
export const createClient = name => request('/api/clients', { method: 'POST', json: { name } });
export const renameClient = (id, name) => request(`/api/clients/${id}`, { method: 'PATCH', json: { name } });
export const deleteClient = id => request(`/api/clients/${id}`, { method: 'DELETE' });
export const setFileKind = (clientId, fileId, kind) => request(`/api/clients/${clientId}/files/${fileId}`, { method: 'PATCH', json: { kind } });
export const deleteClientFile = (clientId, fileId) => request(`/api/clients/${clientId}/files/${fileId}`, { method: 'DELETE' });
export const listReferences = () => request('/api/references');
export const addReferenceFromUrl = url => request('/api/references/from-url', { method: 'POST', json: { url } });
export const deleteReference = id => request(`/api/references/${id}`, { method: 'DELETE' });

export function uploadReference(file) {
  const form = new FormData();
  form.append('file', file);
  return request('/api/references', { method: 'POST', body: form });
}

export function uploadClientFile(clientId, category, file) {
  const form = new FormData();
  form.append('file', file);
  return request(`/api/clients/${clientId}/files?category=${category}`, { method: 'POST', body: form });
}

const PREVIEWABLE = /\.(png|jpe?g|webp|gif|svg|avif|bmp)$/i;

// A small Cloudinary-resized thumbnail for image files; null for anything else (PSD, PDF, ZIP…)
export function thumbnailUrl(file, width = 480) {
  if (file.resource_type !== 'image' || !PREVIEWABLE.test(file.url)) return null;
  return file.url.replace('/image/upload/', `/image/upload/c_limit,w_${width},f_auto,q_auto/`);
}

export function formatBytes(n) {
  if (!n && n !== 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function extension(name) {
  const m = /\.([a-z0-9]{1,5})$/i.exec(name || '');
  return m ? m[1].toUpperCase() : 'FILE';
}

export const extractBrandKit = (clientId, website) => request(`/api/clients/${clientId}/brand-kit/extract`, { method: 'POST', json: { website: website || null } });
export const saveBrandKit = (clientId, data, website) => request(`/api/clients/${clientId}/brand-kit`, { method: 'PUT', json: { data, website: website || null } });
export const deleteBrandKit = clientId => request(`/api/clients/${clientId}/brand-kit`, { method: 'DELETE' });
