// Encore's Settings page: connections, folders and the password. Saved keys never come back to the page;
// a blank key field keeps the saved one (Design Uniformity 7.2).
import { $, api, busy, el, refreshHealth, setCount, toast } from './shell.js';

let rows = [], inputs = {};

function field(label, attrs, hint) {
  const input = el('input', { class: 'input', autocomplete: 'off', ...attrs });
  const box = el('label', { class: 'field' }, el('span', { class: 'label' }, label), input, hint ? el('span', { class: 'hint', style: 'display:block' }, hint) : null);
  return [box, input];
}

const keyField = (label, hasKey) => field(label, { type: 'password', placeholder: hasKey ? 'Saved – leave blank to keep' : 'Paste the API key' });

function indexerRow(indexer) {
  const [nameBox, name] = field('Name', { value: indexer.name });
  const [urlBox, url] = field('Address', { value: indexer.url, placeholder: 'https://' });
  const [keyBox, key] = keyField('API key', indexer.has_key);
  const row = { name, url, key };
  rows.push(row);
  const remove = el('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => { rows.splice(rows.indexOf(row), 1); node.remove(); } }, 'Remove');
  const node = el('div', { class: 'well indexer' }, nameBox, urlBox, keyBox, el('div', { class: 'row', style: 'margin-top:12px' }, remove));
  return node;
}

function report(results) {
  $('report').replaceChildren(...results.map(r => el('li', { class: r.ok ? 'fine' : 'bad' }, r.text)));
}

async function save() {
  await api('/api/settings', { method: 'POST', body: JSON.stringify({
    indexers: rows.map(r => ({ name: r.name.value, url: r.url.value, key: r.key.value })),
    sab: { url: inputs.sabUrl.value, key: inputs.sabKey.value }, plex: { url: inputs.plexUrl.value },
    folders: { music: inputs.music.value, imports: inputs.imports.value, downloads: inputs.downloads.value } }) });
  refreshHealth();
}

async function load() {
  const current = await api('/api/settings');
  rows = [];
  $('indexers').replaceChildren(...current.indexers.map(indexerRow));
  const [sabUrlBox, sabUrl] = field('Address', { value: current.sab.url, placeholder: 'http://sabnzbd:8080' });
  const [sabKeyBox, sabKey] = keyField('API key', current.sab.has_key);
  const [plexBox, plexUrl] = field('Address', { value: current.plex.url, placeholder: 'http://plex:32400' });
  const [musicBox, music] = field('Music library', { value: current.folders.music, placeholder: '/mnt/media/Music' }, 'Albums Encore can file cleanly go here, as Artist/Album (Year).');
  const [importBox, imports] = field('Import folder', { value: current.folders.imports, placeholder: '/mnt/media/MusicImport' }, 'For albums that need you to file them: overlaps with what you own, or tags that need a look.');
  const [downloadBox, downloads] = field('Downloads', { value: current.folders.downloads, placeholder: '/mnt/media/Downloads/Encore' }, 'SABnzbd must see this same path.');
  inputs = { sabUrl, sabKey, plexUrl, music, imports, downloads };
  $('sab').replaceChildren(sabUrlBox, sabKeyBox);
  $('plex').replaceChildren(plexBox);
  $('folders').replaceChildren(musicBox, importBox, downloadBox);
  renderPassword(current.password);
}

$('addIndexer').onclick = () => $('indexers').append(indexerRow({ name: '', url: 'https://', has_key: false }));

$('connections').onsubmit = event => {
  event.preventDefault();
  busy($('save'), async () => {
    await save();
    $('report').replaceChildren();
    toast('Saved.');
    await load();
  });
};

$('saveTest').onclick = event => busy(event.currentTarget, async () => {
  await save();
  $('report').replaceChildren(el('li', { class: 'muted' }, 'Testing…'));
  const { results } = await api('/api/settings/test', { method: 'POST' });
  report(results.map(r => ({ ok: r.ok, text: `${r.name}: ${r.text}` })));
  await load();
});

function renderPassword(on) {
  const [currentBox, current] = field('Current password', { type: 'password', autocomplete: 'current-password' });
  const [newBox, fresh] = field(on ? 'New password' : 'Password', { type: 'password', autocomplete: 'new-password' }, 'At least 8 characters.');
  const send = (button, next, done) => busy(button, async () => {
    await api('/api/password', { method: 'POST', body: JSON.stringify({ current: current.value, new: next }) });
    toast(done);
    if (!next) location.reload();
    else await load();
  });
  if (!on) {
    $('passwordBody').replaceChildren(
      el('p', { class: 'hint card-intro' }, 'Encore is open to anyone who can reach it. Set a password to show a sign-in screen first.'),
      newBox,
      el('div', { class: 'row', style: 'margin-top:16px' },
        el('button', { type: 'button', class: 'btn btn-primary btn-sm', onclick: event => send(event.currentTarget, fresh.value, 'Password is on.') }, 'Turn on password')));
    return;
  }
  const remove = el('button', { type: 'button', class: 'btn btn-danger btn-sm' }, 'Remove password');
  remove.onclick = async () => {
    if (!(await window.duConfirm(remove, 'Remove the password? Anyone who can reach Encore, including from outside your network, could use it.', 'Remove password'))) return;
    send(remove, '', 'Password is off.');
  };
  $('passwordBody').replaceChildren(
    el('p', { class: 'hint card-intro' }, 'A password is on. Signing in lasts 30 days on each device. Changing or removing it needs the current one.'),
    currentBox, newBox,
    el('div', { class: 'row', style: 'margin-top:16px' },
      el('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: event => { if (fresh.value) send(event.currentTarget, fresh.value, 'Password changed.'); else toast('Type the new password first.', 'error'); } }, 'Change password'),
      remove));
}

async function loadYoutube() {
  const { version } = await api('/api/youtube');
  const update = el('button', { type: 'button', class: 'btn btn-ghost btn-sm' }, 'Update yt-dlp');
  update.onclick = () => busy(update, async () => {
    update.textContent = 'Updating…';
    try {
      const result = await api('/api/youtube/update', { method: 'POST' });
      toast(result.version === version ? 'yt-dlp is already up to date.' : `yt-dlp updated to ${result.version}.`);
      await loadYoutube();
    } finally { update.textContent = 'Update yt-dlp'; }
  });
  $('youtubeBody').replaceChildren(
    el('span', { class: 'small muted tabular' }, version ? `yt-dlp ${version}` : "yt-dlp isn't installed. Run scripts/update.sh, or update it here."), update);
}

load().catch(error => toast(error.message, 'error'));
loadYoutube().catch(error => toast(error.message, 'error'));
refreshHealth();
api('/api/queue').then(({ jobs }) => setCount('queue', jobs.filter(j => ['queued', 'downloading', 'verifying', 'filing', 'scanning'].includes(j.state)).length)).catch(() => {});
