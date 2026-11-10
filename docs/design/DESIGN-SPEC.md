# Design Uniformity spec

The shared look and feel for Marc's self-hosted web apps, taken from Binge Stats (v0.1, November 2026). Every new app follows this spec. Existing apps move to it when they're next given UI work.

The goal is that any two apps look like one family. They share:
- the same neutrals;
- the same status colours;
- the same glyphs;
- the same layout shell;
- the same way of writing.

The accent colour says which category an app belongs to (section 3.3). The app icon and name tell apps apart within a category.

> **For Claude Code:** read this file before building or changing any UI. Use the files in this folder rather than re-creating them. Section 17 is the checklist.

---

## 1. Principles

1. **Quiet chrome, loud data.** Neutrals do the structure, and colour is saved for meaning: the accent says "this is the app / this is active", and status colours say "OK / in progress / look at this / broken". Never use colour for decoration.
2. **Dark first, light equal.** Dark is the default. Light is a first-class theme, not an afterthought, and every screen is checked in both.
3. **One way to do each thing.** One button style per role, one badge per status, one empty-state pattern. Pages build from the shared components. Don't create page-specific one-offs.
4. **Phone width is a real target.** Everything works at 375px, with no sideways scroll on the page itself.
5. **Calm motion.** Hovers fade in 150ms and nothing bounces. Live data updates in place without flashing.
6. **Plain words.** UK English, sentence case, and say what happened. See section 15.

## 2. What's in this folder

| Path | What it is | Use it when |
|---|---|---|
| `DESIGN-SPEC.md` | This document | Always |
| `PWA-ICONS.md` | Home screen icons, `manifest.json` and full-screen behaviour on phones | Every app, with section 10 |
| `preview.html` | Every token, component and glyph on one page, with switches for theme and accent | Open it in a browser to see the system or check a change. Click a glyph to copy its SVG. |
| `css/uniform.css` | The whole system as plain CSS: tokens, base, layout, components | Apps with no build step (e.g. Vital Signs: Flask + vanilla JS) |
| `css/tailwind.preset.js` + `css/tailwind-components.css` | The same system for Tailwind (standalone CLI, no Node) | Apps built like Binge Stats |
| `css/accents.css` | The accent families, one per category (section 3.3) | Any app that isn't in Media |
| `glyphs/svg/*.svg` | One file per glyph | Copying one in, or for docs |
| `glyphs/icons.svg` | All glyphs as a sprite: `<svg class="icon"><use href="/static/icons.svg#home"/></svg>` | Vanilla apps |
| `glyphs/glyphs.json` | Name → SVG body, grouped | Tooling, and the preview |
| `templates/macros/icons.html` | Jinja `icon(name, cls)` macro, the same call as Binge Stats | Jinja apps |
| `templates/macros/ui.html` | Jinja components: page_header, stat_tile, empty_state, tabs, badge, progress, banner, kebab, confirm_button, field | Jinja apps |
| `templates/base.html` + `partials/` | The app shell: sidebar, top bar, bottom nav, toasts, theme head and theme switch | Starting a new app |
| `js/icons.js`, `js/toast.js`, `js/chart-palette.js` | Glyph strings, toasts, and chart colours for vanilla JS | Apps without Jinja, Alpine or htmx |
| `brand/` | Each app's icon (Binge Stats, The Back Room, Encore, PodBridge, claude-ops, Muddy Paws, Docket), the app-icon template and a teal example (Vital Signs uses it) | Making an app icon |
| `tools/du_sync.py` | The updater: runs on each app's host and keeps its copy of these files on the current release (section 18) | Installing an app's updates |
| `tools/du_flask.py` + `templates/partials/design_card.html` + `js/design-card.js` | The Settings card for the updater (7.14) | Every Flask app |
| `tools/release.py` | Makes a release and promotes it to `stable` | Publishing a change |
| `tools/check_du_sync.py` | Checks for the updater against fake releases (Linux, as root) | After changing the updater |

The plain CSS and the Tailwind version use **the same class names**. Markup written for one works with the other.

## 3. Colour

All colours are CSS custom properties holding space-separated **R G B** triples, so any of them can take an alpha: `rgb(var(--accent) / 0.15)`, or `bg-accent/15` in Tailwind. The theme is set by `<html data-theme="dark|light">`.

### 3.1 Tokens

| Token | Dark | Light | Used for |
|---|---|---|---|
| `--bg` | `#090b11` | `#f5f6fa` | Page background |
| `--surface` | `#11141d` | `#ffffff` | Cards, sidebar, menus |
| `--surface-2` | `#191d29` | `#f0f2f7` | Inputs, hovers, wells, image placeholders |
| `--border` | `#272c3b` | `#dee2eb` | Every hairline (1px) |
| `--text` | `#e8ebf2` | `#141721` | Body text, values |
| `--muted` | `#929aaf` | `#5f677d` | Secondary text, labels, idle nav, table headers |
| `--faint` | `#5c6378` | `#969daf` | Hints, placeholders, idle icons, counts in brackets |
| `--accent` | `#8b5cf6` | `#7c3aed` | Primary buttons, active nav, focus rings, links on hover |
| `--accent-2` | `#ec4899` | `#db2777` | Gradient partner only (never on its own) |
| `--on-accent` | `#ffffff` | `#ffffff` | Text on a filled accent button |
| `--success` | `#34d399` | `#059669` | Done, online, running, OK |
| `--info` | `#38bdf8` | `#0284c7` | In progress, neutral information, "streaming" |
| `--warning` | `#fb923c` | `#ea580c` | Degraded, needs a look, paused |
| `--danger` | `#f87171` | `#dc2626` | Failed, offline, destructive actions |

In Binge Stats these status colours are called `play`, `stream` and `transcode`. The Tailwind preset keeps those as aliases, so Binge doesn't need to change.

### 3.2 How alpha is used

Keep to these steps so tints look the same everywhere:

| Alpha | For |
|---|---|
| `/10` | Banner background |
| `/15` | Badge and stat-icon tint, active nav background |
| `/20` | Active tab or segment, a toast's error background, the primary button's glow |
| `/25` | Badge inner ring |
| `/30` | Banner border, input focus ring |
| `/60` | Surface glass (sidebar), focus-visible ring on buttons, ghost button fill |
| `/90` | Primary button hover, bottom nav glass |

### 3.3 Accent families: one per category

The accent says what kind of app this is. Apps are grouped into the same five categories as the Home Links page, and every app in a category uses that category's family. Apps in the same category are told apart by their icon and name (section 10), never by a different accent.

Each app only overrides `--accent`, `--accent-2` and `--on-accent`. Neutrals and status colours **never** change per app.

| Category | Family | Class | Dark | Light | Button text |
|---|---|---|---|---|---|
| Media | Violet → pink (default) | none | `#8b5cf6` → `#ec4899` | `#7c3aed` → `#db2777` | white (4.2 / 5.7) |
| Downloads | Blue → indigo | `accent-blue` | `#3b82f6` → `#6366f1` | `#2563eb` → `#4f46e5` | white (3.7 / 5.2) |
| Infrastructure | Graphite (slate) | `accent-graphite` | `#cbd5e1` → `#64748b` | `#334155` → `#64748b` | **dark** in dark theme (13.2), white in light (10.4) |
| Home | Teal → cyan | `accent-teal` | `#14b8a6` → `#06b6d4` | `#0f766e` → `#0e7490` | **dark** in dark theme (7.9), white in light (5.5) |
| Files and tools | Fuchsia → purple | `accent-fuchsia` | `#d946ef` → `#a855f7` | `#a21caf` → `#7e22ce` | **dark** in dark theme (5.7), white in light (6.3) |

The numbers in brackets are contrast ratios for the button text, checked in each theme. To apply a family, load `css/accents.css` and put its class on `<html>`.

**Which category an app is in:** pick by what you do there, not what it touches. An app that fetches media is Downloads; one you watch, browse or read stats in is Media.

| Category | Custom apps |
|---|---|
| Media | Binge Stats, The Back Room, PodBridge, Pinnarr |
| Downloads | Encore, iPlayer Downloader |
| Infrastructure | claude-ops |
| Home | HomeForm, Vital Signs |
| Files and tools | Patchbay, Aviary, Quiz |

Vital Signs is Home: it's a screen and device app, already built teal. It moves from Media to Home on the Home Links page.

**Notes on the two quieter families:**
- **Graphite** sits close to `--text`, so its active nav and selected states read through the tint background and the accent glyph rather than the hue. Check active versus hover in both themes.
- **Fuchsia** is 34° of hue from Media's violet. They're distinct as buttons, but check them side by side in `preview.html` before using one next to the other.

**Don't use** green, amber, orange, red or sky as an accent. They would be read as success, warning, danger or info. That's why the Home Links colours for Media (amber), Infrastructure (orange) and Home (green) change to the families above. If a new family is ever needed:
- check white or dark button text reaches at least 3.5:1, and 4.5:1 is better;
- check the accent shows against `--surface` in both themes (at least 3:1);
- check it's at least 30° of hue from every other family and status colour;
- add it to `accents.css`, `preview.html` and this table.

### 3.4 Gradient

The brand gradient runs `accent → accent-2`. Use it in exactly these places:
- the second word of the app name;
- progress fills;
- the avatar fallback;
- the app icon.

Don't put it on buttons, backgrounds or headings.

## 4. Typography

- **Font stack:** `Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`. Inter isn't bundled; it's used if it's installed, and Segoe UI or San Francisco otherwise. To make it exact everywhere, self-host the Inter woff2 as a pinned asset. Never load it from Google Fonts at runtime.
- **Weights:** only 400 (body), 500 (labels, buttons, nav, emphasis) and 600 (titles, values). No bold (700) and no light weights.
- **Numbers:** anything that updates or lines up uses tabular figures (`.tabular` / `tabular-nums`). That covers values, times, sizes, percentages and table columns.

| Role | Size / line | Weight | Extras |
|---|---|---|---|
| Page title (`.h1`) | 24/32 on phones, 30/36 from 640px | 600 | `letter-spacing: -0.025em` |
| Section title (`.h2`) | 16/24 | 600 | |
| Card title (`.h3`) | 14/20 | 600 | |
| Body | 14/20 | 400 | The base size. Pages are 14px, not 16px. |
| Secondary | 14/20 | 400 | `--muted` |
| Label / eyebrow (`.label`, `.eyebrow`) | 12/16 | 500 | Caps, `letter-spacing: 0.025em`, `--muted` |
| Small / hint (`.small`, `.hint`) | 12/16 | 400 | `--faint` for hints |
| Stat value (`.stat-value`) | 20/28 on phones, 30/36 from 640px | 600 | Tabular, no wrap |
| Bottom nav label | 11px | 500 | |
| Fine print (file names under a title) | 11px | 400 | `--faint`, truncated with a title attribute |

## 5. Spacing, shape and depth

- **Spacing:** a 4px grid, mostly 8, 12, 16 and 24.
  - Card padding is 16px on phones and 20px from 640px.
  - Gaps are 12px between cards and tiles (16px on desktop), and 24px between page sections.
- **Radius:** a fixed set.

| Radius | For |
|---|---|
| 8px (`--r-sm`) | Thumbnails, tab pills, segments, drag handles |
| 12px (`--r-md`) | Buttons, inputs, menus, nav links, icon tiles |
| 16px (`--r-lg`) | Cards, banners, empty states, toasts |
| Full | Badges, avatars, progress bars |

- **Borders:** every card and input has a 1px `--border` hairline. A dashed border means "a group of things, not a thing", like Binge's "Other items" rows.
- **Depth:**
  - Shadows only on things that float: menus, toasts, a card being dragged, and the primary button's soft accent glow. Cards themselves have no shadow.
  - Thumbnails get `shadow-poster`.
  - Bars that sit over content (the sidebar, top bar and bottom nav) are glass: `--surface` or `--bg` at 60–90% with an 8px backdrop blur.

## 6. Layout shell

Use `templates/base.html`, which works with either CSS.

| | Phone (< 1024px) | Desktop (≥ 1024px) |
|---|---|---|
| Navigation | Sticky top bar (icon + name, one action on the right), plus a fixed bottom nav | 256px fixed sidebar: icon + name, nav links, Collapse (6.1), then the footer (sign out, theme switch, version). Collapsed, a 72px rail of icons |
| Bottom nav | Up to 5 items. With more, the 5th is **More**, a menu holding the rest (6.2). Respects `safe-area-inset-bottom`. | Hidden |
| Content | 16px gutters (24px from 640px), 112px bottom padding so the nav never covers content | Max 1280px wide, centred, 32px gutters, 40px top padding |

### 6.1 Collapsing the sidebar

On desktop the sidebar can be collapsed to a **72px rail of icons**, giving the page the width back. Every app with a
sidebar offers it.

- **The button:** `partials/sidebar_toggle.html`, in the sidebar just above the footer: « Collapse (the glyph
  `chevron-left`, turned round to point right when collapsed, reading Expand). Load `js/sidebar.js` (plain script,
  `defer`) on every page with the sidebar.
- **Collapsed:** the brand shows its icon only; each nav link shows its glyph only, with its name as the `title`
  tooltip, so **every nav link carries `title="<name>"`**; the footer's Sign out is its glyph; the theme switch stacks
  its three buttons; the version line hides (it's on About).
- **Remembered** per browser (localStorage `sidebar`), and applied before the first paint by
  `partials/theme_head.html`, so a page never draws wide and then snaps narrow.
- **Phones** (under 1024px) have no sidebar, so nothing changes there.
- `js/sidebar.js` fires a window `resize` when it toggles, so charts and self-sizing grids redraw for the new width.
- Tailwind apps (Binge Stats) build their own shell, so they take the same rules into it rather than these classes.

- **Breakpoints:** `sm` 640px (tighter to roomier spacing) and `lg` 1024px (sidebar layout) are the layout breakpoints. `md` (768px) and `xl` (1280px) are allowed only to add grid columns or show an extra table column as space allows. Don't use them to change the shell or a component's look.
- **Page header** (`page_header`): the title and an optional one-line subtitle on the left, with actions on the right. Actions wrap under the title on phones.
- **Tiles:** 2 per row on phones and 4 from 1024px.
- **Order on a page:**
  1. page header;
  2. banners, which are for page-level problems only;
  3. at-a-glance tiles;
  4. the main list or chart;
  5. secondary sections.
- **Sub-pages:** use the `tabs` strip, which scrolls sideways on phones and never wraps.

### 6.2 More in the bottom nav

With more than five nav items, the phone's bottom nav shows the first four and a fifth item, **More**, holding the
rest. `templates/base.html` renders it; an app with its own shell uses the same markup:

```html
<details class="menu-wrap nav-more">
  <summary class="active?"><svg class="icon">…more…</svg>More</summary>
  <div class="menu">
    <a href="…" class="menu-item active?"><svg class="icon icon-sm">…</svg>Label</a>
  </div>
</details>
```

- **Styles are in `uniform.css`** (`.nav-more`): the summary looks like the other bar items (glyph over an 11px
  label, muted, no disclosure triangle, no tap highlight), and turns the accent colour while open or when the current
  page is one of the items inside it (`active` on the summary). The menu opens **upwards**, above the bar, 8px in from
  the right, and its items are rows (15px, 12px × 14px), so it never opens off the bottom of a phone screen.
- **Glyph:** `more`.
- **Closing:** a `<details>` opens and closes on its own; add the small script in `base.html` so a tap outside it, or
  Escape, closes it too.
- Apps don't add their own `.nav-more` rules: take `uniform.css`.

## 7. Components

Everything below is in `uniform.css` and `tailwind-components.css`. The Jinja macros live in `templates/macros/ui.html`. Use these; don't restyle them per page.

### 7.1 Buttons

| Class | When |
|---|---|
| `btn-primary` | The one main action in a view. At most one per card or form. |
| `btn-ghost` | Everything else: secondary actions, "Save and test", links that act like buttons |
| `btn-danger` | Destructive and irreversible actions only, and always with a confirm |
| `btn-icon` | An icon on its own (36px, or 44px on touch screens). Always has an `aria-label` and a `title`. |

- **Labels:** a verb phrase in sentence case ("Add display", "Sync now", "Save"), with no full stop and no "Click here".
- **Icons:** go on the left at 16px, and only when the icon adds recognition. Not every button needs one.
- **Busy state:** a disabled button at 50% while its request runs. Re-enable it when the request finishes.

### 7.2 Forms

- **Layout:** the label goes above (`.label`: caps, 12px, muted), the input full width, the hint below (`.hint`, faint). Use the `field` macro.
- **Secrets:** never show a saved secret. Use a password input with the placeholder "Saved – leave blank to keep", and leave it blank to keep the saved one.
- **Checkboxes:** a checkbox with a sentence label, plus a hint saying exactly what it does and doesn't do. Switches that change anything outside the app are **off by default**.
- **Errors:** come back as a flash or toast with the reason and what to do ("The URL should start with http:// or https://"). Never just "Invalid input".
- **Saving:** settings pages save per card with a **Save** button. Add **Save and test** where a connection can be tested.

### 7.3 Badges

Badges are a small pill for state or category. **Status badges keep the same meaning in every app:**

| Badge | Meaning | Examples |
|---|---|---|
| `badge-success` | Good, done, online | Online, Direct play, Done |
| `badge-info` | In progress, informational | Syncing, Direct stream, a quality like 1080p |
| `badge-warning` | Needs a look, degraded, paused | Slow, Paused, Transcode, Below cutoff |
| `badge-danger` | Broken, offline, failed | Offline, Failed |
| `badge-accent` | New, or belongs to this app | New |
| `badge-muted` | A neutral tag or source | Sonarr, Queued, Normal |

Keep the text to one or two words. Add a dot or glyph only when the badge stands alone.

### 7.4 Cards and rows

- **`.card`:** the basic surface, with a 1px border, 16px radius and no shadow.
- **List rows inside a card:** separated by hairlines (`.divide`, or `divide-y divide-border`), with 12–16px vertical padding.
- **Rows that are cards themselves**, like Binge's download jobs: 12px apart.
- **Group rows** (`.card-dashed`) stand for many hidden things. Show a count, a total and the glyph `stack`.

### 7.5 Stat tiles (`stat_tile`)

- **Contents:** an eyebrow label, a big tabular value, one sub-line (context, never a second number to compare), and an optional glyph in a 15% tint of its tone.
- **Tones:** pick the tone by meaning. `accent` is the default; use success, info, warning or danger only when the number is a status.

### 7.6 Tables

- **Header cells:** caps 12px muted (`.th` / `.table th`).
- **Body cells:** 14px, with hairlines between rows and a faint hover.
- **Width:** always wrap the table in `.scroll-x` (`overflow-x-auto`) so it scrolls inside its card on phones, with `scroll-fade` (7.17) so a cut-off column fades rather than stopping dead.
- **Alignment:** numbers align right and use tabular figures.
- **Missing values:** shown as "–" (an en dash), never "null", "None" or blank.

### 7.7 Tabs and segmented controls

- **`tabs`:** navigation between sub-pages, as links. The active tab has an accent tint at 20%.
- **`segmented`:** a choice of 2–4 options on the same page (theme, period). Use icons or short words, with `role="radiogroup"` and `aria-checked`.

### 7.8 Menus

- **Trigger:** `kebab` (the glyph `more`) opens a `menu` of `menu-item`s, each with a 16px glyph and a verb ("Move to top", "Rename").
- **Destructive items:** go last, with the `danger` class.
- **Closing:** a menu closes on choosing an item and on clicking outside.
- **Live pages:** pause live polling while a menu is open, so the menu isn't wiped out (see 8.3).

### 7.9 Progress

- **Shape:** a 6px track (8px for the main item on a card) with the brand-gradient fill, animated over 1s linear. Live progress glides between updates rather than jumping.
- **Labels:** put the percentage on the right and "done in 22 mins" below it.

### 7.10 Feedback: banners, toasts and flashes

| Kind | For | Placement |
|---|---|---|
| **Banner** | A page-level problem that persists: a service down or a setting missing. Danger or warning. Always says what to check. | Top of `<main>`, above everything |
| **Toast** | The result of an action you just took ("Moved. It's High priority now…", "Can't reach the Pi in the garage") | Bottom centre on phones, above the bottom nav. Bottom right on desktop. Fades out after about 4.5 s. |
| **Flash** | The result of a full-page form post | Top of `<main>`, success or danger tint |

There are no modal alerts, and never `window.confirm()`. A destructive action asks first with the `confirm_button` macro: an inline popover by the button with the question, **Cancel** and a danger button naming the action ("Delete this channel? Nothing more is sent to it." / Cancel / Delete channel). It closes on Cancel, Escape or a click outside. The popover floats over the page, **above the button** (below it only when there's no room above), and follows it as the page scrolls. So a scrolling table or card never clips it, gains a scrollbar or jumps to make room. `js/confirm.js` places it (`duPlace(pop, button)`), for the macro too, so Alpine apps load `js/confirm.js` as well. Use a modal only for a confirm that needs a lot of reading, and keep it rare. Apps without Alpine put `data-confirm="Question?"` on the form (or on one button) and load `js/confirm.js`, which shows the same popover; the danger button repeats the submit button's label unless `data-confirm-yes` says otherwise. In a click handler, `if (!(await duConfirm(button, question, yes))) return;` does the same.

### 7.11 Empty states (`empty_state`)

Every list and every chart has one. It has:
- a 32px glyph in a 64px rounded well;
- a title that says what's missing ("No displays yet");
- one line saying how things get there ("Install the agent on a Pi and it shows up here within a few seconds").

Never show a bare "No data".

### 7.12 Images, thumbnails and avatars

- **Thumbnails:** 2:3 posters, 1:1 squares or 16:9 art, with an 8px radius and a `surface-2` placeholder showing a faint glyph. The image fades in over the placeholder, and if it fails, the placeholder stays.
- **Avatars:** round, with the initial on the brand gradient at 60%, and the photo over it when there is one.
- **Proxying:** images from other services go through the app's own proxy, so tokens never reach the browser.

### 7.13 Drag to reorder

- **Handle:** a `drag-handle` with the glyph `grip`, on the left edge. Only the handle starts a drag, so scrolling on a phone still works (with a 150ms touch delay).
- **Library:** SortableJS, pinned.
- **While dragging:** the dragged item is at 40% opacity (ghost), with an accent ring while chosen.
- **After the drop:** the item stays where it was dropped (`is-busy` at 60% until confirmed). On an error it snaps back and a toast says why.

### 7.14 Design updater card

Every app's Settings page has a **Design system** card (`templates/partials/design_card.html`, driven by `js/design-card.js` and `du_flask.init_design`). It shows:
- the installed version and its date, and a status badge: Up to date, Update available, Waiting, Rolled back, Check failed, or Not checking (no check for three days);
- when it last checked, and the updater's last message in plain words ("v1.3 is out, but … has uncommitted changes");
- an **Automatic updates** checkbox, on by default;
- **Check now**, which installs a new release even with automatic updates off, and **Roll back to v…**, which asks first in an inline popover and turns automatic updates off.

The app never updates itself: the buttons only leave a flag file for the host's updater (section 18). Apps with their own markup can use `design-card.js` with their own classes through `data-badge`.

### 7.15 Sidebar footer, version and About

Every app's sidebar ends the same way, as Binge Stats does, so these sit in exactly the same place everywhere (`templates/partials/sidebar_foot.html`):
1. **Sign out**, a full-width nav link with the `logout` glyph. Always "Sign out", never "Log out". Leave it out only when the app has no login.
2. **The theme switch** (13).
3. **The version line**, faint 12px text linking to the About page: `v1.0.0 · b81afd5 · About`. That's the app's own version, then the short SHA of the running checkout.

The footer is 24px below the nav, with a hairline and 16px above Sign out. **Nothing else goes in it.** App-specific controls (The Back Room's index status, Rescan and player choice) sit above it in the sidebar, so the three never move.

**Version:** each app sets `__version__` in its package (`MAJOR.MINOR.PATCH`) and passes it to `du_flask.init_design(..., version=__version__)`. That also gives templates `app_version`, `app_sha()` (read from `.git` on each render, so an update shows without a restart) and `runtime_facts()`.

**About** (`/about`, behind the login, `templates/partials/about.html`), three cards side by side on desktop:
- **The app:** icon, name with the gradient half, `v1.0.0 · b81afd5`, then Python, SQLite and anything the app depends on (Plex server, worker state, MQTT broker).
- **Your data:** counts and sizes, then a hint line saying where it all lives.
- **Built with:** the stack and credits, with links, plus any small print ("Not affiliated with Plex Inc.").

A **GitHub** button sits in the page header when the repo is public or the user can reach it.

### 7.16 Maps

From Muddy Paws (`paws/static/gmaps.js`, `map.js`). Maps are Google Maps (the Maps JavaScript API), drawn by one small loader shared by every page with a map.

- **Shape:** a full-bleed map in a card with no padding (`.map-card`, `overflow: hidden`), 60vh tall between 320 and 640px, on `surface-2` while it loads. Anything else about the map (a note, a list of who's on it) sits below it in the same card.
- **Only on pages that have one.** The loader adds Google's script when a page makes its first map, and the server sends that page Google's CSP allowlist (`MAP_CSP` in `paws/web.py`, from Google's CSP guide, applied by `pages.map_page`). Every other page keeps the app's usual CSP, which loads nothing from anywhere. A page whose map has no key yet is sent without it.
- **Keys:** the element carries `data-key` (the browser key, restricted on Google's side to the Maps JavaScript API and the app's hostname) and `data-map-id`. Any other Google key stays on the server (`APP-STANDARD.md` 11).
- **Theme:** `colorScheme` is `DARK` or `LIGHT` from `data-theme` when the map is created, with `backgroundColor` set to `surface-2`. Google only reads it at creation, which is fine because the theme switch reloads the page (13).
- **Controls:** no Street View, map type or clickable points of interest; full screen on; `gestureHandling: "greedy"` so one finger pans on a phone.
- **Markers:** each person, dog or device is drawn in its chart palette slot (11), so it's the same colour on the map as on every chart. A person is a 32px round marker with their initial in white (36px for you); a dog has 10px corners instead. Both have a 2px white ring and a soft shadow, which suit both themes. A faint circle in the same colour (12% fill, 50% stroke) shows a fix's accuracy. Markers glide to a new position over 1s (12), or jump with reduced motion.
- **Map unavailable:** beside the map is a hidden `[data-map-failed]` empty state (7.11) with the glyph `alert`, titled "Map unavailable", and one line saying what to check ("Google Maps didn't load. Check the connection, or that the browser key allows this address."). It replaces the map when the script fails to load, and when Google refuses the key afterwards (Google calls `window.gm_authFailure`). Anything that works without the map (a list, a form) stays.
- **No key yet:** an empty state in place of the card ("No map yet"), telling an admin where to add the keys and anyone else that an admin does it.
- **A sketch where only the shape matters.** Lists of routes don't load a map for each card: `geo.sketch` turns the line into an SVG path (a 240 × 120 viewBox, 2:1), drawn as a 3px round-capped `--accent` stroke on `surface-2`. It costs no map loads and works without Google.

### 7.17 Scroll fade

From Binge Stats (its dashboard's "Airing this week" row). A row that scrolls sideways never cuts a card off hard at its edge: the edge fades out, so it reads as "there's more this way" rather than a mistake.

- **Where:** every sideways scroller that can hide content past an edge: card rows (day cards, posters, sessions), tab strips (6), and wide tables in `.scroll-x` when they're wider than their card on a phone. Add the class `scroll-fade` beside whatever makes it scroll (`.scroll-x`, or `overflow-x-auto` in Tailwind).
- **What it does:** a 48px mask gradient on the side that has more to scroll. At the start only the right edge fades; part way, both; at the end the right fade goes and the last item shows in full. It eases in and out over 0.2s (none with reduced motion).
- **How:** `js/scroll-fade.js` sets `data-fade-l` and `data-fade-r` from the scroll position, on load, scroll and resize, after an htmx swap and when a row's size changes. A page that changes a row some other way calls `duScrollFade()`. Without the script nothing fades, which is the old look, not a broken one.
- **Not** a gradient overlay in a colour: the mask fades the content itself, so it works on any background, in both themes, with no extra elements.

## 8. States and live data

### 8.1 Interaction states

| State | Look |
|---|---|
| Hover | `surface-2` background or one step brighter text, over 150ms |
| Focus | Keyboard focus only (`:focus-visible`): a 2px accent ring at 60%. Inputs: an accent border at 70% plus a 2px ring at 30%. |
| Active / selected | Accent tint (15–20%), with the glyph in the accent colour |
| Disabled | 50% opacity, no pointer |
| Busy | 60% opacity, no pointer (`is-busy`) |

### 8.2 Loading

- **First load:** render from the server, so pages aren't empty while they load.
- **Waits over about a second:** a one-line "Reading … for the first time…" in a card. Use the glyph `buffer` spinning only for something actively being worked on.
- **No skeleton screens.**

### 8.3 Live pages

- **Updating:** htmx `hx-trigger="every 2s [!document.hidden]"`, or 3s for heavier pages, swapping the inner HTML of one container.
- **Pausing:** stop polling while the tab is hidden, and while the user is dragging or has a menu open.
- **Optimistic moves:** show the change at once, then poll faster (every 0.5s) until the server's read confirms it. Ignore older snapshots in the meantime, so nothing jumps back.
- **Stale data:** say so: "Last read 3m ago: is the worker running?" in the warning colour.

## 9. Glyphs

There are 73 glyphs in `glyphs/`. Binge Stats' set is the core; the rest were added for other apps in the same hand.

### 9.1 Drawing rules: every glyph, no exceptions

- 24 × 24 viewBox, drawn inside about 2–22.
- **Stroke 1.8**, `stroke-linecap="round"`, `stroke-linejoin="round"`, `fill="none"`, `stroke="currentColor"`.
- Filled shapes only for:
  - play and pause (solid), which read better filled;
  - dots of about 1.2–1.3 radius (grip, more);
  - brand marks.
- Corners on rectangles: rx 2 (rx 1.5 for small squares).
- One idea per glyph, with no text, no gradients and no second colour.
- Colour comes from `currentColor`:
  - `--muted` when idle;
  - `--text` on hover;
  - `--accent` when active;
  - a status colour only inside a matching tint (a stat icon, a banner).

### 9.2 Sizes

| Size | Where |
|---|---|
| 16px | Inside buttons, menu items, badges, the theme switch |
| 20px | Nav, list rows, stat tiles, banners (the default) |
| 32px | Empty states |
| 24px | Glyph previews only |

### 9.3 The set

| Group | Glyphs |
|---|---|
| Navigation | `home` `grid` `activity` `history` `chart` `bulb` `users` `user` `stack` `bell` `settings` `menu` `more` `logout` |
| Actions | `plus` `pencil` `trash` `x` `check` `search` `filter` `refresh` `upload` `download` `send` `copy` `link` `external` `eye` `lock` `power` `grip` `to-top` `to-bottom` `star` `tag` |
| Direction | `chevron-up` `chevron-down` `chevron-left` `chevron-right` |
| Status | `info` `alert` `check-circle` `bolt` `buffer` `clock` `calendar` |
| Media and playback | `play` `pause` `stop` `tv` `film` `image` `music` |
| Devices and network | `device` `monitor` `server` `chip` `lan` `wan` `headset` |
| Maps and outdoors | `map` `route` `pin` `paw` |
| Weather | `sun` `cloud` `rain` `wind` `thermometer` |
| Theme | `sun` `moon` `monitor` |
| App-specific, don't reuse | `plex` (the Plex mark), `home-assistant` |

### 9.4 Fixed meanings

Use the same glyph for the same idea in every app:

| Idea | Glyph | Idea | Glyph |
|---|---|---|---|
| Home / overview | `home` | Settings | `settings` |
| Dashboard of many things | `grid` | Live now / activity | `activity` |
| Past events / log | `history` | Stats / graphs | `chart` |
| Insights / ideas | `bulb` | People | `users` / `user` |
| Libraries, groups, "other items", More | `stack` | Notifications / alerts setup | `bell` |
| A row's own actions | `more` (⋮) | Mobile menu | `menu` (☰) |
| Add | `plus` | Edit | `pencil` |
| Delete | `trash` | Close / remove from list | `x` |
| Sync, re-read, retry | `refresh` | Test-send a notification | `send` |
| Open in another app | `external` | Copy to clipboard | `copy` |
| Problem / warning | `alert` | Done / OK | `check` / `check-circle` |
| Working on it (spins) | `buffer` | Fast, hardware, power-user | `bolt` |
| Screen / display / client device | `monitor` / `device` | Server / host | `server` |
| On the local network | `lan` | Over the internet | `wan` |
| Turn on or off, restart | `power` | Secret / locked setting | `lock` |
| Reorder by dragging | `grip` | Send to top / bottom | `to-top` / `to-bottom` |
| A map view | `map` | A planned or saved route | `route` |
| A place or position | `pin` | Dogs / pets | `paw` |

### 9.5 Adding a glyph

1. Draw it to 9.1. Start from the nearest existing glyph so it matches in weight.
2. Name it in lower kebab-case after the idea, not the picture (`external`, not `arrow-box`).
3. Add it to **all** the outputs:
   - `glyphs/svg/<name>.svg`;
   - the `icons.svg` sprite;
   - `glyphs.json` (in a group);
   - `templates/macros/icons.html`;
   - `js/icons.js`.
4. Add it to the table above, and to 9.4 if it carries a fixed meaning.
5. Check it in `preview.html` in both themes, next to its neighbours.

## 10. App identity

- **App icon:** start from `brand/app-icon-template.svg`.
  - A 64 × 64 tile with a 16px radius and a diagonal gradient in the app's accent family.
  - One white glyph on the inner 36 × 36, filled with rounded corners.
  - Depth from opacity steps (0.7, 0.85, 1), never from extra colours.
  - It's the favicon, the sidebar logo (32px) and the top bar logo (28px).
- **Name:** two words. The first is in `--text` and the second has the brand gradient ("Binge **Stats**"), at semibold with tight tracking.
- **Tab title:** "Page · App name".
- **`theme-color` meta:** matches `--bg` for the current theme.
- **Home screen:** the touch icons, `manifest.json` and full-screen tags are in `PWA-ICONS.md`. They're made from the app icon with a square tile.

## 11. Charts

These are Apache ECharts with one shared theme, from Binge Stats' `static/js/charts.js`. The colours are in `js/chart-palette.js`.

- **Categorical palette:** 8 slots, in a fixed order, validated against the card surfaces in both themes.
  - **Colour follows the entity, not its rank.** Give each series a stable slot from the server, so the same person or device is the same colour on every chart.
  - "Other" is always `OTHER` grey and comes last.
- **Sequential** (heatmaps): one hue, blue, with low values fading toward the surface.
- **Bars:** at most 24px thick, with 4px rounded ends, and a 2px surface-coloured gap between stacked segments.
- **Axes and grid:** solid hairline gridlines in `--border`, axis text in `--muted` at 11–12px, and no axis lines where the gridlines do the job.
- **Legend:** only with two or more series.
- **Tooltip:** every chart has a hover tooltip, using the app's number formats (section 15).
- **Empty state:** every chart has one (7.11), drawn in place of the chart, not as an empty axis.
- **Theme switching:** charts read the tokens at render time, and redraw when the theme flips (the "system" preference reloads the page).

## 12. Motion

| Movement | Timing |
|---|---|
| Hover and colour changes | 150ms `cubic-bezier(0.4, 0, 0.2, 1)` |
| Toasts, menus and popovers fading | 150–300ms opacity only, no slide or scale |
| Progress and live values | 1s linear (or the poll interval), so they glide between updates |
| Drag and reorder | 150ms (SortableJS `animation: 150`) |

- **Spinners:** only `buffer`, only while something is actively working.
- **Reduced motion:** with `prefers-reduced-motion`, everything is instant (`uniform.css` does this).

## 13. Themes

- **Three preferences:** Dark (the default), Light and System. They're stored per user if the app has logins, otherwise in `localStorage` under "theme". The control is the sun / moon / monitor segmented control in the sidebar footer.
- **Setting the theme:** `data-theme` is set before first paint by the inline script in `partials/theme_head.html`, so the wrong theme never flashes. "System" follows the OS and reloads when the OS flips, so charts redraw.
- **Checking both:** every screen is checked in both themes. The usual traps:
  - images or maps that only suit one theme. Binge darkens OpenStreetMap tiles in dark mode with a CSS filter; Google maps take the theme as their colour scheme (7.16).
  - hard-coded `white` or `black`, or any hex. Use tokens. The only exceptions are:
    - text on a filled accent (`--on-accent`) or danger button;
    - the app icon;
    - another service's brand mark used as that service's sign-in button (Binge's Plex-gold button);
    - HTML bars that mirror the chart palette (11);
    - map backgrounds behind themed tiles;
    - the thin white rings round posters and map markers, the white initial on a marker, and the white knob of a switch, which suit both themes;
    - white text and icons on a dark scrim over artwork (a thumbnail's duration label, a ☆ over a poster), since the artwork is the same in both themes.
- **Print, where it matters:** print forces light tokens (Binge's Wrapped).

## 14. Scrollbars

Scrollbars are thin, rounded and quiet until hovered, and they follow the theme. The styles are in both CSS files.
- **Chromium and Safari:** a 12px bar with a 6px thumb inset by a transparent border, at `--faint` 35%, rising to `--muted` on hover and `--accent` while dragging.
- **Firefox:** `scrollbar-width: thin` plus the same colours.
- **Sideways strips** (tabs, carousels): an 8px bar.

## 15. Writing

- **UK English:** colour, organise, centre, licence (the noun). Use £, metric units and dates as 4 Nov 2026.
- **Case:** sentence case everywhere: titles, buttons, menu items, labels. Labels and buttons have no full stop. Hints and messages are full sentences with one.
- **Voice:** plain and direct, and say what happened and what's next.
  - "Saved. The displays pick it up on their next poll."
  - "Can't reach Sonarr: check the URL and API key."
  - Avoid "Oops!", "Successfully", "Please", exclamation marks and blaming the user.
- **Errors** say what failed and where to look. They don't give a stack trace, an HTTP code on its own, or "Something went wrong" (unless that's genuinely all that's known, and then say what to try).
- **Switches** say exactly what they do and don't do: "It only changes the order; nothing is ever deleted or paused."
- **Formats:**

| Thing | Format |
|---|---|
| Counts | 1,284 (en-GB grouping) |
| Sizes | 560 MB, 1.4 GB, 1.8 TB. Binary units, labelled MB/GB/TB as people expect. One decimal from GB up. |
| Speeds | 108.4 MB/s, with Mbit/s small beside it where bandwidth matters |
| Durations (ETA) | "45 secs", "22 mins", "1h 02m", "6h 22m", "1d 6h" |
| Elapsed / watch time | "59s", "20m 00s", "3h 20m", "1d 17h"; in tight columns "41h 40m" |
| Relative time | "just now", "3m ago", "2h ago", "1d ago". Past a week, show the date. |
| Dates | 4 Nov 2026. Day names only within the week ("Tuesday"). |
| Times | 24-hour, 21:04, in the user's time zone (stored in UTC). |
| Percentages | Whole numbers (62%), with one decimal only when it matters (99.8% uptime) |
| Missing | – (an en dash) |

## 16. Accessibility and phones

- **Touch targets:** at least 44px on touch devices. `btn-icon` grows on `pointer: coarse`, and the bottom nav items are full height.
- **Contrast:**
  - body and muted text pass AA in both themes;
  - `--faint` is only for hints, placeholders and decoration, never essential text;
  - accent button text is checked per family (3.3).
- **Labels:** every icon-only control has `aria-label` and `title`, and decorative glyphs have `aria-hidden="true"` (the macros do this).
- **Colour is never the only signal:** a badge always has a word, and a status dot has a label.
- **Width:**
  - no horizontal scroll at 375px;
  - tables and tab strips scroll inside their own box;
  - long names truncate with `title` holding the full text.
- **Safe areas:** use `viewport-fit=cover`, pad the top bar with `env(safe-area-inset-top)` and the bottom nav with `env(safe-area-inset-bottom)` (`PWA-ICONS.md` 3).
- **Page height on phones:** `uniform.css` sizes pages with `dvh`, plus the status bar as a home-screen app; a full-height page uses `html.full-height` and `js/fit-screen.js`. Never `overflow: hidden` on `html` or `body` (`PWA-ICONS.md` 3.1).

## 17. Building a new app: checklist

1. **Shell:** copy `templates/base.html` and `partials/`. Set the app name parts, the nav (at most 5 for phones, or use More) and the accent family for the app's category (3.3).
2. **CSS:** plain apps link `css/uniform.css` (+ `accents.css`). Tailwind apps use the preset and start `input.css` with `tailwind-components.css`. Build with the standalone CLI.
3. **Glyphs:** use `templates/macros/icons.html` (Jinja), or `glyphs/icons.svg` / `js/icons.js` (vanilla). Use the fixed meanings in 9.4.
4. **Icon:** make one from `brand/app-icon-template.svg` in the app's accent family, then the home screen icons and manifest from it (`PWA-ICONS.md`, checklist in its section 8).
5. **Pages:** build only from the components in section 7. Every list and chart gets an empty state, every action gives toast feedback, and every page-level problem gets a banner.
6. **Assets:**
   - Front-end libraries (htmx, Alpine, ECharts, SortableJS) are pinned by version and sha256 and fetched at build time.
   - No CDNs at runtime. **The one exception is Google Maps**, which can only be loaded from Google; it's allowed on pages with a map, and only those pages' CSP lets Google in (7.16).
   - Images from other services go through the app's own proxy.
7. **Check it:**
   - both themes;
   - 375px and 1280px wide;
   - keyboard focus visible;
   - each empty state;
   - an error path, with its toast or banner;
   - the words against section 15.
8. **Updates:** add the Design system card (7.14) to Settings, write the app's `/etc/design-uniformity/<app>.toml` on its host and run `du_sync.py install` (section 18).
9. **Gaps:** if something isn't covered here, build it from the tokens and components, then add it to this spec and to `preview.html`, so the next app gets it too.

## 18. Changing the system

This folder is the source of truth.
- **A change made in one app** goes back here first: tokens, a component or a glyph. The other apps then pick it up.
- **Tokens:** never fork them per app. Only the accent family differs.
- **Versions:** keep a dated line at the top of this file for each change, and note which apps have picked it up.

### 18.1 How changes reach the apps

This folder is the git repo `github.com/marctew/design-uniformity` (public: there's nothing secret in a design system).
- **Releases:** `python tools/release.py 1.1` writes `version.json` and `MANIFEST.sha256`, commits, and tags `v1.1`. Nothing reaches an app yet.
- **Testing a release:** pin one app to it (`channel = "v1.1"` in its config) and press **Check now** there.
- **Going live:** `python tools/release.py promote 1.1` points the `stable` tag at it. GitHub caches tag downloads for a minute or so, so a Check now straight after promoting can still say "Up to date"; try again a minute later. Every app on `stable` picks it up in its nightly check (02:30 plus up to 2 hours, spread out), or straight away with **Check now**.

On each app's host, `du_sync.py` (installed in `/opt/design-uniformity`, run as root by systemd):
1. downloads the release's tarball from GitHub and checks every file against its manifest;
2. stops if the app's repo has uncommitted changes, and says so on the card;
3. backs up the app's current copies, then copies the new files to where the app's config says;
4. **commits and pushes them to the app's own repo**, so the app's `git pull` and builds never fight the change, and its published image gets it in its next release;
5. runs the app's deploy command (its own update script or a restart) and waits for its health URL;
6. if the app doesn't come back healthy, puts the old copies back, commits that, and deploys again.

Each app has `/etc/design-uniformity/<app>.toml` on its host:

```toml
app = "The Back Room"
repo = "/opt/hmm-browser"              # the app's git working copy
state_dir = "/var/lib/hmm-browser/design"  # status.json, prefs.json and the Check now flags
channel = "stable"                     # or a tag like "v1.1" to pin
deploy = "scripts/update.sh --if-idle" # optional; the app's own deploy (APP-STANDARD.md 6)
health = "http://127.0.0.1:8765/healthz" # anything below 500 counts as up
# state_owner = "claude"               # the user the app runs as, by name (Docker: whoever has the container's uid)
# push = false                         # commit only

[files]                                # release path = where it goes in the repo; a trailing / copies a folder
"css/uniform.css" = "backroom/static/design/uniform.css"
"js/design-card.js" = "backroom/static/design/design-card.js"
"tools/du_flask.py" = "backroom/du_flask.py"
```

`APP-STANDARD.md` 7 has a standard app's whole config.

`du_sync.py install <config>` writes the units: `du-sync-<app>.timer` (nightly) and `du-sync-<app>.path` (runs as soon as the card leaves a flag). `du_sync.py status <config>` prints where it stands. Design files in an app are never edited there: change them here, release, promote.

**claude-ops** controls the Proxmox host, so it never takes the JavaScript automatically: its copy is updated by hand.

| Date | Change | Apps up to date |
|---|---|---|
| 2026-11-04 | First version, from Binge Stats | Binge Stats (source) |
| 2026-11-04 | Accents are now one per category. Added graphite (Infrastructure) and fuchsia (Files and tools). | Binge Stats (no change needed) |
| 2026-11-04 | Added the updater (`tools/du_sync.py`), the Design system card (7.14) and releases from GitHub (18.1). Added `.row`, `.row-between` and the plain-CSS helpers to the Tailwind entry. | – |
| 2026-11-04 | Added `confirm_button` and the `.confirm` popover (7.10), which replace `window.confirm()`. `md` and `xl` are allowed for grid and table columns only (6). Listed the allowed hard-coded colours (13). | Binge Stats |
| 2026-11-04 | Added `js/confirm.js` (7.10), the inline confirm for plain forms, and the `image` glyph. | Vital Signs |
| 2026-11-04 | The Tailwind entry makes `[hidden]` always win, as uniform.css does, so a hidden banner stays hidden. | Binge Stats |
| 2026-11-04 | The sidebar footer and About page (7.15), from Binge Stats: `partials/sidebar_foot.html`, `partials/about.html`, and `du_flask` now provides `app_version`, `app_sha()` and `runtime_facts()`. | The Back Room, Vital Signs, PodBridge |
| 2026-11-04 | `duConfirm(button, question, yes)` in `js/confirm.js`: the inline confirm for click handlers, in place of `window.confirm()`. The `star`, `tag` and `headset` glyphs, and The Back Room's icon. | The Back Room |
| 2026-11-04 | The Design system card sends a CSRF token (`data-csrf`, as `X-CSRF-Token`) for apps that check one on every POST. White text on a dark scrim over artwork is an allowed hard-coded colour (13). | PodBridge |
| 2026-11-04 | Added `APP-STANDARD.md`: how every app is built, run and deployed, with The Back Room as the reference app. The 18.1 example now uses `/healthz` and `scripts/update.sh`. | The Back Room |
| 2026-11-05 | `APP-STANDARD.md`: every app is done, and claude-ops is recorded as the second exception (FastMCP and Docker, the look inlined by hand, no updater). Encore, PodBridge and claude-ops icons added to `brand/`. | claude-ops |
| 2026-11-05 | The `map`, `route`, `pin` and `paw` glyphs, in a new Maps and outdoors group (9.3, 9.4). The 9.3 table now also lists `star`, `tag`, `image` and `headset`. | – |
| 2026-11-05 | From Muddy Paws: the map component (7.16) and Google Maps as the one stated exception to no CDNs at runtime (17.6). Map markers' white ring and initial are allowed hard-coded colours (13). `APP-STANDARD.md` gains apps with more than one person (9), ingest endpoints with their own tokens (10) and secrets an admin enters in Settings (11). Docs only. | Muddy Paws (source) |
| 2026-11-06 | The outside API's shared code, lifted from Binge Stats' pilot: `tools/du_api.py`, `templates/display/base.html`, the API tokens and Webhooks cards for Settings, and `tools/check_api.py`. `API-STANDARD.md` draft 3. Apps that adopt the API add six lines to their updater config (API-STANDARD 12). | Binge Stats (source) |
| 2026-11-06 | Four weather glyphs (9.3), for Muddy Paws' weather: `cloud`, `rain`, `wind`, `thermometer`, in a new Weather group with `sun`. | Muddy Paws (source) |
| 2026-11-06 | Inline confirms (7.10) float above the button, over the page, instead of opening inside it, so a confirm in a scrolling table no longer clips or scrolls the table. The popover's text wraps even inside a `nowrap` cell. `js/confirm.js` gains `duPlace()`, which the `confirm_button` macro and `design-card.js` use. | Muddy Paws (source) |
| 2026-11-06 | The scroll fade (7.17), from Binge Stats: `scroll-fade` in both CSS files and `js/scroll-fade.js`, so a sideways row fades at the edge where there's more to scroll instead of cutting a card off. | Binge Stats (source) |
| 2026-11-06 | `API-STANDARD.md`: Encore has the API (14), with what it found (20). Through the tunnel, Cloudflare's Browser Integrity Check blocks API readers until the app's hostname has a Configuration Rule turning it off for `/api/v1/` and `/display/` (4); Encore and Binge Stats have one. `tools/check_api.py` looks headers up case-insensitively, since Cloudflare sends them in lower case. | Encore (source) |
| 2026-11-06 | Added `PWA-ICONS.md`: home screen icons, `manifest.json` and full-screen tags. The top bar in `uniform.css` pads for `safe-area-inset-top`. | – |
| 2026-11-06 | Muddy Paws and Docket icons added to `brand/`. Vital Signs now uses the teal example icon. | Muddy Paws, Docket, Vital Signs |
| 2026-11-08 | The sidebar collapses to a rail of icons (6.1): `partials/sidebar_toggle.html`, `js/sidebar.js`, and `theme_head.html` restores it before first paint. Nav links carry a `title`. Opt-in: an app gets it by adding the partial and the script. | HomeForm (first), Docket, PodBridge, Vital Signs, Muddy Paws, Box Office, The Back Room, Encore; Binge Stats in its own shell; claude-ops by hand |
| 2026-11-08 | Collapsed, the sidebar footer's theme switch drops its side margins, so its three buttons fit inside the box in the 72px rail. | Every plain-CSS app with the collapsing sidebar; claude-ops by hand |
| 2026-11-09 | More in the phone's bottom nav (6.2), from HomeForm and Muddy Paws: `.nav-more` in `uniform.css` (sits in the bar like the other items, opens upwards, rows inside), and `templates/base.html` renders it as `<details class="menu-wrap nav-more">` with the `more` glyph and a close-on-outside-tap / Escape script, in place of the Alpine menu. The shell's nav glyphs carry `icon h-5 w-5`, so they're sized with plain `uniform.css` as well as Tailwind. Apps can delete their own `.nav-more` rules once they take this release. | – |
| 2026-11-09 | Caching static files, from HomeForm: `APP-STANDARD.md` 12 (pages `no-store`, static `no-cache`, every static link versioned with `?v=<mtime>`) and 13 (the tunnel checklist: a Cloudflare Cache Rule with `browser_ttl: respect_origin` per app hostname, beside the Browser Integrity Check rule), since the zone's 4-hour Browser Cache TTL overrode `no-cache` and phones kept old CSS. `du_flask.init_design` now adds `?v=` to every `url_for('static', ...)` link; `version_static=False` turns it off. | HomeForm (source) |
| 2026-11-09 | No answer may sit in a shared cache, from Encore: Cloudflare kept Encore's signed-in JSON (no `Cache-Control`) and served it to anyone with no cookie. `APP-STANDARD.md` 12: every answer that isn't a static file is `no-store`, or `private, max-age=…` for what a browser may keep, never `public` and never missing, set for every response in `after_request` rather than for `text/html` only. 13: a shared Cache Rule keeps Cloudflare's edge cache off everything but `/static/` on the app hostnames. `du_api.image_response` now answers `private, max-age=86400` (was `public`), and `ApiCore.error` answers `no-store`; `API-STANDARD.md` 6 and 10 say so, and `check_api.py` checks status, errors and images for it. Apps fix their own `after_request` now; they get the `du_api` change on the next update. | Encore (source), Binge Stats, Docket, Muddy Paws, PodBridge, HomeForm, Box Office; claude-ops by hand |
| 2026-11-09 | Page height on phones (`PWA-ICONS.md` 3.1), from HomeForm: `body` and `.shell` use `100dvh` (Safari's `100vh` made short pages scroll), plus `env(safe-area-inset-top)` in `display-mode: standalone`, since a home-screen app with the black-translucent status bar is told the screen is short by the status bar and the fixed bottom nav floated above the bottom edge. New `js/fit-screen.js` and `html.full-height` / `.fill` in `uniform.css` for a page exactly the screen's height whose list scrolls inside it. Never `overflow: hidden` on `html` or `body`. Apps get the CSS on update; a full-height page opts in. | HomeForm (source) |
