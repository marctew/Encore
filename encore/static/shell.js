// Encore: what every page shares. The API helper, DOM builders, the shared glyphs, toasts, and the
// sidebar's service status, with a banner for each problem (Design Uniformity 7.10).
import { toast } from './design/toast.js';

export { toast };
export const $ = id => document.getElementById(id);
export const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node[key] = value;
    else if (value !== undefined && value !== false && value !== null) node.setAttribute(key, value);
  }
  node.append(...children.filter(c => c !== null && c !== undefined && c !== false));
  return node;
}

const SVG = 'http://www.w3.org/2000/svg';
export function glyph(name, cls = 'icon-sm') {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('class', cls);
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG, 'use');
  use.setAttribute('href', `/static/design/icons.svg#${name}`);
  svg.append(use);
  return svg;
}

// A failure between the browser and Encore (the tunnel dropping, Encore restarting) is marked
// `unreachable`, so a caller working through a list can wait and try again.
const unreachable = () => Object.assign(new Error("Couldn't reach Encore. Try again in a moment"), { unreachable: true });

export async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, { ...options, headers: { 'X-Encore': '1', 'Content-Type': 'application/json' } });
  } catch {
    throw unreachable();
  }
  if (res.status === 401 && path !== '/api/login') {
    location.href = '/login';
    throw new Error('Sign in first');
  }
  if (!res.ok) {
    let text = await res.text();
    try { text = JSON.parse(text).error || text; } catch {}
    // An HTML page here is Cloudflare's (or a proxy's), not Encore's: Encore only answers in JSON or plain text.
    if ([502, 504, 520, 521, 522, 523, 524, 530].includes(res.status) || /^\s*</.test(text)) throw unreachable();
    throw new Error(text || 'Request failed');
  }
  return res.json();
}

// Runs a button's request with the shared busy state: disabled while it runs, a toast if it fails.
export async function busy(button, work) {
  if (button) button.disabled = true;
  try { return await work(); } catch (error) { toast(error.message, 'error'); return undefined; } finally { if (button && button.isConnected) button.disabled = false; }
}

export function setCount(key, n) {
  document.querySelectorAll(`[data-count="${key}"]`).forEach(badge => { badge.hidden = !n; badge.textContent = n || ''; });
}

function banner(tone, title, body, action) {
  return el('div', { class: `banner banner-${tone}`, role: 'status' }, glyph('alert', 'icon'),
    el('div', { class: 'banner-text' }, el('strong', {}, title), el('div', { class: 'muted' }, body)), action);
}

const settingsLink = () => el('a', { class: 'btn btn-ghost btn-sm', href: '/settings' }, 'Open Settings');

export async function connectPlex(button) {
  const tab = window.open('', '_blank');
  await busy(button, async () => {
    try {
      const pin = await api('/api/plex/link', { method: 'POST' });
      tab.location = pin.url;
      for (let i = 0; i < 150; i++) {
        await new Promise(done => setTimeout(done, 2000));
        if ((await api(`/api/plex/link/${pin.id}`)).linked) { toast('Plex is connected.'); break; }
      }
    } catch (error) { tab.close(); throw error; }
  });
  refreshHealth();
}

function statusRow(tone, text, extra) {
  return el('div', { class: `side-status-row ${tone}` }, el('i', { class: 'dot' }), el('span', {}, text), extra);
}

export async function refreshHealth() {
  const side = $('sideStatus'), banners = $('banners');
  const onSettings = location.pathname === '/settings';
  const settings = onSettings ? null : settingsLink();
  try {
    const s = await api('/api/status');
    setCount('wanted', s.wanted);
    const rows = [], problems = [];
    if (!s.folders) problems.push(banner('warning', "Folders aren't set", 'Set the music library, import and downloads folders, or Encore has nowhere to file albums.', settingsLink()));
    rows.push(s.indexers ? statusRow('ok', plural(s.indexers, 'indexer')) : statusRow('bad', 'No indexers'));
    if (!s.indexers) problems.push(banner('warning', 'No indexers set up', 'Add a Newznab indexer so Encore can find downloads.', settingsLink()));
    if (s.sab === 'ok') rows.push(statusRow('ok', 'SABnzbd connected'));
    else if (s.sab === 'unset') {
      rows.push(statusRow('bad', 'SABnzbd not set up'));
      problems.push(banner('warning', "SABnzbd isn't set up", 'Add its address and API key so Encore can download.', settingsLink()));
    } else {
      rows.push(statusRow('bad', "Can't reach SABnzbd"));
      problems.push(banner('danger', "Can't reach SABnzbd", 'Check that SABnzbd is running, and its address and API key in Settings.', settingsLink()));
    }
    if (s.plex === 'ok') rows.push(statusRow('ok', 'Plex connected'));
    else if (s.plex === 'error') {
      rows.push(statusRow('bad', "Can't reach Plex"));
      problems.push(banner('danger', "Can't reach Plex", "Downloads still work, but Encore can't see what you own or confirm an album reached Plex.", settingsLink()));
    } else if (s.plex === 'unset') {
      rows.push(statusRow('idle', 'Plex not set up'));
      problems.push(banner('warning', "Plex isn't set up", 'Add its address to see which albums you already own.', settingsLink()));
    } else {
      const link = el('button', { type: 'button', class: 'btn btn-primary btn-sm', onclick: event => connectPlex(event.currentTarget) }, 'Connect Plex');
      rows.push(statusRow('idle', 'Plex not connected'));
      problems.push(banner('warning', "Plex isn't connected", 'Sign in to Plex once so Encore can read your library and confirm filing.', link));
    }
    rows.push(el('div', { class: 'side-status-note tabular' }, `${s.albums.toLocaleString('en-GB')} album folders`),
      el('div', { class: 'side-status-note tabular' }, `${(s.free / 1024 ** 4).toFixed(1)} TB free`));
    if (side) side.replaceChildren(el('div', { class: 'eyebrow side-status-title' }, 'Services'), ...rows);
    if (banners) banners.replaceChildren(...problems.map(b => { if (onSettings) b.querySelector('a.btn')?.remove(); return b; }));
    return s;
  } catch {
    if (side) side.replaceChildren(statusRow('bad', "Can't reach Encore"));
    if (banners) banners.replaceChildren(banner('danger', "Can't reach Encore", 'The page will keep trying. Check that the encore service is running.', settings));
    return null;
  }
}

export const stored = key => { try { return localStorage.getItem(key); } catch { return null; } };
export const store = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
