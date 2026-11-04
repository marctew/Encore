// Encore's main page: Find, Library, Wanted, artist pages and the queue, as views of one page.
// Each view has its own address (/, /library, /wanted, /queue, /artist/<id>), so Back and reloads work.
import { $, api, busy, el, glyph, plural, refreshHealth, setCount, store, stored, toast } from './shell.js';

let albums = [], jobs = [], searched = false, confirming = null;
let mode = 'albums', detail = null, view = 'find', cameFrom = 'find';
let dragging = false, videos = null, page = null, studioOnly = true, wantAll = false, shelf = null, wanted = [];

const VIEWS = {
  find: ['Find', 'Search your library, then everywhere else'],
  library: ['Library', 'Everyone in Plex'],
  wanted: ['Wanted', "Albums you're after and don't have yet"],
  queue: ['Queue', 'Downloads, checks and filing'],
};
const PATHS = { find: '/', library: '/library', wanted: '/wanted', queue: '/queue' };
const wide = matchMedia('(min-width: 1280px)');

const ago = time => {
  const seconds = Date.now() / 1000 - time;
  if (seconds < 90) return 'just now';
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes ago`;
  if (seconds < 129600) return `${Math.round(seconds / 3600)} hours ago`;
  return `${Math.round(seconds / 86400)} days ago`;
};

const btn = (label, onclick, kind = 'ghost', attrs = {}) => el('button', { type: 'button', class: `btn btn-${kind} btn-sm`, onclick, ...attrs }, label);

// Every list has an empty state (7.11); smaller notes inside a page are dashed cards.
const emptyState = (title, body, name = 'music') => el('div', { class: 'card empty' },
  el('div', { class: 'empty-icon' }, glyph(name, 'icon')), el('h3', { class: 'h2' }, title), body ? el('p', {}, body) : null);
const note = (text, tone = '') => el('div', { class: `card-dashed note-card ${tone}` }, text);

$('fix').checked = stored('encore-fix') !== '0';
$('fix').onchange = () => store('encore-fix', $('fix').checked ? '1' : '0');
$('hideOwned').checked = stored('encore-hide-owned') === '1';
$('hideOwned').onchange = () => {
  store('encore-hide-owned', $('hideOwned').checked ? '1' : '0');
  renderAll();
};

// Square covers and round portraits sit on a placeholder; the image fades in, and if it fails the placeholder stays (7.12).
function picture(src, cls, fallback) {
  const img = el('img', { src, alt: '', loading: 'lazy' });
  img.onload = () => img.classList.add('loaded');
  img.onerror = () => img.remove();
  return el('div', { class: cls }, fallback, img);
}
const cover = src => picture(src, 'thumb cover', glyph('music', 'icon cover-glyph'));
const portrait = (src, name) => picture(src, 'portrait', el('span', { class: 'portrait-initial' }, (name || '?').slice(0, 1).toUpperCase()));
const photoUrl = (artist, version = '') => `/api/artist/${artist.id}/photo?best=${artist.best === false ? 0 : 1}&v=${version}`;

function activeJob(id) {
  return jobs.find(j => j.album_id === id && ['queued', 'downloading', 'verifying', 'filing', 'scanning', 'done'].includes(j.state));
}

function ownedNote(album) {
  const owned = album.owned;
  if (!owned) return null;
  const have = owned.quality ? ` at ${owned.quality}` : '';
  return el('div', { class: `owned ${owned.match === 'exact' ? 'exact' : 'close'}` },
    `${owned.match === 'exact' ? 'You have this' : 'Possible match'}${have}: ${owned.folder}`);
}

function albumAction(album) {
  const job = activeJob(album.id);
  if (job) return btn(job.state !== 'done' ? 'In the queue' : job.where === 'library' ? 'In your library' : 'In MusicImport', null, 'ghost', { disabled: true });
  return btn('Find downloads', () => openAlbum(album), 'primary');
}

function wantButton(album) {
  if (activeJob(album.id) || (album.owned && album.owned.match === 'exact')) return null;
  const button = btn(album.wanted ? 'Wanted' : 'Add to Wanted', event => toggleWanted(album, event.currentTarget), 'ghost', { 'aria-pressed': album.wanted ? 'true' : 'false' });
  if (album.wanted) button.prepend(glyph('star', 'icon-sm star-on'));
  return button;
}

function markWanted(ids, value) {
  const lists = [albums, page && page.albums ? page.albums : [], detail && detail.data ? [detail.data] : [], detail ? [detail.summary] : []];
  for (const list of lists) for (const album of list) if (ids.includes(album.id)) album.wanted = value;
}

async function toggleWanted(album, button) {
  const adding = !album.wanted;
  await busy(button, async () => {
    if (adding) await api('/api/wanted', { method: 'POST', body: JSON.stringify({ album_ids: [album.id] }) });
    else await api(`/api/wanted/${album.id}`, { method: 'DELETE' });
    markWanted([album.id], adding);
    await loadWanted();
    renderAll();
  });
}

function albumCard(album) {
  return el('article', { class: 'card album' },
    el('button', { type: 'button', class: 'open', 'aria-label': `Open ${album.title}`, onclick: () => openAlbum(album) }, cover(album.cover)),
    el('div', { class: 'album-body' },
      el('button', { type: 'button', class: 'open', onclick: () => openAlbum(album) }, el('h3', { class: 'h3 album-title' }, album.title)),
      el('div', { class: 'small muted' }, [album.artist, album.year].filter(Boolean).join(' · ')),
      album.kind ? el('div', { class: 'row meta' }, el('span', { class: 'badge-muted' }, album.kind)) : null,
      ownedNote(album),
      el('div', { class: 'row actions' }, albumAction(album), wantButton(album))));
}

function clock(seconds) {
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds / 60) % 60, rest = String(seconds % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`;
}

function trackRows(album) {
  const rows = [];
  let disc = null;
  for (const track of album.tracks_list) {
    if (album.discs > 1 && track.disc !== disc) {
      disc = track.disc;
      rows.push(el('li', { class: 'disc eyebrow' }, `Disc ${disc}`));
    }
    const guest = track.artist && track.artist !== album.artist ? el('small', { class: 'muted' }, track.artist) : null;
    rows.push(el('li', { class: 'track' },
      el('span', { class: 'num muted' }, track.number),
      el('span', { class: 'track-title' }, track.title, guest),
      el('span', { class: 'time muted tabular' }, track.seconds ? clock(track.seconds) : '')));
  }
  return el('section', { class: 'detail-section' }, el('h3', { class: 'h2' }, 'Tracks'), el('ol', { class: 'tracks' }, ...rows));
}

function sizeText(bytes) {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`;
}

function ageText(posted) {
  if (!posted) return '';
  const days = Math.floor((Date.now() / 1000 - posted) / 86400);
  return days >= 365 ? `${(days / 365).toFixed(1)} years old` : `${plural(days, 'day')} old`;
}

function releaseButton(album, row, label, best) {
  const queued = activeJob(album.id);
  if (queued) return btn(queued.state === 'done' ? 'Done' : 'In the queue', null, 'ghost', { disabled: true });
  if (album.owned && confirming !== row.key) return btn('Get anyway', () => { confirming = row.key; renderDetail(); });
  return btn(album.owned ? 'Yes, get it' : label, event => getRelease(album, row, event.currentTarget), best ? 'primary' : 'ghost');
}

function releaseRow(album, row, best) {
  const facts = [sizeText(row.size), row.indexer, ageText(row.posted)].filter(Boolean).join(' · ');
  const notes = [...row.notes];
  if (row.failed) notes.unshift(`Failed last time: ${row.failed}`);
  return el('li', { class: `release ${row.tier}` },
    el('div', { class: 'release-body' },
      el('div', { class: 'release-title' }, row.title),
      el('div', { class: 'row meta' },
        best ? el('span', { class: 'badge-accent' }, 'Best match') : null,
        row.quality ? el('span', { class: 'badge-info' }, row.quality) : null,
        el('span', { class: 'small muted' }, facts)),
      notes.length ? el('div', { class: row.tier === 'no' || row.failed ? 'why' : 'note' }, notes.join('. ') + '.') : null),
    releaseButton(album, row, row.tier === 'no' ? 'Get anyway' : 'Get', best));
}

function downloadsBlock(album) {
  const found = detail.downloads;
  const head = el('h3', { class: 'h2' }, 'Downloads');
  if (detail.downloadError) return el('section', { class: 'detail-section' }, head, note(detail.downloadError, 'bad'));
  if (!found) return el('section', { class: 'detail-section' }, head, note('Searching your indexers…'));
  const usable = found.results.filter(r => r.tier !== 'no'), other = found.results.filter(r => r.tier === 'no');
  const parts = found.problems.map(text => el('div', { class: 'why' }, `${text}.`));
  if (usable.length) parts.push(el('ul', { class: 'releases divide' }, ...usable.map((row, i) => releaseRow(album, row, i === 0 && !row.failed))));
  else parts.push(note(found.results.length ? 'Nothing here looks like the whole album in FLAC.' : 'Neither indexer has this album.'));
  if (other.length) {
    parts.push(el('details', { class: 'more', open: !usable.length || undefined },
      el('summary', {}, `${plural(other.length, 'other result')} left out`),
      el('ul', { class: 'releases divide' }, ...other.map(row => releaseRow(album, row, false)))));
  }
  return el('section', { class: 'detail-section' }, head, ...parts);
}

function renderDetail() {
  if (!detail) return;
  const album = detail.data || detail.summary;
  const facts = [album.released || album.year, album.label, album.kind, album.discs > 1 ? plural(album.discs, 'disc') : ''].filter(Boolean).join(' · ');
  const summary = album.tracks ? `${plural(album.tracks, 'track')} · ${album.minutes} min` : '';
  const body = [el('div', { class: 'detail-head' },
    cover(album.cover),
    el('div', { class: 'detail-info' },
      el('h2', { class: 'h1 detail-title' }, album.title),
      el('div', { class: 'muted' }, album.artist),
      summary ? el('div', { class: 'small muted tabular' }, summary) : null,
      facts ? el('div', { class: 'small muted' }, facts) : null,
      album.editions > 1 ? el('div', { class: 'small muted' }, `${album.editions} editions are known, and a download can fit any of them.`) : null,
      ownedNote(album),
      el('div', { class: 'row actions' }, wantButton(album),
        album.saved_at ? btn('Refresh tracks', () => openAlbum(album, true)) : null),
      album.saved_at ? el('div', { class: 'small faint' }, `Saved ${ago(album.saved_at)}`) : null))];
  if (detail.error) body.push(note(detail.error, 'bad'));
  else if (!detail.data) body.push(note('Loading tracks…'));
  else body.push(downloadsBlock(album), trackRows(album));
  $('detail').replaceChildren(
    el('button', { type: 'button', class: 'btn-icon dialog-close', 'aria-label': 'Close', title: 'Close', onclick: () => $('detail').close() }, glyph('x', 'icon')),
    el('div', { class: 'detail' }, ...body));
}

async function openAlbum(album, refresh = false) {
  detail = { summary: album };
  confirming = null;
  const opened = detail;
  renderDetail();
  if (!$('detail').open) $('detail').showModal();
  $('detail').scrollTop = 0;
  try {
    opened.data = await api(`/api/album/${album.id}${refresh ? '?refresh=1' : ''}`);
  } catch (error) {
    opened.error = error.message;
  }
  if (detail === opened) renderDetail();
  if (opened.error) return;
  try {
    opened.downloads = await api(`/api/album/${album.id}/downloads`);
  } catch (error) {
    opened.downloadError = error.message;
  }
  if (detail === opened) renderDetail();
}

$('detail').onclose = () => { detail = null; };
$('detail').onclick = event => { if (event.target === $('detail')) $('detail').close(); };

function renderResults() {
  const box = $('results');
  if (!searched) return;
  const hide = $('hideOwned').checked;
  const shown = albums.filter(a => !(hide && a.owned && a.owned.match === 'exact'));
  const hidden = albums.length - shown.length;
  $('hiddenCount').textContent = hidden && shown.length ? `${plural(hidden, 'album')} you have hidden` : '';
  if (!shown.length) {
    box.replaceChildren(hidden
      ? emptyState(`You already have ${hidden === 1 ? 'this album' : `all ${hidden} of these`}`, 'Untick Hide albums I have to see them.', 'check-circle')
      : emptyState('No albums found', 'Try fewer words, or search by artist instead.', 'search'));
    return;
  }
  box.replaceChildren(el('div', { class: 'album-grid' }, ...shown.map(albumCard)));
}

const stateText = {
  queued: 'Waiting', downloading: 'Downloading', verifying: 'Checking every track',
  filing: 'Filing the album', scanning: 'Plex is scanning',
};
const isRunning = job => ['downloading', 'verifying', 'filing', 'scanning'].includes(job.state);

function jobCard(job) {
  const running = isRunning(job);
  const percent = job.state === 'downloading' ? job.percent : 100;
  let state = ['failed', 'done'].includes(job.state) ? job.message : stateText[job.state];
  if (job.state === 'downloading') state = job.step ? `SABnzbd: ${job.step}` : `Downloading ${Math.round(job.percent)}%`;
  if (job.state === 'done' && job.quality) state += ` · ${job.quality}`;
  const buttons = [];
  if (job.state === 'failed') {
    if (job.kind !== 'video') buttons.push(btn('Pick another', () => openAlbum({ id: job.album_id, title: job.title, artist: job.artist, year: job.year, cover: job.cover })));
    buttons.push(btn('Retry', event => act(`/api/queue/${job.id}/retry`, 'POST', event.currentTarget)));
  }
  if (job.state === 'failed' && (job.can_import || job.message.startsWith('Not all FLAC'))) {
    buttons.push(btn('Import anyway', event => act(`/api/queue/${job.id}/import`, 'POST', event.currentTarget)));
  }
  if (job.state === 'failed' && job.can_partial) buttons.push(btn('Get the rest', event => act(`/api/queue/${job.id}/partial`, 'POST', event.currentTarget)));
  if (!['filing', 'scanning'].includes(job.state)) {
    buttons.push(btn(running ? 'Cancel' : job.state === 'queued' ? 'Remove' : 'Clear', event => act(`/api/queue/${job.id}`, 'DELETE', event.currentTarget)));
  }
  const grip = job.state === 'queued' ? el('button', { type: 'button', class: 'drag-handle', title: 'Drag to reorder',
    'aria-label': `Move ${job.title}. Use the up and down arrow keys.` }, glyph('grip', 'icon')) : null;
  if (grip) {
    grip.onpointerdown = event => startDrag(event, grip);
    grip.onkeydown = event => nudge(event, grip);
  }
  return el('div', { class: `job ${job.state} ${job.where || ''}`, 'data-job': job.id },
    grip,
    cover(job.cover),
    el('div', { class: 'job-body' },
      el('div', { class: 'job-title' }, `${job.artist} – ${job.title}`),
      el('div', { class: 'job-state small' }, state),
      running ? el('div', { class: 'progress-track job-bar' }, el('div', { class: 'progress-fill', style: `width:${percent}%` })) : null,
      buttons.length ? el('div', { class: 'row actions' }, ...buttons) : null));
}

function startDrag(event, grip) {
  event.preventDefault();
  const card = grip.closest('.job'), box = $('jobs');
  const startY = event.clientY;
  const others = [...box.querySelectorAll('.job.queued')].filter(other => other !== card);
  let target = null;
  const mark = (node, name) => { others.forEach(o => o.classList.remove('drop-before', 'drop-after')); if (node) node.classList.add(name); };
  dragging = true;
  card.classList.add('dragging');
  grip.setPointerCapture(event.pointerId);
  grip.onpointermove = move => {
    card.style.transform = `translateY(${move.clientY - startY}px)`;
    target = others.find(other => {
      const rect = other.getBoundingClientRect();
      return move.clientY < rect.top + rect.height / 2;
    }) || null;
    if (target) mark(target, 'drop-before');
    else if (others.length) mark(others[others.length - 1], 'drop-after');
  };
  const finish = keep => {
    grip.onpointermove = grip.onpointerup = grip.onpointercancel = null;
    mark(null);
    card.classList.remove('dragging');
    card.style.transform = '';
    dragging = false;
    if (!keep) return renderJobs();
    if (target) box.insertBefore(card, target);
    else if (others.length) others[others.length - 1].after(card);
    saveOrder();
  };
  grip.onpointerup = () => finish(true);
  grip.onpointercancel = () => finish(false);
}

function nudge(event, grip) {
  if (!['ArrowUp', 'ArrowDown'].includes(event.key)) return;
  event.preventDefault();
  const card = grip.closest('.job');
  const queued = [...$('jobs').querySelectorAll('.job.queued')];
  const place = queued.indexOf(card) + (event.key === 'ArrowUp' ? -1 : 1);
  if (place < 0 || place >= queued.length) return;
  if (event.key === 'ArrowUp') queued[place].before(card); else queued[place].after(card);
  saveOrder(card.dataset.job);
}

async function saveOrder(focus) {
  const ids = [...$('jobs').querySelectorAll('.job.queued')].map(card => card.dataset.job);
  const rest = jobs.filter(job => !ids.includes(job.id));
  jobs = [...rest, ...ids.map(id => jobs.find(job => job.id === id)).filter(Boolean)];
  renderJobs();
  if (focus) $('jobs').querySelector(`[data-job="${focus}"] .drag-handle`)?.focus();
  try { await api('/api/queue/order', { method: 'POST', body: JSON.stringify({ ids }) }); } catch (error) { toast(error.message, 'error'); }
  refreshJobs();
}

function renderJobs() {
  if (dragging) return;
  const running = jobs.filter(isRunning), waiting = jobs.filter(j => j.state === 'queued');
  const finished = jobs.filter(j => ['failed', 'done'].includes(j.state)).reverse();
  const part = text => el('h3', { class: 'eyebrow part-title' }, text);
  $('clearDone').hidden = !jobs.some(j => j.state === 'done');
  setCount('queue', running.length + waiting.length);
  if (!jobs.length) {
    return $('jobs').replaceChildren(el('div', { class: 'queue-empty' },
      el('div', { class: 'empty-icon' }, glyph('download', 'icon')),
      el('div', { class: 'h3' }, 'Nothing queued yet'),
      el('p', { class: 'small muted' }, 'Open an album, pick a download, and it waits here while it downloads, gets checked and filed.')));
  }
  $('jobs').replaceChildren(
    ...(running.length ? [part('Now'), ...running.map(jobCard)] : []),
    ...(waiting.length ? [part('Up next'), ...waiting.map(jobCard)] : []),
    ...(finished.length ? [part('Finished'), ...finished.map(jobCard)] : []));
}

async function refreshJobs() {
  if (dragging) return;
  try {
    const next = (await api('/api/queue')).jobs;
    if (dragging) return;
    const changed = JSON.stringify(next) !== JSON.stringify(jobs);
    jobs = next;
    if (changed) { renderJobs(); renderAll(); }
  } catch {}
}

async function act(path, method, button) {
  await busy(button, () => api(path, { method }));
  refreshJobs();
}

async function getRelease(album, row, button) {
  confirming = null;
  const added = await busy(button, () => api('/api/queue', { method: 'POST', body: JSON.stringify({ album_id: album.id, release: row.key, fix_artist: $('fix').checked }) }));
  if (added !== undefined) toast(`${album.title} is in the queue.`);
  await refreshJobs();
  await loadWanted();
  renderAll();
}

function renderAll() {
  renderResults();
  renderArtistPage();
  renderDetail();
  if (view === 'wanted') renderWanted();
}

// ------------------------------------------------------------------ views and addresses

function show(next, { push = true } = {}) {
  view = next;
  const shown = next === 'queue' ? 'find' : next;
  $('viewFind').hidden = shown !== 'find';
  $('viewArtist').hidden = shown !== 'artist';
  $('viewLibrary').hidden = shown !== 'library';
  $('viewWanted').hidden = shown !== 'wanted';
  $('layout').dataset.view = next;
  $('pageHeader').hidden = next === 'artist';
  if (VIEWS[shown]) {
    $('viewTitle').textContent = VIEWS[shown][0];
    $('viewSub').textContent = VIEWS[shown][1];
  }
  $('viewActions').replaceChildren();
  const tab = next === 'artist' ? cameFrom : next;
  document.querySelectorAll('.nav-link[data-view], .bottom-nav [data-view]').forEach(link => link.classList.toggle('active', link.dataset.view === tab));
  const title = next === 'artist' ? (page && page.artist ? page.artist.name : 'Artist') : VIEWS[next][0];
  document.title = next === 'find' ? 'Encore' : `${title} · Encore`;
  if (push) {
    const url = next === 'artist' ? `/artist/${page.id}` : PATHS[next];
    if (location.pathname !== url) history.pushState({ view: next, artist: next === 'artist' ? page.id : null, best: page ? page.best : true }, '', url);
  }
}

function go(next, options) {
  if (next === 'queue' && wide.matches) next = 'find';
  show(next, options);
  if (next === 'library') loadLibrary();
  if (next === 'wanted') loadWanted();
}

document.querySelectorAll('a[data-view]').forEach(link => {
  link.onclick = event => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
    event.preventDefault();
    go(link.dataset.view);
    scrollTo(0, 0);
  };
});

addEventListener('popstate', event => {
  const state = event.state || { view: viewFromPath() };
  if (state.view === 'artist' && state.artist) openArtistPage(state.artist, false, state.best !== false, { push: false });
  else go(state.view || 'find', { push: false });
});

function viewFromPath() {
  const path = location.pathname;
  if (path.startsWith('/artist/')) return 'artist';
  return Object.keys(PATHS).find(key => PATHS[key] === path) || 'find';
}

wide.onchange = () => { if (wide.matches && view === 'queue') show('find'); };

function freshness(saved, refresh, label = 'Refresh') {
  return [saved ? el('span', { class: 'small faint' }, `Saved ${ago(saved)}`) : null,
    btn(label, event => busy(event.currentTarget, refresh), 'ghost')].filter(Boolean);
}

function artistFacts(artist) {
  const span = artist.begin ? `${artist.begin}–${artist.end || (artist.ended ? '' : 'now')}` : '';
  return [artist.kind, artist.country, span, artist.note].filter(Boolean).join(' · ');
}

function tagList(artist) {
  return artist.tags && artist.tags.length ? el('div', { class: 'row tags' }, ...artist.tags.map(tag => el('span', { class: 'badge-muted' }, tag))) : null;
}

function artistPick(artist) {
  return el('button', { type: 'button', class: 'card pick', onclick: () => openArtistPage(artist.id, false, artist.best) },
    portrait(photoUrl(artist), artist.name),
    el('div', { class: 'pick-body' },
      el('div', { class: 'row pick-name' }, el('h3', { class: 'h2' }, artist.name), artist.in_library ? el('span', { class: 'badge-success' }, 'In your library') : null),
      el('div', { class: 'small muted' }, artistFacts(artist)),
      tagList(artist)));
}

async function openArtistPage(id, refresh = false, best = true, { push = true } = {}) {
  if (view !== 'artist') cameFrom = view === 'queue' ? 'find' : view;
  page = { id, loading: true, best };
  wantAll = false;
  show('artist', { push });
  renderArtistPage();
  scrollTo(0, 0);
  try {
    const data = await api(`/api/artist/${id}${refresh ? '?refresh=1' : ''}`);
    if (page.id !== id) return;
    page = { ...data, id, best };
    page.artist.best = best;
    document.title = `${page.artist.name} · Encore`;
  } catch (error) {
    if (page.id === id) page = { id, best, error: error.message };
  }
  renderArtistPage();
}

function backButton() {
  const back = btn('Back', () => go(cameFrom));
  back.prepend(glyph('chevron-left'));
  back.classList.add('back');
  return back;
}

function renderArtistPage() {
  if (!page || view !== 'artist') return;
  const back = backButton();
  if (page.loading) return $('viewArtist').replaceChildren(back, note('Loading albums. The first visit to an artist takes a moment.'));
  if (page.error) return $('viewArtist').replaceChildren(back, note(page.error, 'bad'));
  const { artist } = page;
  const chosen = page.albums.filter(a => !studioOnly || a.kind === 'Album');
  const have = chosen.filter(a => a.owned && a.owned.match === 'exact').length;
  const hide = $('hideOwned').checked;
  const shown = chosen.filter(a => !(hide && a.owned && a.owned.match === 'exact'));
  const missing = chosen.filter(a => !a.owned && !a.wanted && !activeJob(a.id));
  const filter = (label, value) => el('button', { type: 'button', role: 'radio', 'aria-checked': studioOnly === value ? 'true' : 'false',
    onclick: () => { studioOnly = value; wantAll = false; renderArtistPage(); } }, label);
  const want = missing.length ? btn(wantAll ? `Yes, add ${plural(missing.length, 'album')}` : `Add ${missing.length} missing to Wanted`,
    event => addAllMissing(missing, event.currentTarget), wantAll ? 'primary' : 'ghost') : null;
  const sections = [back,
    el('div', { class: 'artist-head' },
      portrait(photoUrl(artist, Math.round(page.saved_at)), artist.name),
      el('div', { class: 'artist-info' },
        el('h1', { class: 'h1' }, artist.name),
        el('div', { class: 'muted' }, artistFacts(artist)),
        tagList(artist),
        el('div', { class: 'small muted' }, `You have ${have} of ${plural(chosen.length, 'album')} here`))),
    el('div', { class: 'row-between toolbar' },
      el('div', { class: 'row' },
        el('div', { class: 'segmented seg-text', role: 'radiogroup', 'aria-label': 'Show' }, filter('Studio albums', true), filter('Everything', false)),
        want),
      el('div', { class: 'row' }, ...freshness(page.saved_at, () => openArtistPage(page.id, true, page.best, { push: false })))),
    shown.length ? el('div', { class: 'album-grid' }, ...shown.map(albumCard))
      : emptyState(chosen.length ? 'You have all of these' : 'No albums here', chosen.length ? 'Untick Hide albums I have to see them.' : 'Try Everything to see singles, EPs and live albums.', chosen.length ? 'check-circle' : 'music')];
  sections.push(el('h2', { class: 'h2 section-title' }, 'Videos'), videoBlock());
  if (page.others.length) {
    sections.push(el('h2', { class: 'h2 section-title' }, 'Also in your library'),
      el('div', { class: 'album-grid' }, ...page.others.map(album => el('article', { class: 'card album' }, cover(album.cover),
        el('div', { class: 'album-body' }, el('h3', { class: 'h3 album-title' }, album.title), el('div', { class: 'small muted' }, album.year || ''))))));
  }
  $('viewArtist').replaceChildren(...sections);
}

function videoRow(row) {
  const job = activeJob(`video-${row.key}`);
  const facts = [sizeText(row.size), row.indexer, ageText(row.posted)].filter(Boolean).join(' · ');
  const notes = [...row.notes];
  if (row.failed) notes.unshift(`Failed last time: ${row.failed}`);
  return el('li', { class: `release ${row.tier}` },
    el('div', { class: 'release-body' },
      el('div', { class: 'release-title' }, el('strong', {}, row.song || 'Unknown song')),
      el('div', { class: 'row meta' }, el('span', { class: 'badge-info' }, row.quality), el('span', { class: 'small muted' }, facts)),
      el('div', { class: 'small faint' }, row.title),
      notes.length ? el('div', { class: row.tier === 'no' || row.failed ? 'why' : 'note' }, notes.join('. ') + '.') : null),
    job ? btn(job.state === 'done' ? 'Done' : 'In the queue', null, 'ghost', { disabled: true })
      : btn(row.tier === 'no' ? 'Get anyway' : 'Get', event => getVideo(row, event.currentTarget), 'ghost', { disabled: !row.song }));
}

function videoBlock() {
  if (!videos || videos.id !== page.id) {
    const find = btn('Find videos', () => findVideos());
    find.prepend(glyph('film'));
    return el('div', { class: 'card card-pad video-card' }, el('p', { class: 'small muted' }, "Searches your indexers for this artist's music videos. Plex shows them in the artist's Extras."), find);
  }
  if (videos.loading) return note('Searching your indexers…');
  if (videos.error) return note(videos.error, 'bad');
  const usable = videos.results.filter(r => r.tier !== 'no'), other = videos.results.filter(r => r.tier === 'no');
  const parts = videos.problems.map(text => el('div', { class: 'why' }, `${text}.`));
  if (usable.length) parts.push(el('ul', { class: 'releases divide' }, ...usable.map(videoRow)));
  else parts.push(note('No usable videos found for this artist.'));
  if (other.length) {
    parts.push(el('details', { class: 'more' }, el('summary', {}, `${plural(other.length, 'other result')} left out`),
      el('ul', { class: 'releases divide' }, ...other.map(videoRow))));
  }
  return el('div', { class: 'card card-pad' }, ...parts);
}

async function findVideos() {
  const id = page.id;
  videos = { id, loading: true };
  renderArtistPage();
  try {
    videos = { id, ...(await api(`/api/artist/${id}/videos`)) };
  } catch (error) {
    videos = { id, error: error.message };
  }
  renderArtistPage();
}

async function getVideo(row, button) {
  const added = await busy(button, () => api('/api/videos', { method: 'POST', body: JSON.stringify({ artist_id: page.id, release: row.key, song: row.song }) }));
  if (added !== undefined) toast(`${row.song} is in the queue.`);
  await refreshJobs();
  renderArtistPage();
}

async function addAllMissing(missing, button) {
  if (!wantAll) { wantAll = true; return renderArtistPage(); }
  wantAll = false;
  const ids = missing.map(a => a.id);
  const done = await busy(button, async () => {
    await api('/api/wanted', { method: 'POST', body: JSON.stringify({ album_ids: ids }) });
    markWanted(ids, true);
    await loadWanted();
    return true;
  });
  if (done) toast(`Added ${plural(ids.length, 'album')} to Wanted.`);
  renderArtistPage();
}

async function loadLibrary(sync = false) {
  if (!shelf) $('viewLibrary').replaceChildren(note('Loading your library…'));
  try {
    shelf = await api('/api/library' + (sync ? '/sync' : ''), { method: sync ? 'POST' : 'GET' });
    renderLibrary();
    renderLocal();
  } catch (error) {
    $('viewLibrary').replaceChildren(note(error.message, 'bad'));
  }
}

function libraryTile(artist) {
  const open = () => artist.id ? openArtistPage(artist.id) : searchFor(artist.name);
  return el('button', { type: 'button', class: 'card tile', onclick: open },
    portrait(`/api/plex/thumb/${artist.rating}`, artist.name),
    el('strong', { class: 'tile-name' }, artist.name), el('span', { class: 'small muted' }, plural(artist.albums, 'album')));
}

const plainText = text => text.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();

function libraryMatches() {
  const words = plainText($('query').value.trim());
  return words && shelf ? shelf.artists.filter(a => plainText(a.name).includes(words)) : [];
}

function renderLocal() {
  const found = libraryMatches();
  $('local').replaceChildren(...(found.length ? [el('h2', { class: 'h2 section-title first' }, 'In your library'),
    el('div', { class: 'tiles' }, ...found.slice(0, 12).map(libraryTile))] : []));
  return found;
}

function renderLibrary() {
  if (view === 'library') {
    $('viewSub').textContent = shelf.artists.length ? `${plural(shelf.artists.length, 'artist')} · ${plural(shelf.albums, 'album')}` : VIEWS.library[1];
    $('viewActions').replaceChildren(...freshness(shelf.saved_at, () => loadLibrary(true), 'Sync with Plex'));
  }
  if (!shelf.artists.length) {
    return $('viewLibrary').replaceChildren(emptyState('Nothing from Plex yet', 'Connect Plex from the banner above, then Sync with Plex.', 'plex'));
  }
  $('viewLibrary').replaceChildren(el('div', { class: 'tiles' }, ...shelf.artists.map(libraryTile)));
}

async function loadWanted() {
  try {
    wanted = (await api('/api/wanted')).albums;
    setCount('wanted', wanted.length);
    if (view === 'wanted') renderWanted();
  } catch (error) {
    if (view === 'wanted') $('viewWanted').replaceChildren(note(error.message, 'bad'));
  }
}

function wantedRow(album) {
  const job = activeJob(album.id);
  return el('article', { class: 'wanted-row' },
    cover(album.cover),
    el('div', { class: 'wanted-body' },
      el('button', { type: 'button', class: 'open', onclick: () => openAlbum(album) }, el('h3', { class: 'h3 album-title' }, album.title)),
      el('div', { class: 'small muted' }, [album.artist, album.year].filter(Boolean).join(' · ')),
      el('div', { class: 'small faint' }, job ? 'In the queue' : `Added ${ago(album.added)}`)),
    el('div', { class: 'row actions' },
      btn('Find downloads', () => openAlbum(album), 'primary', { disabled: !!job }),
      btn('Remove', event => toggleWanted({ ...album, wanted: true }, event.currentTarget))));
}

function renderWanted() {
  if (view === 'wanted') $('viewSub').textContent = wanted.length ? plural(wanted.length, 'album') : VIEWS.wanted[1];
  $('viewWanted').replaceChildren(wanted.length
    ? el('div', { class: 'card divide wanted-list' }, ...wanted.map(wantedRow))
    : emptyState('Nothing wanted yet', 'Add albums from an artist page or an album.', 'star'));
}

function searchFor(name) {
  setMode('artist');
  $('query').value = name;
  go('find');
  $('find').requestSubmit();
}

function showFresh(saved, refresh) {
  $('fresh').replaceChildren(...freshness(saved, refresh));
}

async function searchArtists(q, refresh) {
  const data = await api(`/api/artists?q=${encodeURIComponent(q)}${refresh ? '&refresh=1' : ''}`);
  searched = false;
  showFresh(data.saved_at, () => runSearch(true));
  const mine = libraryMatches();
  const known = new Set(mine.map(a => a.id));
  const others = data.artists.filter(a => !known.has(a.id));
  $('results').replaceChildren(...(others.length ? [
    ...(mine.length ? [el('h2', { class: 'h2 section-title' }, 'Everyone else')] : []),
    el('div', { class: 'picks' }, ...others.map(artistPick))]
    : [mine.length ? note('Nobody else has that name.') : emptyState('No artists with that name', 'Check the spelling, or search albums instead.', 'search')]));
}

async function runSearch(refresh = false) {
  const q = $('query').value.trim();
  if (!q) return;
  $('hiddenCount').textContent = '';
  $('fresh').replaceChildren();
  $('results').replaceChildren(note('Searching…'));
  try {
    if (mode === 'artist') return await searchArtists(q, refresh);
    const data = await api(`/api/search?q=${encodeURIComponent(q)}${refresh ? '&refresh=1' : ''}`);
    albums = data.albums;
    showFresh(data.saved_at, () => runSearch(true));
    searched = true;
    renderResults();
  } catch (error) { $('results').replaceChildren(note(error.message, 'bad')); }
}

const idle = () => emptyState("What's next?", 'Search your library and everywhere else from the box above.', 'search');

$('find').onsubmit = event => {
  event.preventDefault();
  go('find');
  runSearch();
};

$('query').oninput = () => {
  const q = $('query').value.trim();
  const found = renderLocal();
  searched = false;
  $('fresh').replaceChildren();
  $('hiddenCount').textContent = '';
  if (q && view !== 'find') go('find');
  $('results').replaceChildren(!q ? idle()
    : note(found.length ? 'Press Enter to search everywhere too.' : `No one called “${q}” in your library. Press Enter to search everywhere.`));
};

function setMode(next) {
  mode = next;
  store('encore-mode', next);
  document.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-checked', b.dataset.mode === next));
  $('query').placeholder = next === 'artist' ? 'Artist name' : 'Artist or album';
}

document.querySelectorAll('[data-mode]').forEach(button => {
  button.onclick = () => {
    setMode(button.dataset.mode);
    if ($('query').value.trim()) $('find').requestSubmit();
  };
});
if (stored('encore-mode') === 'artist') setMode('artist');

$('clearDone').onclick = event => act('/api/queue/clear', 'POST', event.currentTarget);

// ------------------------------------------------------------------ start

$('results').replaceChildren(idle());
const start = $('layout').dataset;
history.replaceState({ view: start.view, artist: start.artist || null, best: true }, '', location.pathname);
if (start.view === 'artist' && start.artist) openArtistPage(start.artist, false, true, { push: false });
else go(start.view || 'find', { push: false });
renderJobs();
refreshHealth();
loadWanted();
loadLibrary();
refreshJobs();
setInterval(refreshJobs, 2000);
setInterval(refreshHealth, 60000);
