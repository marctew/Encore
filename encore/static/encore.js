// Encore's main page: Find, Library, Wanted, artist pages and the queue, as views of one page.
// Each view has its own address (/, /library, /wanted, /queue, /artist/<id>), so Back and reloads work.
import { $, api, busy, el, glyph, plural, refreshHealth, setCount, store, stored, toast } from './shell.js';

let albums = [], jobs = [], searched = false, confirming = null;
let mode = 'albums', detail = null, view = 'find', cameFrom = 'find';
let dragging = false, videos = null, single = null, singleNode = null, page = null, studioOnly = true, wantAll = false, shelf = null, wanted = [];

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
// Pictures already shown once skip the fade-in, so redrawing a list doesn't make them blink.
const shownPictures = new Set();

function picture(src, cls, fallback) {
  const seen = shownPictures.has(src);
  const img = el('img', { src, alt: '', loading: seen ? 'eager' : 'lazy', class: seen ? 'loaded' : undefined });
  img.onload = () => { img.classList.add('loaded'); shownPictures.add(src); };
  img.onerror = () => img.remove();
  return el('div', { class: cls }, fallback, img);
}
const cover = src => picture(src, 'thumb cover', glyph('music', 'icon cover-glyph'));
const portrait = (src, name) => picture(src, 'portrait', el('span', { class: 'portrait-initial' }, (name || '?').slice(0, 1).toUpperCase()));
const photoUrl = (artist, version = '') => `/api/artist/${artist.id}/photo?best=${artist.best === false ? 0 : 1}&v=${version}`;

function activeJob(id) {
  return jobs.find(j => j.album_id === id && ['queued', 'downloading', 'verifying', 'filing', 'scanning', 'review', 'done'].includes(j.state));
}

function ownedNote(album) {
  const owned = album.owned;
  if (!owned) return null;
  const have = owned.quality ? ` at ${owned.quality}` : '';
  return el('div', { class: `owned ${owned.match === 'exact' ? 'exact' : 'close'}` },
    `${owned.match === 'exact' ? 'You have this' : 'Possible match'}${have}: ${owned.folder}`);
}

// Opens the artist or album in Plex's web app, in a new tab. Only shown for what's really in Plex.
const plexLink = (url, name) => url ? el('a', { class: 'btn btn-ghost btn-sm', href: url, target: '_blank', rel: 'noopener noreferrer',
  title: `Open ${name} in Plex`, 'aria-label': `Open ${name} in Plex` }, glyph('plex', 'icon-sm'), 'Plex') : null;

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
      el('div', { class: 'row actions' }, albumAction(album), wantButton(album), plexLink(album.plex_url, album.title))));
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

function renderDetail(force = false) {
  if (!detail) return;
  // The builder keeps its own rows, so the queue's refresh doesn't wipe a link half typed.
  if (detail.view === 'youtube' && !force && $('detail').querySelector('.yt-builder')) return refreshYoutubeBar();
  if (detail.view === 'split' && !force && $('detail').querySelector('.split')) return;
  if (detail.view === 'free') {
    if (!force && $('detail').querySelector('.free')) return;
    return $('detail').replaceChildren(
      el('button', { type: 'button', class: 'btn-icon dialog-close', 'aria-label': 'Close', title: 'Close', onclick: () => $('detail').close() }, glyph('x', 'icon')),
      el('div', { class: 'detail' }, freeBlock()));
  }
  const album = detail.data || detail.summary;
  const switchView = ['youtube', 'split'].includes(detail.view)
    ? btn('Back to downloads', () => showAlbumView(album))
    : btn('Build from YouTube', () => openYoutube(album));
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
        detail.data && !detail.error && !detail.free ? switchView : null,
        plexLink(album.plex_url, album.title),
        album.saved_at ? btn('Refresh tracks', () => openAlbum(album, true, detail.view, detail.fillJob)) : null),
      album.saved_at ? el('div', { class: 'small faint' }, `Saved ${ago(album.saved_at)}`) : null))];
  if (detail.error) body.push(note(detail.error, 'bad'));
  else if (!detail.data) body.push(note('Loading tracks…'));
  else if (detail.view === 'youtube') body.push(youtubeBlock(album));
  else if (detail.view === 'split') body.push(splitBlock(album));
  else body.push(downloadsBlock(album), trackRows(album));
  $('detail').replaceChildren(
    el('button', { type: 'button', class: 'btn-icon dialog-close', 'aria-label': 'Close', title: 'Close', onclick: () => $('detail').close() }, glyph('x', 'icon')),
    el('div', { class: 'detail' }, ...body));
}

async function openAlbum(album, refresh = false, startIn = 'album', fillJob = null) {
  detail = { summary: album, view: startIn, fillJob };
  const opened = detail;
  openDialog();
  try {
    opened.data = await api(`/api/album/${album.id}${refresh ? '?refresh=1' : ''}`);
  } catch (error) {
    opened.error = error.message;
  }
  if (detail === opened) renderDetail(true);
  if (opened.error) return;
  if (opened.view === 'youtube') return loadYoutube(opened.data);
  if (opened.view === 'split') return loadSplit(opened);
  await loadDownloads(opened, album);
}

function openDialog() {
  confirming = null;
  renderDetail(true);
  if (!$('detail').open) {
    $('detail').showModal();
    // A history entry for the open window, so the phone's back gesture (or Back) closes it.
    history.pushState({ ...(history.state || {}), album: true }, '', location.href);
    albumEntry = true;
  }
  $('detail').scrollTop = 0;
}

// ------------------------------------------------------------------ an album MusicBrainz doesn't have
// One YouTube video, named by hand: Encore guesses the names from the title and description.

function openFree() {
  detail = { view: 'free', summary: { id: '', title: 'Album from YouTube' }, free: true, freeVideo: null, freeForm: null };
  openDialog();
  setTimeout(() => $('detail').querySelector('.free .yt-url')?.focus(), 50);
}

function freeBlock() {
  const video = detail.freeVideo, form = detail.freeForm, working = detail.freeBusy;
  const input = el('input', { class: 'input yt-url', type: 'url', inputmode: 'url', placeholder: 'Paste the YouTube link',
    value: detail.freeTyped ?? (video ? watchUrl(video.id) : ''), disabled: working || undefined, 'aria-label': 'YouTube link for the whole album' });
  let sent = false;
  const commit = () => { if (sent || !input.value.trim()) return; sent = true; lookupFree(input.value.trim()); };
  input.onchange = commit;
  input.onpaste = () => setTimeout(commit, 0);
  input.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); commit(); } };
  const parts = [
    el('h2', { class: 'h1 detail-title' }, 'Album from YouTube'),
    el('p', { class: 'small muted yt-intro' }, "For a whole album in one YouTube video, when MusicBrainz doesn't have the album (or the artist). Encore takes its sound, finds where each track starts from the video's chapters or the gaps between songs, and asks you to check before it cuts and files anything. It's filed as Artist/Album (Year), tagged from what you type here, with the video's thumbnail as the cover. The sound is AAC, as YouTube has it."),
    el('div', { class: 'yt-input-row' }, input),
    working ? el('div', { class: 'small muted yt-status' }, 'Looking it up…') : null,
    detail.freeError ? el('div', { class: 'why' }, `${detail.freeError}.`.replace(/\.\.$/, '.')) : null];
  if (video && !working) parts.push(videoCard(video, true));
  if (form && !working) {
    const field = (label, key, attrs = {}) => {
      const box = el('input', { class: 'input', value: form[key], 'aria-label': label, ...attrs });
      box.oninput = () => { form[key] = box.value; };
      return el('label', { class: 'field free-field' }, el('span', { class: 'label' }, label), box);
    };
    const list = el('textarea', { class: 'input free-tracks', rows: Math.min(16, Math.max(6, form.tracks.split('\n').length + 1)),
      'aria-label': 'Track names, one on each line', placeholder: 'One track on each line' });
    list.value = form.tracks;
    list.oninput = () => { form.tracks = list.value; refreshFreeCount(); };
    const job = detail.freeJob || jobs.find(j => j.album_id === `yt-album-${video.id}` && !['done', 'failed'].includes(j.state));
    const queue = btn(job ? 'In the queue' : 'Queue', event => queueFree(event.currentTarget), 'primary', { disabled: !!job || undefined });
    parts.push(
      el('div', { class: 'free-grid' }, field('Artist', 'artist'), field('Album', 'title'),
        field('Year', 'year', { inputmode: 'numeric', maxlength: 4, placeholder: 'Blank if unknown', class: 'input free-year' })),
      el('label', { class: 'field free-field' }, el('span', { class: 'label' }, 'Tracks'), list,
        el('span', { class: 'hint free-count' }, freeCount(form.tracks, video))),
      el('div', { class: 'yt-bar' }, el('span', {}), el('div', { class: 'row actions' }, queue)));
  }
  return el('section', { class: 'free' }, ...parts);
}

function trackNames(text) {
  return text.split('\n').map(line => line.trim()).filter(Boolean);
}

function freeCount(text, video) {
  const names = trackNames(text), chapters = (video.chapters || []).length;
  if (!names.length) return "Leave it empty and Encore decides how many tracks there are from the gaps. You name them when you check them.";
  const how = chapters === names.length ? `The video has ${plural(chapters, 'chapter')}, one for each, so Encore starts from those.` : 'Encore finds that many starts at the longest gaps in the sound.';
  return `${plural(names.length, 'track')}. ${how}`;
}

function refreshFreeCount() {
  const hint = $('detail').querySelector('.free-count');
  if (hint && detail.freeForm) hint.textContent = freeCount(detail.freeForm.tracks, detail.freeVideo);
}

async function lookupFree(url) {
  const opened = detail;
  Object.assign(opened, { freeBusy: true, freeError: null, freeTyped: url });
  renderDetail(true);
  try {
    const found = await api('/api/youtube/album', { method: 'POST', body: JSON.stringify({ url }) });
    const guess = found.guess;
    Object.assign(opened, { freeVideo: found.video, freeJob: found.job, freeTyped: undefined,
      freeForm: { artist: guess.artist, title: guess.title, year: guess.year, tracks: guess.tracks.join('\n') } });
  } catch (error) {
    opened.freeError = error.message;
  }
  opened.freeBusy = false;
  if (detail === opened) renderDetail(true);
}

async function queueFree(button) {
  const opened = detail, form = opened.freeForm, video = opened.freeVideo;
  const names = trackNames(form.tracks);
  const added = await busy(button, () => api('/api/queue/youtube/free', { method: 'POST',
    body: JSON.stringify({ video: video.id, artist: form.artist, title: form.title, year: form.year.trim(), tracks: names }) }));
  if (added === undefined) return;
  toast(`${form.title} is in the queue. Once it's downloaded, Encore asks you to check the tracks.`);
  $('detail').close();
  await refreshJobs();
}

async function loadDownloads(opened, album) {
  opened.downloadsAsked = true;
  try {
    opened.downloads = await api(`/api/album/${album.id}/downloads`);
  } catch (error) {
    opened.downloadError = error.message;
  }
  if (detail === opened) renderDetail(true);
}

function showAlbumView(album) {
  stopPlaying();
  detail.view = 'album';
  detail.fillJob = null;
  detail.filling = false;
  renderDetail(true);
  if (!detail.downloadsAsked) loadDownloads(detail, album);
}

// ------------------------------------------------------------------ an album from YouTube links
// One link per track. The page only ever changes one row at a time, and saves each link as it goes.

const watchUrl = id => `https://youtu.be/${id}`;
const FIT = { good: ['badge-success', 'Good fit'], close: ['badge-warning', 'Length differs'], off: ['badge-danger', 'Very different'] };

function openYoutube(album) {
  Object.assign(detail, { view: 'youtube', yt: null, ytError: null, ytBusy: {}, ytErrors: {}, ytTyped: {}, found: {}, filling: false });
  renderDetail(true);
  loadYoutube(album);
}

async function loadYoutube(album) {
  const opened = detail;
  Object.assign(opened, { ytBusy: opened.ytBusy || {}, ytErrors: opened.ytErrors || {}, ytTyped: opened.ytTyped || {}, found: opened.found || {} });
  const only = opened.fillJob ? `?job=${opened.fillJob}` : '';
  try { opened.yt = await api(`/api/album/${album.id}/youtube${only}`); } catch (error) { opened.ytError = error.message; }
  if (opened.yt && !opened.ytMode) opened.ytMode = opened.yt.whole && !opened.yt.tracks.some(t => t.video) ? 'whole' : 'tracks';
  if (detail === opened) renderDetail(true);
}

function videoFacts(video) {
  const fit = FIT[video.fit];
  return el('div', { class: 'row meta' },
    fit ? el('span', { class: fit[0] }, fit[1]) : null,
    video.audio_only ? el('span', { class: 'badge-muted' }, 'Audio only') : null,
    el('span', { class: 'small muted' }, [video.channel, video.seconds ? clock(video.seconds) : ''].filter(Boolean).join(' · ')));
}

function videoCard(video, linked) {
  const name = linked
    ? el('a', { class: 'yt-video-title', href: watchUrl(video.id), target: '_blank', rel: 'noopener noreferrer' }, video.title)
    : el('div', { class: 'yt-video-title' }, video.title);
  const notes = (video.notes || []).filter(text => !text.startsWith('Audio only'));
  return el('div', { class: 'yt-video' },
    picture(video.thumb, 'yt-thumb', glyph('film', 'icon')),
    el('div', { class: 'yt-video-body' }, name, videoFacts(video),
      notes.length ? el('div', { class: 'note' }, notes.join('. ') + '.') : null));
}

function youtubeRow(album, track) {
  const key = track.key, working = detail.ytBusy[key], found = detail.found[key];
  const current = track.video ? watchUrl(track.video.id) : '';
  // A link that was refused stays in the box beside its reason, so it can be fixed.
  const input = el('input', { class: 'input yt-url', type: 'url', inputmode: 'url', placeholder: 'Paste a YouTube link',
    value: detail.ytTyped[key] ?? current, disabled: working || undefined, 'aria-label': `YouTube link for ${track.title}` });
  // Enter, paste and leaving the box can all fire for one link; only the first sends it.
  let sent = false;
  const commit = () => {
    if (sent || input.value.trim() === current) return;
    sent = true;
    setLink(album, key, input.value.trim());
  };
  input.onchange = commit;
  input.onpaste = () => setTimeout(commit, 0);
  input.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); commit(); } };
  const find = el('button', { type: 'button', class: 'btn-icon', title: 'Find on YouTube', 'aria-label': `Find ${track.title} on YouTube`,
    disabled: working || found === 'loading' || undefined, onclick: () => findOnYoutube(album, key).catch(() => {}) }, glyph('search', 'icon-sm'));
  const clear = track.video ? el('button', { type: 'button', class: 'btn-icon', title: 'Remove link', 'aria-label': `Remove the link for ${track.title}`,
    disabled: working || undefined, onclick: () => setLink(album, key, '') }, glyph('x', 'icon-sm')) : null;
  let choices = null;
  if (found === 'loading') choices = el('div', { class: 'small muted yt-status' }, 'Searching YouTube…');
  else if (found) {
    choices = el('div', { class: 'yt-choices' },
      el('div', { class: 'row-between' }, el('span', { class: 'eyebrow' }, found.length ? 'Pick one' : 'Nothing found'),
        btn('Close', () => { delete detail.found[key]; updateRow(album, key); })),
      el('ul', { class: 'divide' }, ...found.map(video => el('li', {},
        el('button', { type: 'button', class: 'yt-choice', onclick: () => setLink(album, key, video.id) }, videoCard(video, false))))));
  }
  const guest = track.artist && track.artist !== album.artist ? el('small', { class: 'muted' }, track.artist) : null;
  return el('li', { class: 'yt-track', 'data-key': key },
    el('div', { class: 'yt-left' },
      el('span', { class: 'num muted' }, track.number),
      el('span', { class: 'track-title' }, track.title, guest),
      el('span', { class: 'time muted tabular' }, track.seconds ? clock(track.seconds) : '')),
    el('div', { class: 'yt-right' },
      el('div', { class: 'yt-input-row' }, input, find, clear),
      working ? el('div', { class: 'small muted yt-status' }, 'Looking it up…') : null,
      track.video && !working ? videoCard(track.video, true) : null,
      detail.ytErrors[key] ? el('div', { class: 'why' }, `${detail.ytErrors[key]}.`.replace(/\.\.$/, '.')) : null,
      choices));
}

function youtubeBar(album) {
  const yt = detail.yt, linked = yt.tracks.filter(t => t.video).length, job = activeJob(album.id);
  const edition = yt.editions.length > 1 && !yt.fill ? el('select', { class: 'input yt-edition', 'aria-label': 'Edition' },
    ...yt.editions.map(e => el('option', { value: e.id, selected: e.id === yt.edition || undefined }, e.label))) : null;
  if (edition) edition.onchange = () => changeEdition(album, edition.value, edition);
  const fill = btn(detail.filling ? 'Stop' : 'Fill all', () => fillAll(album), 'ghost',
    { disabled: (!detail.filling && linked === yt.tracks.length) || undefined });
  const queue = yt.fill
    ? btn('Fill and file', event => fillGaps(album, event.currentTarget), 'primary', { disabled: !linked || detail.filling || undefined })
    : btn(job ? (job.state === 'done' ? 'Done' : 'In the queue') : 'Queue', event => queueYoutube(album, event.currentTarget), 'primary',
      { disabled: !!job || !linked || detail.filling || undefined });
  return el('div', { class: 'yt-bar' }, edition,
    el('span', { class: 'small muted tabular yt-count' }, `${linked} of ${plural(yt.tracks.length, 'track')} linked`),
    el('div', { class: 'row actions' }, fill, queue));
}

function youtubeBlock(album) {
  const fill = detail.yt && detail.yt.fill, gaps = fill ? detail.yt.tracks.length : 0;
  const head = el('h3', { class: 'h2' }, fill ? 'Fill the gaps from YouTube' : 'Build from YouTube');
  const intro = el('p', { class: 'small muted yt-intro' }, fill
    ? `${plural(gaps, 'track')} didn't come with the download. Find ${gaps === 1 ? 'it' : 'them'} on YouTube and Encore files ${gaps === 1 ? 'it' : 'them'} with the other ${fill.have}, as AAC beside the FLAC. Videos go with the artist's music videos.`
    : "Paste a link for each track, or let Encore find them. The sound becomes the album, as YouTube has it (AAC), and each video is filed with the artist's music videos.");
  if (detail.ytError) return el('section', { class: 'detail-section yt-builder' }, head, note(detail.ytError, 'bad'));
  if (!detail.yt) return el('section', { class: 'detail-section yt-builder' }, head, note('Loading tracks…'));
  if (!fill && detail.ytMode === 'whole') return el('section', { class: 'detail-section yt-builder' }, head, youtubeModes(), ...wholeBlock(album));
  const rows = [];
  let disc = null;
  for (const track of detail.yt.tracks) {
    if (detail.yt.discs > 1 && track.disc !== disc) {
      disc = track.disc;
      rows.push(el('li', { class: 'disc eyebrow' }, `Disc ${disc}`));
    }
    rows.push(youtubeRow(album, track));
  }
  return el('section', { class: 'detail-section yt-builder' }, head, fill ? null : youtubeModes(), intro, youtubeBar(album), el('ol', { class: 'yt-tracks' }, ...rows));
}

function youtubeModes() {
  const choice = (label, value) => el('button', { type: 'button', role: 'radio', 'aria-checked': detail.ytMode === value ? 'true' : 'false',
    onclick: () => { if (detail.ytMode !== value) { detail.ytMode = value; renderDetail(true); } } }, label);
  return el('div', { class: 'segmented seg-text yt-mode', role: 'radiogroup', 'aria-label': 'Build from' },
    choice('A link per track', 'tracks'), choice('One video of the whole album', 'whole'));
}

// ------------------------------------------------------------------ one video of the whole album
// One link for the lot. Encore downloads its sound, finds where each track starts, and waits in the
// queue for a person to check the joins (the split view, below) before anything is cut or filed.

function chapterNote(video, tracks) {
  const count = (video.chapters || []).length;
  if (count === tracks) return el('div', { class: 'small yt-chapters ok' }, glyph('check', 'icon-sm'), `${plural(count, 'chapter')} on the video, one for each track.`);
  if (count) return el('div', { class: 'note' }, `The video has ${plural(count, 'chapter')} for ${plural(tracks, 'track')}, so Encore will go by the track lengths and the gaps instead.`);
  return el('div', { class: 'small muted' }, 'No chapters or timestamps on the video, so Encore will go by the track lengths and the gaps.');
}

function wholeBlock(album) {
  const yt = detail.yt, video = yt.whole, working = detail.wholeBusy, found = detail.wholeFound;
  const current = video ? watchUrl(video.id) : '';
  const input = el('input', { class: 'input yt-url', type: 'url', inputmode: 'url', placeholder: 'Paste the YouTube link',
    value: detail.wholeTyped ?? current, disabled: working || undefined, 'aria-label': `YouTube link for the whole of ${album.title}` });
  let sent = false;
  const commit = () => {
    if (sent || input.value.trim() === current) return;
    sent = true;
    setWhole(album, input.value.trim());
  };
  input.onchange = commit;
  input.onpaste = () => setTimeout(commit, 0);
  input.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); commit(); } };
  const find = el('button', { type: 'button', class: 'btn-icon', title: 'Find on YouTube', 'aria-label': `Find ${album.title} on YouTube`,
    disabled: working || found === 'loading' || undefined, onclick: () => findWhole(album) }, glyph('search', 'icon-sm'));
  const clear = video ? el('button', { type: 'button', class: 'btn-icon', title: 'Remove link', 'aria-label': 'Remove the link',
    disabled: working || undefined, onclick: () => setWhole(album, '') }, glyph('x', 'icon-sm')) : null;
  let choices = null;
  if (found === 'loading') choices = el('div', { class: 'small muted yt-status' }, 'Searching YouTube…');
  else if (found) {
    choices = el('div', { class: 'yt-choices' },
      el('div', { class: 'row-between' }, el('span', { class: 'eyebrow' }, found.length ? 'Pick one' : 'Nothing found'),
        btn('Close', () => { detail.wholeFound = null; renderDetail(true); })),
      el('ul', { class: 'divide' }, ...found.map(item => el('li', {},
        el('button', { type: 'button', class: 'yt-choice', onclick: () => setWhole(album, item.id) }, videoCard(item, false))))));
  }
  const job = activeJob(album.id);
  const edition = yt.editions.length > 1 ? el('select', { class: 'input yt-edition', 'aria-label': 'Edition' },
    ...yt.editions.map(e => el('option', { value: e.id, selected: e.id === yt.edition || undefined }, e.label))) : null;
  if (edition) edition.onchange = () => changeEdition(album, edition.value, edition);
  const queue = btn(job ? (job.state === 'done' ? 'Done' : 'In the queue') : 'Queue', event => queueWhole(album, event.currentTarget), 'primary',
    { disabled: !!job || !video || working || undefined });
  return [
    el('p', { class: 'small muted yt-intro' }, `For an upload of the whole album in one video. Encore takes its sound, works out where each of the ${plural(yt.tracks.length, 'track')} starts, from the timestamps in the description if it has them and the gaps between songs, and then asks you to check before it cuts and files anything. The album is AAC, as YouTube has it, and no music videos are filed.`),
    el('div', { class: 'yt-bar' }, edition,
      el('span', { class: 'small muted tabular yt-count' }, `${plural(yt.tracks.length, 'track')} · ${clock(yt.seconds)}`),
      el('div', { class: 'row actions' }, queue)),
    el('div', { class: 'yt-whole' },
      el('div', { class: 'yt-input-row' }, input, find, clear),
      working ? el('div', { class: 'small muted yt-status' }, 'Looking it up…') : null,
      video && !working ? videoCard(video, true) : null,
      video && !working ? chapterNote(video, yt.tracks.length) : null,
      detail.wholeError ? el('div', { class: 'why' }, `${detail.wholeError}.`.replace(/\.\.$/, '.')) : null,
      choices)];
}

async function setWhole(album, url) {
  const opened = detail;
  Object.assign(opened, { wholeBusy: true, wholeError: null });
  renderDetail(true);
  try {
    opened.yt = await api(`/api/album/${album.id}/youtube/whole`, { method: 'POST', body: JSON.stringify({ url }) });
    opened.wholeFound = null;
    opened.wholeTyped = undefined;
  } catch (error) {
    opened.wholeError = error.message;
    opened.wholeTyped = url;
  }
  opened.wholeBusy = false;
  if (detail === opened) renderDetail(true);
}

async function findWhole(album) {
  const opened = detail;
  Object.assign(opened, { wholeFound: 'loading', wholeError: null });
  renderDetail(true);
  try {
    opened.wholeFound = (await api(`/api/album/${album.id}/youtube/whole/find`, { method: 'POST' })).results;
  } catch (error) {
    opened.wholeFound = null;
    opened.wholeError = error.message;
  }
  if (detail === opened) renderDetail(true);
}

async function queueWhole(album, button) {
  const video = detail.yt.whole;
  if (video.fit === 'off' && !(await window.duConfirm(button, `The video is very different in length from the album. Queue it anyway?`, 'Queue anyway'))) return;
  const added = await busy(button, () => api('/api/queue/youtube/whole', { method: 'POST', body: JSON.stringify({ album_id: album.id }) }));
  if (added === undefined) return;
  toast(`${album.title} is in the queue. Once it's downloaded, Encore asks you to check the tracks.`);
  await refreshJobs();
  await loadWanted();
  if (detail) showAlbumView(detail.data || detail.summary);
}

// ------------------------------------------------------------------ checking where each track starts
// cuts holds N + 1 boundaries for N tracks: each track's start, then where the last one ends. Every
// change is saved as it's made, so a restart keeps it; nothing is cut until File the album.

const audio = new Audio();
audio.preload = 'none';
let playing = null;          // { key, stop } while a join plays
const fmt = seconds => {
  const whole = Math.floor(seconds), tenth = Math.floor((seconds - whole) * 10 + 1e-6);
  return `${clock(whole)}.${tenth}`;
};
const fitOf = (listed, seconds) => {
  if (!listed || !seconds) return '';
  const gap = Math.abs(listed - seconds);
  if (gap <= Math.max(4, listed * 0.05)) return 'good';
  return gap <= Math.max(10, listed * 0.15) ? 'close' : 'off';
};
const SHORTEST = 1;

function openSplit(job) {
  const album = { id: job.album_id, title: job.title, artist: job.artist, year: job.year, cover: job.cover };
  if (!(job.source && job.source.free)) return openAlbum(album, false, 'split', job.id);
  // No MusicBrainz album to load: the job says all the page shows of it.
  detail = { summary: album, data: album, view: 'split', fillJob: job.id, free: true };
  openDialog();
  loadSplit(detail);
}

async function loadSplit(opened) {
  try {
    const split = opened.split = await api(`/api/queue/${opened.fillJob}/split`);
    opened.cuts = [...split.cuts];
    opened.marks = [...split.marks];
    opened.titles = split.free ? [...split.titles] : null;
    opened.picked = 1;
    opened.cursor = null;
  } catch (error) {
    opened.splitError = error.message;
  }
  if (detail === opened) renderDetail(true);
}

function stopPlaying() {
  audio.pause();
  if (playing) cancelAnimationFrame(playing.frame);
  playing = null;
  const head = document.querySelector('.split-head');
  if (head) head.hidden = true;
  document.querySelectorAll('.split .playing').forEach(node => node.classList.remove('playing'));
}

// Plays from..to (to null: until stopped), moving the playhead. The same key again stops it.
function play(key, from, to) {
  const again = playing && playing.key === key;
  stopPlaying();
  if (again) return;
  const src = `/api/queue/${detail.fillJob}/split/audio`;
  if (!audio.src.endsWith(src)) audio.src = src;
  const duration = detail.split.duration;
  audio.currentTime = Math.max(0, from);
  audio.play().catch(() => { toast("Couldn't play the album's sound", 'error'); stopPlaying(); });
  playing = { key, frame: 0, from };
  document.querySelector(`.split [data-play="${key}"]`)?.classList.add('playing');
  const tick = () => {
    if (!playing) return;
    const head = document.querySelector('.split-head');
    if (head) { head.hidden = false; head.style.left = `${audio.currentTime / duration * 100}%`; }
    if ((to !== null && audio.currentTime >= to) || audio.ended) return stopPlaying();
    playing.frame = requestAnimationFrame(tick);
  };
  playing.frame = requestAnimationFrame(tick);
}

let saveTimer = null;
function saveCuts(opened) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try { await api(`/api/queue/${opened.fillJob}/split`, { method: 'POST', body: JSON.stringify({ cuts: opened.cuts, titles: opened.titles }) }); }
    catch (error) { toast(error.message, 'error'); }
  }, 600);
}

function limits(cuts, place, duration) {
  return [place === 0 ? 0 : cuts[place - 1] + SHORTEST, place === cuts.length - 1 ? duration : cuts[place + 1] - SHORTEST];
}

function setCut(place, value, redraw = true) {
  const cuts = detail.cuts, [low, high] = limits(cuts, place, detail.split.duration);
  cuts[place] = Math.round(Math.min(high, Math.max(low, value)) * 10) / 10;
  if (place < detail.marks.length) detail.marks[place] = { from: 'you', gap: false, moved: 0 };
  detail.picked = place;
  saveCuts(detail);
  if (redraw) redrawSplit();
}

function snap(place) {
  const cuts = detail.cuts, [low, high] = limits(cuts, place, detail.split.duration);
  const near = detail.split.gaps.filter(at => at >= low && at <= high && Math.abs(at - cuts[place]) <= 30 && Math.abs(at - cuts[place]) > 0.05)
    .sort((a, b) => Math.abs(a - cuts[place]) - Math.abs(b - cuts[place]))[0];
  if (near === undefined) return toast('No other gap within 30 seconds of here.');
  setCut(place, near);
}

function redrawSplit() {
  const node = $('detail').querySelector('.split');
  if (node) node.replaceWith(splitBlock(detail.data || detail.summary));
}

function waveform(peaks) {
  const n = peaks.length, top = [], bottom = [];
  peaks.forEach((value, i) => { const h = Math.max(0.5, value / 2); top.push(`${i},${50 - h}`); bottom.unshift(`${i},${50 + h}`); });
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${Math.max(1, n - 1)} 100`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const shape = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
  shape.setAttribute('points', [...top, ...bottom].join(' '));
  svg.append(shape);
  return svg;
}

// How sure Encore is of a start: a chapter mark and a gap agreeing is best; a start from the track
// lengths (or put halfway, with no track lengths) with no gap near it is a guess.
function markBadge(place) {
  const mark = detail.marks[place];
  if (!mark) return null;
  if (mark.from === 'you') return el('span', { class: 'badge-muted' }, 'Set by you');
  if (place === 0) return null;
  if (mark.from === 'chapter') return mark.gap ? el('span', { class: 'badge-success' }, 'Timestamp and gap') : el('span', { class: 'badge-warning' }, 'No gap at the timestamp');
  return mark.gap ? el('span', { class: 'badge-info' }, 'Gap found') : el('span', { class: 'badge-danger' }, 'Guess, no gap near');
}

// An album without MusicBrainz can gain or lose tracks: join one with the next, or split where it's playing.
function joinNext(place) {
  detail.cuts.splice(place + 1, 1);
  detail.marks.splice(place + 1, 1);
  detail.titles.splice(place + 1, 1);
  detail.picked = place;
  saveCuts(detail);
  redrawSplit();
}

function splitHere() {
  // Until the sound has loaded the playhead reads 0, so it's never taken as earlier than where play started.
  const at = Math.round((playing ? Math.max(audio.currentTime, playing.from) : detail.cursor ?? -1) * 10) / 10;
  const cuts = detail.cuts, after = cuts.findIndex(c => c > at);
  if (at < 0) return toast('Click the waveform where the new track starts, or play it and press this as it starts.');
  if (after <= 0 || at - cuts[after - 1] < SHORTEST || cuts[after] - at < SHORTEST) return toast('Too close to another track start.');
  cuts.splice(after, 0, at);
  detail.marks.splice(after, 0, { from: 'you', gap: false, moved: 0 });
  detail.titles.splice(after, 0, '');
  detail.picked = after;
  saveCuts(detail);
  redrawSplit();
  toast(`New track at ${fmt(at)}. Give it a name.`);
}

function nudges(place, label) {
  const step = (by, text) => el('button', { type: 'button', class: 'btn btn-ghost btn-sm split-step', 'aria-label': `${label}: ${text}`,
    onclick: () => setCut(place, detail.cuts[place] + by) }, text);
  return [step(-2, '−2s'), step(-0.5, '−½s'), step(0.5, '+½s'), step(2, '+2s'),
    el('button', { type: 'button', class: 'btn btn-ghost btn-sm', title: 'Move to the nearest other gap', onclick: () => snap(place) }, 'Next gap')];
}

function playButton(key, label, from, to) {
  return el('button', { type: 'button', class: 'btn-icon split-play', 'data-play': key, title: label, 'aria-label': label,
    onclick: () => play(key, from, to) }, glyph('play', 'icon-sm'));
}

function splitTitle(track, place) {
  if (!detail.free) return el('span', { class: 'track-title' }, track.title,
    track.chapter && track.chapter.toLowerCase() !== track.title.toLowerCase() ? el('small', { class: 'muted' }, `Timestamp: ${track.chapter}`) : null);
  const input = el('input', { class: 'input split-name', value: detail.titles[place], placeholder: `Track ${place + 1}`, maxlength: 200,
    'aria-label': `Name of track ${place + 1}` });
  input.oninput = () => { detail.titles[place] = input.value; saveCuts(detail); };
  return input;
}

function splitRow(split, cuts, track, place, multi) {
  const start = cuts[place], length = cuts[place + 1] - start, fit = FIT[fitOf(track.seconds, length)];
  const join = place === 0 ? playButton('s0', `Play the start of ${track.title}`, start, start + 6)
    : playButton(`s${place}`, `Play the join into ${track.title}`, start - 4, start + 4);
  return el('li', { class: `split-track${detail.picked === place ? ' picked' : ''}`, 'data-place': place },
    el('div', { class: 'split-left' },
      el('span', { class: 'num muted' }, multi ? `${track.disc}.${track.number}` : track.number),
      splitTitle(track, place),
      el('span', { class: 'time muted tabular' }, track.seconds ? clock(track.seconds) : '')),
    el('div', { class: 'split-right' },
      el('div', { class: 'row meta' },
        join,
        el('span', { class: 'small tabular split-at' }, `Starts ${fmt(start)}`),
        el('span', { class: 'small muted tabular' }, `${clock(Math.round(length))} long`),
        fit ? el('span', { class: fit[0] }, fit[1]) : null,
        markBadge(place)),
      el('div', { class: 'row split-steps' }, ...nudges(place, `Start of ${track.title}`),
        detail.free && place < cuts.length - 2 ? el('button', { type: 'button', class: 'btn btn-ghost btn-sm', title: 'Make this and the next track one',
          onclick: () => joinNext(place) }, 'Join with next') : null)));
}

function splitMarker(split, cuts, place, label) {
  const mark = el('button', { type: 'button', class: `split-mark${detail.picked === place ? ' on' : ''}`, style: `left:${cuts[place] / split.duration * 100}%`,
    'aria-label': `${label}, ${fmt(cuts[place])}. Arrow keys move it.`, title: label }, el('span', {}, place === cuts.length - 1 ? 'End' : String(place + 1)));
  mark.onkeydown = event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    setCut(place, cuts[place] + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 2 : 0.5));
    $('detail').querySelector(`.split-mark[data-place="${place}"]`)?.focus();
  };
  mark.dataset.place = place;
  mark.onclick = event => event.stopPropagation();
  mark.onpointerdown = event => {
    event.preventDefault();
    event.stopPropagation();
    const wave = mark.parentElement, box = wave.getBoundingClientRect();
    mark.setPointerCapture(event.pointerId);
    detail.picked = place;
    mark.classList.add('on');
    let moved = false;
    mark.onpointermove = move => {
      moved = true;
      const [low, high] = limits(detail.cuts, place, split.duration);
      const at = Math.min(high, Math.max(low, (move.clientX - box.left) / box.width * split.duration));
      detail.cuts[place] = Math.round(at * 10) / 10;
      mark.style.left = `${detail.cuts[place] / split.duration * 100}%`;
    };
    mark.onpointerup = mark.onpointercancel = () => {
      mark.onpointermove = mark.onpointerup = mark.onpointercancel = null;
      if (moved) setCut(place, detail.cuts[place]);
      else {
        redrawSplit();
        $('detail').querySelector(`.split-track[data-place="${place}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    };
  };
  return mark;
}

function splitBlock(album) {
  const head = el('h3', { class: 'h2' }, 'Check the tracks');
  if (detail.splitError) return el('section', { class: 'detail-section split' }, head, note(detail.splitError, 'bad'));
  if (!detail.split) return el('section', { class: 'detail-section split' }, head, note('Loading where each track starts…'));
  const split = detail.split, cuts = detail.cuts, last = cuts.length - 1;
  const tracks = detail.free
    ? detail.titles.map((title, n) => ({ key: `1-${n + 1}`, disc: 1, number: n + 1, title: title || `Track ${n + 1}`, seconds: 0, chapter: '' }))
    : split.tracks;
  const how = {
    chapters: "Encore took each start from the video's chapters (the timestamps in its description), then moved it to the nearest gap in the sound.",
    lengths: "Encore worked out each start from the album's track lengths, then moved it to the nearest gap in the sound.",
    gaps: "There are no track lengths for this album, so Encore split it at the longest gaps in the sound.",
  }[split.source];
  const unsure = detail.marks.filter((m, i) => i > 0 && m.from !== 'you' && !m.gap).length;
  const cursor = detail.free && detail.cursor != null ? el('div', { class: 'split-cursor', style: `left:${detail.cursor / split.duration * 100}%` }) : null;
  const wave = el('div', { class: 'split-wave', title: 'Click to play from here' }, waveform(split.peaks), cursor, el('div', { class: 'split-head', hidden: true }),
    ...cuts.map((_, place) => splitMarker(split, cuts, place, place === last ? 'End of the last track' : `Start of ${tracks[place].title}`)));
  wave.onclick = event => {
    const box = wave.getBoundingClientRect(), at = (event.clientX - box.left) / box.width * split.duration;
    detail.cursor = at;
    const line = wave.querySelector('.split-cursor');
    if (line) line.style.left = `${at / split.duration * 100}%`;
    else if (detail.free) wave.prepend(el('div', { class: 'split-cursor', style: `left:${at / split.duration * 100}%` }));
    play('wave', at, null);
  };
  const multi = split.discs > 1;
  const rows = tracks.map((track, place) => splitRow(split, cuts, track, place, multi));
  const end = el('li', { class: `split-track${detail.picked === last ? ' picked' : ''}`, 'data-place': last },
    el('div', { class: 'split-left' }, el('span', {}), el('span', { class: 'track-title muted' }, 'End of the album'), el('span', {})),
    el('div', { class: 'split-right' },
      el('div', { class: 'row meta' }, playButton('end', 'Play the end of the album', cuts[last] - 6, cuts[last]),
        el('span', { class: 'small tabular split-at' }, `Ends ${fmt(cuts[last])}`),
        cuts[last] < split.duration - 0.05 ? el('span', { class: 'badge-muted' }, `${clock(Math.round(split.duration - cuts[last]))} left off`) : null),
      el('div', { class: 'row split-steps' }, ...nudges(last, 'End of the album').slice(0, 4))));
  const changed = cuts.length !== split.found.length || cuts.some((c, i) => Math.abs(c - split.found[i]) > 0.05)
    || (detail.free && detail.titles.some((t, i) => t !== split.found_titles[i]));
  const reset = btn('Undo my changes', () => {
    Object.assign(detail, { cuts: [...split.found], marks: [...split.found_marks], titles: detail.free ? [...split.found_titles] : null });
    saveCuts(detail);
    redrawSplit();
  }, 'ghost', { disabled: !changed || undefined });
  const splitButton = detail.free ? btn('Split here', () => splitHere(), 'ghost', { title: "Starts a new track where it's playing, or where you last clicked the waveform" }) : null;
  const file = btn('File the album', event => fileSplit(album, event.currentTarget), 'primary');
  return el('section', { class: 'detail-section split' }, head,
    el('p', { class: 'small muted yt-intro' }, `${how} Play each join to hear that it's clean, and move any that are off with the buttons, the arrow keys or by dragging its marker.${detail.free ? ' Join two tracks that should be one, or press Split here as a new track starts. Name each track.' : ''} Nothing is cut until you press File the album.`),
    ...split.warnings.map(text => el('div', { class: 'note split-warning' }, text)),
    unsure ? el('div', { class: 'note split-warning' }, `${plural(unsure, 'start')} had no gap nearby. Listen to ${unsure === 1 ? 'that one' : 'those'} first.`) : null,
    wave,
    el('div', { class: 'row-between small muted tabular split-scale' }, el('span', {}, '0:00'), el('span', {}, clock(Math.round(split.duration)))),
    el('div', { class: 'yt-bar' }, el('span', { class: 'small muted tabular yt-count' }, `${plural(tracks.length, 'track')} · ${split.job.source.title}`),
      el('div', { class: 'row actions' }, splitButton, reset, file)),
    el('ol', { class: 'split-tracks' }, ...rows, end));
}

async function fileSplit(album, button) {
  const opened = detail;
  clearTimeout(saveTimer);
  const done = await busy(button, () => api(`/api/queue/${opened.fillJob}/split`, { method: 'POST', body: JSON.stringify({ cuts: opened.cuts, titles: opened.titles, file: true }) }));
  if (done === undefined) return;
  stopPlaying();
  toast(`${album.title} is being cut into ${plural(opened.cuts.length - 1, 'track')} and filed.`);
  $('detail').close();
  await refreshJobs();
}

function updateRow(album, key) {
  const node = $('detail').querySelector(`.yt-track[data-key="${key}"]`), track = detail.yt.tracks.find(t => t.key === key);
  if (node && node.isConnected && track) node.replaceWith(youtubeRow(album, track));
  refreshYoutubeBar();
}

function refreshYoutubeBar() {
  const bar = $('detail').querySelector('.yt-bar');
  if (bar && detail && detail.yt) bar.replaceWith(youtubeBar(detail.data || detail.summary));
}

async function setLink(album, key, url) {
  const opened = detail;
  opened.ytBusy[key] = true;
  delete opened.ytErrors[key];
  updateRow(album, key);
  try {
    const row = await api(`/api/album/${album.id}/youtube/link`, { method: 'POST', body: JSON.stringify({ key, url }) });
    opened.yt.tracks = opened.yt.tracks.map(t => (t.key === key ? row : t));
    delete opened.found[key];
    delete opened.ytTyped[key];
  } catch (error) {
    opened.ytErrors[key] = error.message;
    opened.ytTyped[key] = url;
  }
  opened.ytBusy[key] = false;
  if (detail === opened) updateRow(album, key);
}

async function findOnYoutube(album, key) {
  const opened = detail;
  opened.found[key] = 'loading';
  delete opened.ytErrors[key];
  updateRow(album, key);
  let failure = null;
  try {
    opened.found[key] = (await api(`/api/album/${album.id}/youtube/find`, { method: 'POST', body: JSON.stringify({ key }) })).results;
  } catch (error) {
    delete opened.found[key];
    opened.ytErrors[key] = error.message;
    failure = error;
  }
  if (detail === opened) updateRow(album, key);
  if (failure) throw failure;
  return opened.found[key];
}

// One track at a time, so YouTube isn't asked twelve things at once. A best match that's far off
// in length isn't taken: its choices stay open for a person to pick from. If Encore can't be reached
// (a tunnel reconnecting takes a few seconds to a minute), it waits and tries the same track again,
// and stops if it still can't, rather than failing every track left.
const RETRY_WAITS = [5000, 15000, 30000];
const pause = ms => new Promise(done => setTimeout(done, ms));

async function fillAll(album) {
  const opened = detail;
  if (opened.filling) { opened.filling = false; return refreshYoutubeBar(); }
  opened.filling = true;
  refreshYoutubeBar();
  const going = () => opened.filling && detail === opened && opened.view === 'youtube';
  for (const track of opened.yt.tracks.filter(t => !t.video)) {
    let found;
    for (let attempt = 0; going(); attempt++) {
      try {
        found = await findOnYoutube(album, track.key);
        break;
      } catch (error) {
        if (!error.unreachable || attempt >= RETRY_WAITS.length) break;
        opened.ytErrors[track.key] = `${error.message}. Trying again in ${RETRY_WAITS[attempt] / 1000} seconds`;
        if (detail === opened) updateRow(album, track.key);
        await pause(RETRY_WAITS[attempt]);
      }
    }
    if (!going()) break;
    if (!found && opened.ytErrors[track.key]?.startsWith("Couldn't reach Encore")) {
      toast("Fill all stopped: Encore can't be reached. Try again in a minute.", 'error');
      break;
    }
    const best = Array.isArray(found) ? found[0] : null;
    if (best && going() && best.fit !== 'off' && best.score > 50) await setLink(album, track.key, best.id);
  }
  opened.filling = false;
  if (detail === opened) refreshYoutubeBar();
}

async function changeEdition(album, edition, select) {
  const opened = detail;
  await busy(select, async () => {
    opened.yt = await api(`/api/album/${album.id}/youtube/edition`, { method: 'POST', body: JSON.stringify({ edition }) });
    opened.found = {};
    opened.ytErrors = {};
    opened.ytTyped = {};
  });
  if (detail === opened) renderDetail(true);
}

async function fillGaps(album, button) {
  const tracks = detail.yt.tracks, linked = tracks.filter(t => t.video), blank = tracks.length - linked.length;
  if (blank && !(await window.duConfirm(button, `${plural(blank, 'track')} still ${blank === 1 ? 'has' : 'have'} no link. File the album without ${blank === 1 ? 'it' : 'them'}?`, 'File without them'))) return;
  const off = linked.filter(t => t.video.fit === 'off').length;
  if (off && !(await window.duConfirm(button, `${plural(off, 'video')} ${off === 1 ? 'is' : 'are'} very different in length from the album. Use ${off === 1 ? 'it' : 'them'} anyway?`, 'Use anyway'))) return;
  const done = await busy(button, () => api(`/api/queue/${detail.fillJob}/fill`, { method: 'POST' }));
  if (done === undefined) return;
  toast(`${album.title} is back in the queue with ${plural(linked.length, 'track')} from YouTube.`);
  $('detail').close();
  await refreshJobs();
}

async function queueYoutube(album, button) {
  const tracks = detail.yt.tracks, linked = tracks.filter(t => t.video), blank = tracks.length - linked.length;
  if (blank && !(await window.duConfirm(button, `${plural(blank, 'track')} ${blank === 1 ? 'has' : 'have'} no link. Queue the other ${linked.length}?`, `Queue ${plural(linked.length, 'track')}`))) return;
  const off = linked.filter(t => t.video.fit === 'off').length;
  if (off && !(await window.duConfirm(button, `${plural(off, 'video')} ${off === 1 ? 'is' : 'are'} very different in length from the album. Queue anyway?`, 'Queue anyway'))) return;
  const added = await busy(button, () => api('/api/queue/youtube', { method: 'POST', body: JSON.stringify({ album_id: album.id, partial: blank > 0 }) }));
  if (added === undefined) return;
  toast(`${album.title} is in the queue.`);
  await refreshJobs();
  await loadWanted();
  if (detail) showAlbumView(detail.data || detail.summary);
}

// Closed from its own button, Escape or the backdrop: drop the history entry it added, and ignore
// the popstate that causes.
let albumEntry = false, skipPop = false;
$('detail').onclose = () => {
  stopPlaying();
  detail = null;
  if (albumEntry) {
    albumEntry = false;
    skipPop = true;
    history.back();
  }
};
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
  filing: 'Filing the album', scanning: 'Plex is scanning', review: 'Downloaded. Check where each track starts, then file it.',
};
const isRunning = job => ['downloading', 'verifying', 'filing', 'scanning'].includes(job.state);

function jobCard(job) {
  const running = isRunning(job);
  const copying = job.state === 'filing' && job.step;
  const percent = job.state === 'downloading' || copying ? job.percent : 100;
  let state = ['failed', 'done'].includes(job.state) ? job.message : stateText[job.state];
  if (job.state === 'downloading') {
    if (job.kind === 'youtube' || (job.step || '').startsWith('YouTube')) state = `${job.step || 'Downloading'} · ${Math.round(job.percent)}%`;
    else state = job.step ? `SABnzbd: ${job.step}` : `Downloading ${Math.round(job.percent)}%`;
  }
  if (copying) state = `${stateText.filing}: ${job.step.toLowerCase()} · ${Math.round(job.percent)}%`;
  if (job.state === 'done' && job.quality) state += ` · ${job.quality}`;
  const buttons = [];
  if (job.state === 'review') buttons.push(btn('Check the tracks', () => openSplit(job), 'primary'));
  if (job.state === 'failed') {
    const album = { id: job.album_id, title: job.title, artist: job.artist, year: job.year, cover: job.cover };
    if (job.kind === 'youtube') buttons.push(btn('Change links', () => openAlbum(album, false, 'youtube')));
    else if (job.kind === 'youtube_album' && !job.source.free) buttons.push(btn('Change the video', () => openAlbum(album, false, 'youtube')));
    else if (job.kind !== 'video' && job.kind !== 'single') buttons.push(btn('Pick another', () => openAlbum(album)));
    buttons.push(btn('Retry', event => act(`/api/queue/${job.id}/retry`, 'POST', event.currentTarget)));
  }
  if (job.state === 'failed' && (job.can_import || job.message.startsWith('Not all FLAC'))) {
    buttons.push(btn('Import anyway', event => act(`/api/queue/${job.id}/import`, 'POST', event.currentTarget)));
  }
  if (job.state === 'failed' && job.can_partial) {
    if (job.kind !== 'youtube' && job.missing && job.missing.length) {
      buttons.push(btn('Fill from YouTube', () => openAlbum({ id: job.album_id, title: job.title, artist: job.artist, year: job.year, cover: job.cover }, false, 'youtube', job.id), 'primary'));
    }
    buttons.push(btn('Get the rest', event => act(`/api/queue/${job.id}/partial`, 'POST', event.currentTarget)));
  }
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
      el('div', { class: 'job-title' }, `${job.artist} – ${job.title}`, job.kind === 'youtube' || (job.source && job.source.type === 'youtube') ? el('span', { class: 'badge-muted job-kind' }, 'YouTube') : null),
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
  const running = jobs.filter(isRunning), waiting = jobs.filter(j => j.state === 'queued'), review = jobs.filter(j => j.state === 'review');
  const finished = jobs.filter(j => ['failed', 'done'].includes(j.state)).reverse();
  const part = text => el('h3', { class: 'eyebrow part-title' }, text);
  $('clearDone').hidden = !jobs.some(j => j.state === 'done');
  setCount('queue', running.length + waiting.length + review.length);
  if (!jobs.length) {
    return $('jobs').replaceChildren(el('div', { class: 'queue-empty' },
      el('div', { class: 'empty-icon' }, glyph('download', 'icon')),
      el('div', { class: 'h3' }, 'Nothing queued yet'),
      el('p', { class: 'small muted' }, 'Open an album, pick a download, and it waits here while it downloads, gets checked and filed.')));
  }
  $('jobs').replaceChildren(
    ...(review.length ? [part('Waiting for you'), ...review.map(jobCard)] : []),
    ...(running.length ? [part('Now'), ...running.map(jobCard)] : []),
    ...(waiting.length ? [part('Up next'), ...waiting.map(jobCard)] : []),
    ...(finished.length ? [part('Finished'), ...finished.map(jobCard)] : []));
}

async function refreshJobs() {
  if (dragging) return;
  try {
    const next = (await api('/api/queue')).jobs;
    if (dragging) return;
    // The queue redraws for any change (a download's percent), the rest of the page only when a job
    // comes, goes or changes state, since that's all album cards and buttons show of it.
    const shape = list => JSON.stringify(list.map(j => [j.id, j.album_id, j.state, j.where]));
    const changed = JSON.stringify(next) !== JSON.stringify(jobs), moved = shape(next) !== shape(jobs);
    jobs = next;
    if (changed) renderJobs();
    if (moved) renderAll();
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
  if (shown === 'find') $('viewActions').append(btn('Album from YouTube', () => openFree(), 'ghost', { title: "Split one YouTube video of a whole album that MusicBrainz doesn't have" }));
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
  if (skipPop) { skipPop = false; return; }
  if ($('detail').open) {                       // Back with an album open closes it and stays put
    albumEntry = false;
    $('detail').close();
    return;
  }
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
        el('div', { class: 'small muted' }, `You have ${have} of ${plural(chosen.length, 'album')} here`),
        artist.plex_url ? el('div', { class: 'row actions' }, plexLink(artist.plex_url, artist.name)) : null)),
    el('div', { class: 'row-between toolbar' },
      el('div', { class: 'row' },
        el('div', { class: 'segmented seg-text', role: 'radiogroup', 'aria-label': 'Show' }, filter('Studio albums', true), filter('Everything', false)),
        want),
      el('div', { class: 'row' }, ...freshness(page.saved_at, () => openArtistPage(page.id, true, page.best, { push: false })))),
    shown.length ? el('div', { class: 'album-grid' }, ...shown.map(albumCard))
      : emptyState(chosen.length ? 'You have all of these' : 'No albums here', chosen.length ? 'Untick Hide albums I have to see them.' : 'Try Everything to see singles, EPs and live albums.', chosen.length ? 'check-circle' : 'music')];
  sections.push(el('h2', { class: 'h2 section-title' }, 'Videos'), videoBlock(), singleBlock());
  if (page.others.length) {
    sections.push(el('h2', { class: 'h2 section-title' }, 'Also in your library'),
      el('div', { class: 'album-grid' }, ...page.others.map(album => el('article', { class: 'card album' }, cover(album.cover),
        el('div', { class: 'album-body' }, el('h3', { class: 'h3 album-title' }, album.title), el('div', { class: 'small muted' }, album.year || ''),
          album.plex_url ? el('div', { class: 'row actions' }, plexLink(album.plex_url, album.title)) : null)))));
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

// One music video from YouTube, for a song MusicBrainz doesn't list yet (a single out this week).
// The block keeps its node while someone is typing in it, so a queue update doesn't take the cursor away.
function singleBlock() {
  if (!single || single.id !== page.id) single = { id: page.id, q: '', song: '' };
  if (singleNode && singleNode.dataset.artist === page.id && singleNode.contains(document.activeElement)) return singleNode;
  singleNode = buildSingle();
  return singleNode;
}

function redrawSingle() {
  const old = singleNode;
  singleNode = buildSingle();
  if (old && old.isConnected) old.replaceWith(singleNode);
}

function buildSingle() {
  const head = el('div', { class: 'single-head' },
    el('h3', { class: 'h3' }, 'From YouTube'),
    el('p', { class: 'small muted' }, "For a song that isn't listed yet, like a new single. Paste a link or type the song's name, then file its video, the song, or both."));
  const parts = [head];
  if (single.picked) parts.push(pickedSingle(single.picked));
  else {
    const input = el('input', { class: 'input', type: 'search', id: 'singleQuery', placeholder: 'Song name or YouTube link', value: single.q,
      disabled: single.loading || undefined, 'aria-label': 'Song name or YouTube link', enterkeyhint: 'search' });
    input.oninput = () => { single.q = input.value; };
    input.onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); findSingle(); } };
    const find = btn('Find', () => findSingle(), 'ghost', { disabled: single.loading || undefined });
    find.prepend(glyph('search'));
    parts.push(el('div', { class: 'single-search' }, input, find));
    if (single.loading) parts.push(el('div', { class: 'small muted' }, 'Looking on YouTube…'));
    if (single.error) parts.push(el('div', { class: 'why' }, `${single.error}.`.replace(/\.\.$/, '.')));
    if (single.results) {
      parts.push(single.results.length
        ? el('div', { class: 'yt-choices' }, el('span', { class: 'eyebrow' }, 'Pick one'),
          el('ul', { class: 'divide' }, ...single.results.map(video => el('li', {},
            el('button', { type: 'button', class: 'yt-choice', onclick: () => pickSingle(video) }, videoCard(video, false))))))
        : el('div', { class: 'small muted' }, 'Nothing found on YouTube. Try other words, or paste the link.'));
    }
  }
  return el('div', { class: 'card card-pad single-card', 'data-artist': page.id }, ...parts);
}

function pickedSingle(video) {
  const job = activeJob(`video-yt-${video.id}`);
  const name = el('input', { class: 'input', id: 'singleSong', value: single.song, maxlength: 120, autocomplete: 'off' });
  const song = () => single.song.trim() || 'Song';
  const year = video.year || new Date().getFullYear();
  const hint = el('div', { class: 'hint' });
  const videoHint = el('span', { class: 'hint', style: 'display:block' });
  const audioHint = el('span', { class: 'hint', style: 'display:block' });
  const ready = () => single.song.trim() && (single.wantVideo || single.wantAudio);
  const sync = () => {
    hint.textContent = "It's what Plex shows, so check it.";
    videoHint.textContent = video.audio_only ? 'This upload is the song over a still picture, so it makes a poor video.'
      : `Filed as ${page.artist.name}/${song()}-video.mp4, in the artist's Extras.`;
    audioHint.textContent = `Filed as ${page.artist.name}/${song()} (${year}), one track as YouTube has it (AAC). Not checked against MusicBrainz.`;
    if (!job) get.disabled = !ready();
  };
  const tick = (key, label, help) => {
    const box = el('input', { type: 'checkbox', checked: single[key] || undefined });
    box.onchange = () => { single[key] = box.checked; sync(); };
    return el('label', { class: 'check' }, box, el('span', {}, label, help));
  };
  name.oninput = () => { single.song = name.value; sync(); };
  name.onkeydown = event => { if (event.key === 'Enter' && ready()) { event.preventDefault(); getSingle(get); } };
  const get = job ? btn(job.state === 'done' ? 'Done' : 'In the queue', null, 'primary', { disabled: true })
    : btn('Get', event => getSingle(event.currentTarget), 'primary');
  const back = btn('Pick another', () => { single.picked = null; redrawSingle(); });
  sync();
  return el('div', { class: 'single-picked' },
    videoCard(video, true),
    el('div', { class: 'single-field' },
      el('label', { class: 'label', for: 'singleSong' }, 'Song'),
      name, hint),
    el('div', { class: 'single-ticks' },
      tick('wantVideo', 'The music video', videoHint),
      tick('wantAudio', 'The song, as a single', audioHint)),
    el('div', { class: 'row actions' }, get, back));
}

async function findSingle() {
  const q = single.q.trim(), opened = single;
  if (!q) return;
  Object.assign(opened, { loading: true, error: null, results: null });
  redrawSingle();
  try {
    opened.results = (await api(`/api/artist/${opened.id}/youtube`, { method: 'POST', body: JSON.stringify({ q }) })).results;
    // A pasted link is the video wanted, so there's nothing to choose between.
    if (opened.results.length === 1 && /[/.]/.test(q)) pickSingle(opened.results[0], false);
  } catch (error) {
    opened.error = error.message;
  }
  opened.loading = false;
  if (single === opened) redrawSingle();
  const box = $('singleSong') || $('singleQuery');
  if (box && single === opened) box.focus();
}

function pickSingle(video, redraw = true) {
  Object.assign(single, { picked: video, song: video.song || '', wantVideo: !video.audio_only, wantAudio: true });
  if (redraw) {
    redrawSingle();
    const box = $('singleSong');
    if (box) box.focus();
  }
}

async function getSingle(button) {
  const video = single.picked, song = single.song.trim();
  const added = await busy(button, () => api('/api/videos/youtube', { method: 'POST',
    body: JSON.stringify({ artist_id: page.id, video: video.id, song, video_file: single.wantVideo, audio: single.wantAudio }) }));
  if (added === undefined) return;
  toast(`${song} is in the queue.`);
  Object.assign(single, { q: '', song: '', picked: null, results: null });
  await refreshJobs();
  redrawSingle();
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

// Plex hasn't matched every artist to MusicBrainz. For those, Encore finds the artist itself (once,
// then it's remembered) and only falls back to a name search when it can't tell who it is.
async function openPlexArtist(artist, button) {
  if (artist.id) return openArtistPage(artist.id);
  let id = null;
  await busy(button, async () => { id = (await api(`/api/plex/artist/${artist.rating}/match`)).id; });
  if (!id) return searchFor(artist.name);
  for (const each of shelf ? shelf.artists : []) if (each.rating === artist.rating) each.id = id;
  artist.id = id;
  openArtistPage(id);
}

function libraryTile(artist) {
  const open = event => openPlexArtist(artist, event.currentTarget);
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
