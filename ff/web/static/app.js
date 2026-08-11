'use strict';

/* ------------------------------------------------------------------ *
 * Tiny helpers
 * ------------------------------------------------------------------ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

let toastTimer;
function toast(message, isError = false) {
  const box = $('#toast');
  box.textContent = message;
  box.className = 'toast' + (isError ? ' err' : '');
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, isError ? 6000 : 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

const num = (v, digits = 1) =>
  (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toFixed(digits);

/* ------------------------------------------------------------------ *
 * App state
 * ------------------------------------------------------------------ */

const State = {
  boot: null,
  league: null,
  view: 'draft',
  posFilter: 'ALL',
  search: '',
  draft: null,
  pool: [],
  // The week the in-season views are looking at. Null until the server picks
  // one, which it does by finding the first week with no scores recorded.
  week: null,
};

const POS_ALL = ['ALL', 'QB', 'RB', 'WR', 'TE', 'K', 'DST'];

function mount(templateId) {
  const main = $('#main');
  main.replaceChildren($(templateId).content.cloneNode(true));
  return main;
}

function setView(view) {
  State.view = view;
  $$('#nav .tab').forEach(t => t.classList.toggle('active', t.dataset.view === view));
  ({ draft: viewDraft, week: viewWeek, results: viewResults, trades: viewTrades,
     players: viewPlayers, settings: viewSettings })[view]();
}

/* ------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------ */

async function boot() {
  State.boot = await api('/api/bootstrap');

  const picker = $('#leaguePicker');
  picker.replaceChildren(
    ...State.boot.leagues.map(l => el('option', { value: l.id }, `${l.name} (${l.platform})`)),
    el('option', { value: '__new__' }, '+ New league…'),
  );
  picker.onchange = () => {
    if (picker.value === '__new__') { viewSetup(); return; }
    loadLeague(picker.value);
  };

  $$('#nav .tab').forEach(tab => { tab.onclick = () => setView(tab.dataset.view); });

  if (State.boot.leagues.length) {
    picker.hidden = false;
    await loadLeague(State.boot.leagues[0].id);
  } else {
    viewSetup();
  }
}

async function loadLeague(id) {
  const { league, schedule } = await api(`/api/league/${id}`);
  State.league = league;
  State.schedule = schedule;
  // Anything cached from the previous league is about a different set of
  // players and a different season.
  State.week = null;
  State.pool = [];
  $('#nav').hidden = false;
  $('#leaguePicker').hidden = false;
  $('#leaguePicker').value = id;
  startCountdown();
  setView(league.teams?.length ? 'draft' : 'settings');
}

/* ------------------------------------------------------------------ *
 * Draft countdown
 *
 * Ticks locally off the stored ISO timestamp rather than polling, so it
 * stays accurate without hammering the server.
 * ------------------------------------------------------------------ */

let countdownTimer;

function startCountdown() {
  clearInterval(countdownTimer);
  const box = $('#draftClock');
  const iso = State.league?.draft?.scheduled_at;
  if (!iso) { box.hidden = true; return; }

  const target = new Date(iso);
  if (Number.isNaN(target.getTime())) { box.hidden = true; return; }

  const tick = () => {
    const seconds = Math.floor((target - new Date()) / 1000);
    box.hidden = false;
    if (seconds <= 0) {
      const elapsed = -seconds;
      box.className = 'draft-countdown live';
      box.textContent = elapsed < 6 * 3600
        ? '● Draft is live'
        : `Drafted ${target.toLocaleDateString()}`;
      return;
    }
    box.className = 'draft-countdown' + (seconds < 3600 ? ' soon' : '');
    box.replaceChildren(
      el('span', { class: 'cd-label' }, 'Draft in'),
      el('b', {}, formatCountdown(seconds)));
    box.title = `${target.toLocaleString()} · ${State.league.draft.timezone || ''}`;
  };

  tick();
  countdownTimer = setInterval(tick, 1000);
}

function formatCountdown(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m ${s}s`;
  return `${m}m ${s}s`;
}

/** Split a stored ISO string into the value a datetime-local input wants
 *  plus its offset, without letting the browser shift it into local time. */
function splitIso(iso) {
  const match = String(iso || '').match(
    /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2})?(Z|[+-]\d{2}:\d{2})?$/);
  if (!match) return { local: '', offset: '-04:00' };
  return {
    local: `${match[1]}T${match[2]}`,
    offset: match[3] === 'Z' ? '+00:00' : (match[3] || '-04:00'),
  };
}

/* ================================================================== *
 * SETUP WIZARD
 * ================================================================== */

function viewSetup() {
  mount('#tpl-setup');
  $('#nav').hidden = true;

  let platform = 'yahoo';
  const settings = {};

  const list = $('#platformList');
  const fields = $('#platformFields');

  function renderPlatforms() {
    list.replaceChildren(...State.boot.platforms.map(p =>
      el('button', {
        class: 'platform' + (p.kind === platform ? ' sel' : ''),
        type: 'button',
        onclick: () => { platform = p.kind; renderPlatforms(); renderFields(); },
      }, el('b', {}, p.label), el('span', {}, p.description))));
  }

  function renderFields() {
    const spec = State.boot.platforms.find(p => p.kind === platform);
    fields.replaceChildren(...(spec?.fields || []).map(f =>
      el('label', {}, f.label,
        el('input', {
          type: f.key.includes('secret') ? 'password' : 'text',
          placeholder: f.help,
          oninput: e => { settings[f.key] = e.target.value; },
        }))));
    $('#yahooAuth').hidden = platform !== 'yahoo';
  }

  renderPlatforms();
  renderFields();

  $('#btnAuthUrl').onclick = async () => {
    try {
      const { url } = await api('/api/yahoo/authorize-url', { method: 'POST', body: { settings } });
      $('#authUrl').href = url;
      $('#authUrl').textContent = url;
      $('#authUrlBox').hidden = false;
    } catch (err) { toast(err.message, true); }
  };

  $('#btnExchange').onclick = async () => {
    try {
      await api('/api/yahoo/exchange', {
        method: 'POST', body: { settings, code: $('#yahooCode').value },
      });
      toast('Connected to Yahoo.');
    } catch (err) { toast(err.message, true); }
  };

  // -- teams ---------------------------------------------------------
  const rows = $('#teamRows');
  const addRow = (name = '', manager = '') => rows.append(
    el('div', { class: 'team-row' },
      el('input', { type: 'text', placeholder: 'Team name', value: name }),
      el('input', { type: 'text', placeholder: 'Manager', value: manager }),
      el('button', { type: 'button', title: 'Remove',
        onclick: e => e.target.closest('.team-row').remove() }, '×')));

  for (let i = 0; i < 4; i++) addRow();
  $('#btnAddTeam').onclick = () => addRow();

  $('#btnCreate').onclick = async () => {
    const teams = $$('.team-row', rows)
      .map(r => {
        const [nameInput, managerInput] = $$('input', r);
        return { name: nameInput.value.trim(), manager: managerInput.value.trim() };
      })
      .filter(t => t.name);

    const body = {
      name: $('#fName').value.trim() || 'My League',
      platform,
      team_count: Number($('#fTeams').value) || 12,
      ppr: Number($('#fPpr').value),
      teams,
    };

    if (teams.length && teams.length !== body.team_count) {
      showProblems('#setupProblems', [
        `You listed ${teams.length} teams but set the league to ${body.team_count}. ` +
        `Either add the rest or change the team count.`]);
      return;
    }

    try {
      const { league } = await api('/api/leagues', { method: 'POST', body });
      // Persist platform credentials alongside the league.
      league.platform.settings = settings;
      league.draft.rounds = Number($('#fRounds').value) || 15;
      await api(`/api/league/${league.id}`, { method: 'PUT', body: { league } });
      State.boot = await api('/api/bootstrap');
      const picker = $('#leaguePicker');
      picker.replaceChildren(
        ...State.boot.leagues.map(l => el('option', { value: l.id }, `${l.name} (${l.platform})`)),
        el('option', { value: '__new__' }, '+ New league…'));
      toast('League created.');
      await loadLeague(league.id);
    } catch (err) {
      showProblems('#setupProblems', [err.message]);
    }
  };
}

function showProblems(selector, problems) {
  const box = $(selector);
  if (!box) return;
  if (!problems.length) { box.hidden = true; return; }
  box.replaceChildren(
    el('b', {}, 'Fix these first:'),
    el('ul', {}, ...problems.map(p => el('li', {}, p))));
  box.hidden = false;
}

/* ================================================================== *
 * DRAFT
 * ================================================================== */

async function viewDraft() {
  mount('#tpl-draft');
  const id = State.league.id;

  const [draft, players] = await Promise.all([
    api(`/api/league/${id}/draft`).catch(err => ({ error: err.message })),
    api(`/api/league/${id}/players?limit=600`),
  ]);

  if (draft.error) {
    $('#main').replaceChildren(el('div', { class: 'empty' },
      el('h3', {}, 'Draft not ready'), el('p', {}, draft.error)));
    return;
  }

  State.draft = draft;
  State.pool = players.players;
  $('#noProjections').hidden = players.total > 0;
  renderDepthWarnings(players.depth_warnings);

  const teamSel = $('#draftTeam');
  teamSel.replaceChildren(...draft.teams.map(t => el('option', { value: t.id }, t.name)));
  const onClock = draft.board.on_the_clock;
  teamSel.value = onClock ? onClock.team_id : (draft.my_team_id || draft.teams[0]?.id);
  teamSel.onchange = renderRecs;

  $('#btnUndo').onclick = async () => {
    await api(`/api/league/${id}/draft/undo`, { method: 'POST' });
    toast('Pick undone.');
    viewDraft();
  };
  $('#btnReset').onclick = async () => {
    if (!confirm('Clear every pick in this draft? This cannot be undone.')) return;
    await api(`/api/league/${id}/draft/reset`, { method: 'POST' });
    toast('Draft reset.');
    viewDraft();
  };

  $('#search').oninput = e => { State.search = e.target.value.toLowerCase(); renderAvailable(); };

  $('#posChips').replaceChildren(...POS_ALL.map(pos =>
    el('button', {
      class: 'chip' + (pos === State.posFilter ? ' on' : ''), type: 'button',
      onclick: () => { State.posFilter = pos; viewDraftRefreshChips(); renderAvailable(); },
    }, pos)));

  renderClock();
  renderScarcity();
  renderRecent();
  renderAvailable();
  await renderRecs();
}

function renderDepthWarnings(warnings) {
  const host = $('#noProjections');
  if (!host || !warnings || !warnings.length) return;
  host.parentNode.insertBefore(
    el('div', { class: 'problems' },
      el('b', {}, 'Your projections are too shallow for this league:'),
      el('ul', {}, ...warnings.map(w => el('li', {}, w.message)))),
    host.nextSibling);
}

function viewDraftRefreshChips() {
  $$('#posChips .chip').forEach(c =>
    c.classList.toggle('on', c.textContent === State.posFilter));
}

function renderClock() {
  const board = State.draft.board;
  const clock = board.on_the_clock;
  const box = $('#clock');
  if (!clock) {
    box.replaceChildren(el('span', {}, 'Draft complete — '),
      el('b', {}, `${board.picks_made} picks made`));
    return;
  }
  const team = State.draft.teams.find(t => t.id === clock.team_id);
  box.replaceChildren(
    el('span', {}, `Round ${clock.round}, pick ${clock.pick_in_round} · `),
    el('b', {}, team ? team.name : clock.team_id),
    el('span', {}, ` · #${clock.overall} of ${board.picks_total}`));
}

function renderScarcity() {
  const rows = State.draft.board.scarcity || [];
  const max = Math.max(1, ...rows.map(r => r.cliff));
  $('#scarcity').replaceChildren(...rows.map(r =>
    el('div', { class: 'scar' },
      el('span', { class: `pos ${r.pos}` }, r.pos),
      el('div', { class: 'bar' }, el('i', { style: `width:${(r.cliff / max) * 100}%` })),
      el('span', { class: 'num2', title: 'Points lost after this tier clears' },
        `−${num(r.cliff, 0)}`))));
}

function renderRecent() {
  const picks = State.draft.board.recent || [];
  $('#recent').replaceChildren(...(picks.length
    ? picks.map(p => el('div', { class: 'prow' },
        el('span', { class: `pos ${p.pos}` }, p.pos),
        el('div', {}, p.player_name,
          el('div', { class: 'meta' }, `R${p.round}.${p.pick_in_round}`)),
        el('span', {}), el('span', {})))
    : [el('div', { class: 'hint' }, 'No picks yet.')]));
}

function renderAvailable() {
  const taken = new Set((State.draft.picks || [])
    .filter(p => p.player_id).map(p => p.player_id));

  const rows = State.pool
    .filter(p => !taken.has(p.player_id))
    .filter(p => State.posFilter === 'ALL' || p.pos === State.posFilter)
    .filter(p => !State.search || p.name.toLowerCase().includes(State.search))
    .slice(0, 200);

  $('#avail').replaceChildren(...rows.map(p =>
    el('div', { class: 'prow', title: 'Click to draft', onclick: () => draftPlayer(p) },
      el('span', { class: `pos ${p.pos}` }, p.pos),
      el('div', {}, p.name,
        el('div', { class: 'meta' },
          `${p.team || 'FA'} · ${p.pos}${p.pos_rank} · tier ${p.value_tier}` +
          (p.adp ? ` · ADP ${num(p.adp, 0)}` : ''))),
      el('span', { class: 'num2', title: 'Projected points' }, num(p.points, 0)),
      el('span', { class: 'num2', title: 'Value over replacement' }, num(p.vor, 0)))));
}

async function renderRecs() {
  const id = State.league.id;
  const teamId = $('#draftTeam').value;
  const team = State.draft.teams.find(t => t.id === teamId);
  $('#recTeam').textContent = team ? team.name : teamId;

  let data;
  try {
    data = await api(`/api/league/${id}/draft/recommend?team=${encodeURIComponent(teamId)}`);
  } catch (err) { toast(err.message, true); return; }

  const gap = data.picks_until_next;
  $('#recs').replaceChildren(
    ...(gap ? [el('div', { class: 'hint' },
        `${gap} pick${gap === 1 ? '' : 's'} until this team is up again.`)] : []),
    ...(data.recommendations.length
      ? data.recommendations.map((r, i) => {
          const p = r.player;
          return el('div', { class: 'rec', onclick: () => draftPlayer(p, teamId) },
            el('span', { class: 'rank' }, `${i + 1}`),
            el('div', {},
              el('div', { class: 'who' },
                el('span', { class: `pos ${p.pos}` }, p.pos), ' ', p.name,
                ' ', el('span', { class: 'meta' }, p.team || '')),
              el('div', { class: 'why' }, r.reason || '')),
            el('span', { class: 'score', title: 'Blended draft score' }, num(r.score, 0)));
        })
      : [el('div', { class: 'hint' }, 'No recommendations — the roster is full or the pool is empty.')]));

  $('#myRoster').replaceChildren(...(data.roster.length
    ? data.roster.map(p => el('div', { class: 'prow' },
        el('span', { class: `pos ${p.pos}` }, p.pos),
        el('div', {}, p.name),
        el('span', { class: 'num2' }, num(p.points, 0)),
        el('span', {})))
    : [el('div', { class: 'hint' }, 'Empty.')]));
}

async function draftPlayer(player, teamId) {
  try {
    await api(`/api/league/${State.league.id}/draft/pick`, {
      method: 'POST',
      body: { player_id: player.player_id, team_id: teamId || undefined },
    });
    toast(`Drafted ${player.name}.`);
    viewDraft();
  } catch (err) { toast(err.message, true); }
}

/* ================================================================== *
 * MY TEAM
 * ================================================================== */

/* ================================================================== *
 * THE WEEK
 *
 * Lineup and waiver wire together, because they are one decision: the
 * question is never "who starts" or "who do I add" in isolation, it is
 * "what is the best team I can field on Sunday".
 * ================================================================== */

const STATUS_OPTIONS = [
  ['', 'Healthy'], ['Q', 'Questionable'], ['D', 'Doubtful'],
  ['O', 'Out'], ['IR', 'IR'], ['SUS', 'Suspended'],
];

function teamName(id) {
  return (State.league.teams || []).find(t => t.id === id)?.name || id || '—';
}

async function viewWeek() {
  mount('#tpl-week');
  const id = State.league.id;
  const teams = State.league.teams || [];

  $('#wkTeam').replaceChildren(...teams.map(t => el('option', { value: t.id }, t.name)));
  $('#wkTeam').value = State.league.my_team_id || teams[0]?.id || '';
  $('#wkTeam').onchange = () => refresh();
  $('#wkPicker').onchange = () => { State.week = Number($('#wkPicker').value); refresh(); };

  await refresh();

  async function refresh() {
    await loadWeek();
    await loadWaivers();
  }

  function query(extra = {}) {
    const q = new URLSearchParams({ team: $('#wkTeam').value, ...extra });
    if (State.week) q.set('week', State.week);
    return q;
  }

  async function setStatus(playerId, status) {
    try {
      await api(`/api/league/${id}/week/status?week=${State.week}`,
        { method: 'POST', body: { player_id: playerId, status } });
    } catch (err) { toast(err.message, true); return; }
    await refresh();
  }

  function playerRow(player, slot) {
    return el('div', { class: 'prow' + (player && !player.playable ? ' out' : '') },
      slot ? el('span', { class: 'slotname' }, slot) : null,
      player ? el('span', { class: `pos ${player.pos}` }, player.pos) : el('span', {}),
      el('div', {}, player ? player.name : '—',
        player && el('div', { class: 'meta' },
          [player.team || 'FA',
           player.opponent ? `vs ${player.opponent}` : null,
           player.bye ? `bye ${player.bye}` : null].filter(Boolean).join(' · '))),
      el('span', { class: 'num2' }, player ? num(player.points) : ''),
      player
        ? el('select', {
            class: 'status', title: 'Availability',
            onchange: e => setStatus(player.player_id, e.target.value),
          }, ...STATUS_OPTIONS.map(([value, label]) =>
              el('option', { value, selected: player.status === value || null }, label)))
        : el('span', {}));
  }

  async function loadWeek() {
    let data;
    try { data = await api(`/api/league/${id}/week?${query()}`); }
    catch (err) { toast(err.message, true); return; }

    State.week = data.week;
    $('#wkNum').textContent = data.week;
    $('#wkPicker').replaceChildren(...data.weeks.map(w =>
      el('option', { value: w, selected: w === data.week || null }, `Week ${w}`)));

    const banner = $('#wkBanner');
    banner.hidden = data.weekly_projections;
    banner.textContent =
      'No week-specific projections imported — these are season totals split ' +
      'evenly across the schedule, so they know nothing about this week\'s ' +
      'matchup. Import a CSV with a "week" column for real weekly numbers.';

    $('#wkTotal').textContent = `${num(data.projected)} pts`;
    $('#wkMatchup').replaceChildren(renderMatchup(data.matchup, data.projected));
    $('#wkLineup').replaceChildren(
      ...data.lineup.map(row => playerRow(row.player, row.slot)));
    $('#wkBench').replaceChildren(...(data.bench.length
      ? data.bench.map(p => playerRow(p, null))
      : [el('div', { class: 'hint' }, 'No bench players.')]));
  }

  function renderMatchup(game, projected) {
    if (!game) {
      return el('div', { class: 'hint' },
        'No fixture this week — check the schedule under League.');
    }
    const final = game.result !== 'pending';
    return el('div', { class: `matchup-card ${final ? game.result : 'pending'}` },
      el('div', { class: 'side' },
        el('h4', {}, 'You'),
        el('div', { class: 'score' }, final ? num(game.my_points) : num(projected)),
        el('div', { class: 'meta' }, final ? 'final' : 'projected')),
      el('div', { class: 'vs' }, final ? game.result.toUpperCase() : 'vs'),
      el('div', { class: 'side' },
        el('h4', {}, teamName(game.opponent)),
        el('div', { class: 'score' },
          final ? num(game.opponent_points) : num(game.opponent_projected)),
        el('div', { class: 'meta' }, final ? 'final' : 'projected')));
  }

  async function loadWaivers() {
    const box = $('#wkWaivers');
    box.replaceChildren(el('div', { class: 'hint' }, 'Searching the wire…'));
    let data;
    try { data = await api(`/api/league/${id}/waivers?${query({ limit: 12 })}`); }
    catch (err) { box.replaceChildren(el('div', { class: 'hint' }, err.message)); return; }

    $('#wkFaab').textContent = data.faab
      ? `$${num(data.faab.remaining, 0)} of $${num(data.faab.budget, 0)} left`
      : `${data.roster_size}/${data.roster_capacity} roster spots`;
    const limits = $('#wkLimits');
    limits.hidden = !(data.limits || []).length;
    limits.replaceChildren(...(data.limits || []).map(m => el('div', {}, m)));

    if (!data.recommendations.length) {
      box.replaceChildren(el('div', { class: 'empty' },
        el('h3', {}, 'Nothing worth adding'),
        el('p', {}, 'No free agent improves this lineup. That is a good sign.')));
    } else {
      box.replaceChildren(...data.recommendations.map(r => el('div', { class: 'wrec' },
        el('div', { class: 'rec-head' },
          el('span', { class: `pos ${r.player.pos}` }, r.player.pos),
          el('b', {}, r.player.name),
          r.bid !== null && r.bid !== undefined
            ? el('span', { class: 'pill' }, `bid $${r.bid}`) : null,
          r.fills_gap ? el('span', { class: 'pill warn' }, 'fills a hole') : null),
        el('div', { class: 'rec-nums' },
          el('span', {
            class: 'gain' + (r.gain_week > 0 ? '' : ' none'),
            title: 'Points added to this week\'s starting lineup',
          }, `week ${r.gain_week > 0 ? '+' : ''}${num(r.gain_week)}`),
          el('span', {
            class: 'gain' + (r.gain_per_week > 0 ? '' : ' none'),
            title: 'Points added per week for the rest of the season',
          }, `rest ${r.gain_per_week > 0 ? '+' : ''}${num(r.gain_per_week)}/wk`),
          el('span', { title: 'Value over replacement' }, `vor ${num(r.vor)}`)),
        el('div', { class: 'meta' }, r.reason))));
    }

    $('#wkDrops').replaceChildren(...(data.drops || []).slice(0, 5).map(d =>
      el('div', { class: 'prow' },
        el('span', { class: `pos ${d.player.pos}` }, d.player.pos),
        el('div', {}, d.player.name,
          el('div', { class: 'meta' },
            d.starter ? 'currently starting' : 'not in your lineup')),
        el('span', { class: 'num2', title: 'What dropping him costs you' },
          d.cost > 0 ? `−${num(d.cost)}` : 'free'),
        el('span', {}))));
  }
}

/* ================================================================== *
 * RESULTS
 * ================================================================== */

async function viewResults() {
  mount('#tpl-results');
  const id = State.league.id;
  const teams = State.league.teams || [];

  $('#rsWeek').onchange = () => { State.week = Number($('#rsWeek').value); loadBoard(); };
  $('#btnScores').onclick = () => {
    const form = $('#rsScoreForm');
    form.hidden = !form.hidden;
  };

  await Promise.all([loadBoard(), loadStandings(), loadActivity()]);
  setupMover();

  async function loadBoard() {
    let data;
    const q = State.week ? `?week=${State.week}` : '';
    try { data = await api(`/api/league/${id}/scoreboard${q}`); }
    catch (err) { toast(err.message, true); return; }

    State.week = data.week;
    $('#rsWeek').replaceChildren(...data.weeks.map(w =>
      el('option', { value: w, selected: w === data.week || null }, `Week ${w}`)));
    $('#rsState').textContent = data.complete
      ? `final · median ${num(data.median)}` : 'not final';

    $('#rsBoard').replaceChildren(...(data.games.length
      ? data.games.map(g => el('div', { class: 'game' + (g.final ? ' final' : '') },
          el('div', { class: 'gteam' + (g.outcome === 'home' ? ' won' : '') },
            teamName(g.home)),
          el('div', { class: 'gscore' },
            g.final ? num(g.home_points) : num(g.home_projected)),
          el('div', { class: 'gdash' }, g.final ? '–' : 'proj'),
          el('div', { class: 'gscore' },
            g.final ? num(g.away_points) : num(g.away_projected)),
          el('div', { class: 'gteam' + (g.outcome === 'away' ? ' won' : '') },
            teamName(g.away))))
      : [el('div', { class: 'hint' }, 'No fixtures for this week.')]));

    renderScoreForm(data.recorded || {});
  }

  function renderScoreForm(recorded) {
    const inputs = teams.map(t => el('label', {}, t.name,
      el('input', {
        type: 'number', step: '0.01', class: 'scorein',
        'data-team': t.id, value: recorded[t.id] ?? '',
      })));
    $('#rsScoreForm').replaceChildren(
      el('div', { class: 'fields' }, ...inputs),
      el('button', {
        class: 'btn primary', type: 'button',
        onclick: async () => {
          const scores = {};
          $$('#rsScoreForm .scorein').forEach(input => {
            if (input.value !== '') scores[input.dataset.team] = Number(input.value);
          });
          try {
            await api(`/api/league/${id}/scores?week=${State.week}`,
              { method: 'POST', body: { scores } });
          } catch (err) { toast(err.message, true); return; }
          toast('Scores saved');
          await Promise.all([loadBoard(), loadStandings()]);
        },
      }, 'Save week ' + State.week));
  }

  async function loadStandings() {
    let data;
    try { data = await api(`/api/league/${id}/standings`); }
    catch (err) { toast(err.message, true); return; }

    $('#rsTiebreak').textContent =
      `Top ${data.playoff_teams} make the playoffs (weeks ` +
      `${data.playoff_weeks.join(', ')}). Ties broken on ${data.tiebreaker}.`;

    $('#rsStandings').replaceChildren(
      el('div', { class: 'srow head' },
        el('span', {}, '#'), el('div', {}, 'Team'), el('span', {}, 'Record'),
        el('span', {}, 'PF'), el('span', {}, 'PA'), el('span', {}, 'Streak')),
      ...data.standings.map(r => el('div', {
        class: 'srow' + (r.in_playoffs ? ' playoff' : '')
             + (r.team_id === data.my_team_id ? ' mine' : ''),
      },
        el('span', {}, r.rank),
        el('div', {}, teamName(r.team_id)),
        el('span', {}, r.games_played ? r.record : '—'),
        el('span', {}, num(r.points_for, 0)),
        el('span', {}, num(r.points_against, 0)),
        el('span', {}, r.streak))));
  }

  async function loadActivity() {
    let data;
    try { data = await api(`/api/league/${id}/transactions`); }
    catch (err) { toast(err.message, true); return; }

    $('#rsChased').replaceChildren(...(data.positions_chased.length
      ? [el('div', { class: 'hint' }, 'The league is buying: '),
         ...data.positions_chased.map(p =>
           el('span', { class: 'pill' }, `${p.pos} ×${p.adds}`))]
      : []));

    $('#rsFeed').replaceChildren(...(data.feed.length
      ? data.feed.map(t => el('div', { class: 'frow' },
          el('span', { class: 'pill' }, t.week ? `wk ${t.week}` : '—'),
          el('div', {}, t.summary),
          el('button', {
            class: 'btn ghost tiny', type: 'button', title: 'Undo this move',
            onclick: async () => {
              try { await api(`/api/league/${id}/transactions/${t.id}`, { method: 'DELETE' }); }
              catch (err) { toast(err.message, true); return; }
              await loadActivity();
            },
          }, '×')))
      : [el('div', { class: 'hint' }, 'No moves recorded yet.')]));
  }

  function setupMover() {
    const options = teams.map(t => el('option', { value: t.id }, t.name));
    $('#mvTeam').replaceChildren(...options);
    $('#mvTeam').value = State.league.my_team_id || teams[0]?.id || '';
    $('#mvPartner').replaceChildren(...teams.map(t => el('option', { value: t.id }, t.name)));
    $('#mvType').onchange = () => {
      $('#mvPartnerWrap').hidden = $('#mvType').value !== 'trade';
    };

    $('#btnMove').onclick = async () => {
      const ids = await resolvePlayers([$('#mvAdds').value, $('#mvDrops').value]);
      if (ids.problems.length) { showProblems('#mvProblems', ids.problems); return; }
      showProblems('#mvProblems', []);

      const body = {
        type: $('#mvType').value,
        team_id: $('#mvTeam').value,
        adds: ids.groups[0],
        drops: ids.groups[1],
        week: State.week,
        faab: $('#mvFaab').value === '' ? null : Number($('#mvFaab').value),
      };
      if (body.type === 'trade') body.partner_team_id = $('#mvPartner').value;

      try { await api(`/api/league/${id}/transactions`, { method: 'POST', body }); }
      catch (err) { showProblems('#mvProblems', [err.message]); return; }
      toast('Move recorded');
      $('#mvAdds').value = $('#mvDrops').value = $('#mvFaab').value = '';
      await loadActivity();
    };
  }

  /* Names are what people type; ids are what the API wants. Resolve here and
     say exactly which name failed rather than silently dropping it. */
  async function resolvePlayers(fields) {
    if (!State.pool.length) {
      const data = await api(`/api/league/${id}/players?limit=2000`);
      State.pool = data.players;
    }
    const index = new Map(State.pool.map(p => [p.name.toLowerCase(), p.player_id]));
    const problems = [];
    const groups = fields.map(text => (text || '')
      .split(',').map(s => s.trim()).filter(Boolean)
      .map(name => {
        const found = index.get(name.toLowerCase());
        if (!found) problems.push(`No player called "${name}" in the pool.`);
        return found;
      })
      .filter(Boolean));
    return { groups, problems };
  }
}

/* ================================================================== *
 * TRADES
 * ================================================================== */

async function viewTrades() {
  mount('#tpl-trades');
  const id = State.league.id;
  const picker = $('#tradeTeam');
  picker.replaceChildren(...(State.league.teams || [])
    .map(t => el('option', { value: t.id }, t.name)));
  picker.value = State.league.my_team_id || State.league.teams?.[0]?.id || '';

  const nameOf = tid =>
    (State.league.teams || []).find(t => t.id === tid)?.name || tid;

  $('#btnSuggest').onclick = async () => {
    $('#tradeList').replaceChildren(el('div', { class: 'hint' }, 'Searching…'));
    let data;
    try {
      data = await api(`/api/league/${id}/trades/suggest?team=${encodeURIComponent(picker.value)}`);
    } catch (err) {
      $('#tradeList').replaceChildren(el('div', { class: 'hint' }, err.message));
      return;
    }

    if (!data.suggestions.length) {
      $('#tradeList').replaceChildren(el('div', { class: 'empty' },
        el('h3', {}, 'No trades worth proposing'),
        el('p', {}, 'Every legal pairing either breaks a league rule or fails to ' +
                    'improve both starting lineups.')));
      return;
    }

    const chunk = players => el('div', {},
      ...players.map(p => el('div', { class: 'prow' },
        el('span', { class: `pos ${p.pos}` }, p.pos),
        el('div', {}, p.name),
        el('span', { class: 'num2' }, num(p.points, 0)),
        el('span', {}))));

    $('#tradeList').replaceChildren(...data.suggestions.map(t =>
      el('div', { class: 'trade' },
        el('div', { class: 'trade-head' },
          el('span', { class: `verdict ${t.verdict}` }, t.verdict.replace('-', ' ')),
          el('b', {}, `with ${nameOf(t.partner_team_id)}`),
          el('span', { class: 'pill' }, `you +${num(t.team_a.lineup_gain)}`),
          el('span', { class: 'pill' }, `them +${num(t.team_b.lineup_gain)}`),
          el('span', { class: 'pill' }, `${num(t.gap_pct, 0)}% value gap`),
          t.veto_risk && el('span', { class: 'verdict favors-you' }, 'veto risk')),
        el('div', { class: 'trade-sides' },
          el('div', { class: 'side' }, el('h4', {}, 'You send'), chunk(t.team_a.sends)),
          el('div', { class: 'swap' }, '⇄'),
          el('div', { class: 'side' }, el('h4', {}, 'You get'), chunk(t.team_a.receives))),
        el('ul', { class: 'notes' }, ...(t.notes || []).map(n => el('li', {}, n))))));
  };
}

/* ================================================================== *
 * PLAYERS
 * ================================================================== */

async function viewPlayers() {
  mount('#tpl-players');
  let filter = 'ALL', search = '';

  $('#pChips').replaceChildren(...POS_ALL.map(pos =>
    el('button', { class: 'chip' + (pos === 'ALL' ? ' on' : ''), type: 'button',
      onclick: e => {
        filter = pos;
        $$('#pChips .chip').forEach(c => c.classList.toggle('on', c === e.target));
        render();
      } }, pos)));
  $('#pSearch').oninput = e => { search = e.target.value.toLowerCase(); render(); };

  const data = await api(`/api/league/${State.league.id}/players?limit=800`);

  function render() {
    const rows = data.players
      .filter(p => filter === 'ALL' || p.pos === filter)
      .filter(p => !search || p.name.toLowerCase().includes(search))
      .slice(0, 250);

    $('#playerTable').replaceChildren(
      rows.length
        ? el('div', { class: 'avail' }, ...rows.map((p, i) =>
            el('div', { class: 'prow' },
              el('span', { class: `pos ${p.pos}` }, p.pos),
              el('div', {}, `${i + 1}. ${p.name}`,
                el('div', { class: 'meta' },
                  `${p.team || 'FA'} · ${p.pos}${p.pos_rank} · tier ${p.value_tier}`)),
              el('span', { class: 'num2', title: 'Projected points' }, num(p.points, 0)),
              el('span', { class: 'num2', title: 'Value over replacement' }, num(p.vor, 0)))))
        : el('div', { class: 'empty' },
            el('h3', {}, 'No players'),
            el('p', {}, 'Import a projections CSV under League › Projections.')));
  }
  render();
}

/* ================================================================== *
 * PLATFORM SYNC
 *
 * Every button here comes from the adapter's own capability list, so
 * adding a platform never means touching this file.
 * ================================================================== */

/* Connecting an *existing* league. The setup wizard collects these too, but a
   league created before you had credentials — or one whose token expired —
   had nowhere to enter them, which is every league by the time you get round
   to connecting one. */
function renderConnectionPanel(league) {
  const box = $('#connFields');
  if (!box) return;

  const kind = league.platform.kind;
  const spec = State.boot.platforms.find(p => p.kind === kind);
  const settings = league.platform.settings;

  if (!spec?.fields?.length) {
    box.replaceChildren(el('div', { class: 'hint' },
      `${spec?.label || kind} needs no credentials.`));
    $('#connAuth').hidden = true;
    return;
  }

  box.replaceChildren(...spec.fields.map(f => el('label', {}, f.label,
    el('input', {
      type: f.key.includes('secret') ? 'password' : 'text',
      placeholder: f.help,
      // Credentials are write-only: they live in the secret store and the
      // server never sends them back, so an empty box means "unchanged",
      // not "cleared".
      value: f.credential ? '' : (settings[f.key] ?? ''),
      oninput: e => { settings[f.key] = e.target.value; },
    }),
    f.credential
      ? el('div', { class: 'meta' }, 'Write-only. Leave blank to keep what is stored.')
      : null)));

  $('#connAuth').hidden = kind !== 'yahoo';

  $('#btnConnAuthUrl').onclick = async () => {
    try {
      const { url } = await api('/api/yahoo/authorize-url',
        { method: 'POST', body: { settings } });
      $('#connAuthUrl').href = url;
      $('#connAuthUrl').textContent = url;
      $('#connAuthBox').hidden = false;
    } catch (err) { toast(err.message, true); }
  };

  $('#btnConnExchange').onclick = async () => {
    const code = ($('#connCode').value || '').trim();
    if (!code) { toast('Paste the code from the address bar first.', true); return; }
    try {
      await api('/api/yahoo/exchange', { method: 'POST', body: { settings, code } });
    } catch (err) { toast(err.message, true); return; }
    toast('Connected to Yahoo.');
    // Persist the non-secret settings (league id, redirect uri) so the next
    // sync uses the same values the token was minted against.
    try {
      await api(`/api/league/${league.id}`, { method: 'PUT', body: { league } });
    } catch (err) { toast(err.message, true); }
    renderSyncPanel(league.id);
  };

  api(`/api/league/${league.id}/platform/status`)
    .then(s => { $('#connState').textContent = s.detail || (s.ready ? 'Connected' : ''); })
    .catch(() => {});
}

async function renderSyncPanel(leagueId) {
  const box = $('#syncButtons');
  if (!box) return;
  box.replaceChildren(el('div', { class: 'hint' }, 'Checking your platform…'));

  let status;
  try { status = await api(`/api/league/${leagueId}/platform/status`); }
  catch (err) {
    box.replaceChildren(el('div', { class: 'hint' }, err.message));
    return;
  }

  $('#syncState').textContent = status.ready
    ? (status.detail || 'Connected') : (status.detail || 'Not connected');

  const supported = (status.capabilities || []).filter(c => c.supported);
  if (!supported.length) {
    box.replaceChildren(el('div', { class: 'hint' },
      `${status.label || 'This platform'} has no sync support — everything is ` +
      `entered by hand, which works for every feature in the app.`));
    return;
  }

  box.replaceChildren(...supported.map(cap => el('button', {
    class: 'btn ghost sync-btn', type: 'button',
    disabled: !status.ready || null,
    onclick: e => runSync(leagueId, cap.key, cap.label, e.target.closest('button')),
  }, cap.label, el('span', { class: 'cap' }, `sync ${cap.key}`))));
}

async function runSync(leagueId, operation, label, button) {
  const out = $('#syncResult');
  out.hidden = false;
  out.replaceChildren(el('div', { class: 'hint' }, `Syncing ${label.toLowerCase()}…`));
  if (button) button.disabled = true;

  let data;
  try {
    data = await api(`/api/league/${leagueId}/sync/${operation}`, { method: 'POST' });
  } catch (err) {
    out.replaceChildren(el('div', { class: 'warn' }, err.message));
    return;
  } finally {
    if (button) button.disabled = false;
  }

  if (operation === 'rules') { renderRulesDiff(leagueId, data); return; }

  const notes = [];
  const push = (key, word) => {
    const rows = data[key] || [];
    if (rows.length) notes.push(`${rows.length} ${word}: ${rows.slice(0, 8).join(', ')}`);
  };
  push('unmatched_teams', 'teams could not be matched to your league');
  push('players_not_in_pool', 'players are not in your projection pool');
  push('dropped_teams', 'teams are no longer in your league');
  if (data.hint) notes.push(data.hint);

  const headline =
      operation === 'scoreboard' ? `Week ${data.week}: ${data.teams_scored} scores imported`
    : operation === 'transactions' ? `${data.imported} moves imported, ${data.already_recorded} already had`
    : operation === 'rosters' ? `${data.teams_written} rosters written`
    : operation === 'draft' ? `${data.picks} picks imported`
    : operation === 'league' ? `${data.teams} teams imported`
    : 'Done';

  out.replaceChildren(
    el('div', { class: notes.length ? 'warn' : 'ok' }, headline),
    notes.length ? el('ul', {}, ...notes.map(n => el('li', {}, n))) : null);

  if (operation === 'league') await loadLeague(leagueId);
}

/* Scoring drives every valuation in the app, so an imported ruleset is shown
   before it is written, not after. */
function renderRulesDiff(leagueId, data) {
  const out = $('#syncResult');
  const changes = data.changes || [];
  const unmapped = data.unmapped || [];

  if (!changes.length) {
    out.replaceChildren(el('div', { class: 'ok' },
      'Your league config already matches the platform exactly.'),
      ...(unmapped.length ? [unmappedList(unmapped)] : []));
    return;
  }

  out.replaceChildren(
    el('div', { class: 'warn' },
      `${changes.length} rule block(s) differ from your config. Review before applying.`),
    el('table', {}, ...changes.map(c => el('tr', {},
      el('td', {}, c.block),
      el('td', {}, summariseChange(c))))),
    unmapped.length ? unmappedList(unmapped) : null,
    el('button', {
      class: 'btn primary', type: 'button', style: 'margin-top:12px',
      onclick: async () => {
        try {
          await api(`/api/league/${leagueId}/sync/rules/apply`,
            { method: 'POST', body: { proposed: data.proposed } });
        } catch (err) { toast(err.message, true); return; }
        toast('League rules updated from your platform');
        await loadLeague(leagueId);
        setView('settings');
      },
    }, 'Apply these rules'));
}

function summariseChange(change) {
  const after = change.after;
  if (after && typeof after === 'object' && !Array.isArray(after)) {
    const before = change.before || {};
    const keys = Object.keys(after);
    const diff = keys.filter(k => JSON.stringify(before[k]) !== JSON.stringify(after[k]));
    if (!diff.length) return `${keys.length} settings, all unchanged`;
    return diff.slice(0, 12).map(k => `${k}: ${JSON.stringify(before[k]) ?? '—'} → ${JSON.stringify(after[k])}`).join(', ')
      + (diff.length > 12 ? `, +${diff.length - 12} more` : '');
  }
  return `${JSON.stringify(change.before) ?? '—'} → ${JSON.stringify(after)}`;
}

/* Anything the adapter could not translate is named, never dropped quietly —
   a missing scoring rule would change every valuation with nothing on screen
   to explain why. */
function unmappedList(unmapped) {
  return el('div', {},
    el('div', { class: 'warn', style: 'margin-top:10px' },
      `${unmapped.length} platform rule(s) had no equivalent here and were left out:`),
    el('ul', {}, ...unmapped.map(u =>
      el('li', {}, `${u.name || u.stat_id} (${u.value}) — ${u.why}`))));
}

/* ================================================================== *
 * SETTINGS
 * ================================================================== */

async function viewSettings() {
  mount('#tpl-settings');
  const league = structuredClone(State.league);
  league.draft = league.draft || {};
  league.platform = league.platform || { kind: 'manual', settings: {} };
  league.platform.settings = league.platform.settings || {};

  // -- league info ---------------------------------------------------
  const bind = (sel, value, onChange, prop = 'value') => {
    const node = $(sel);
    if (!node) return null;
    node[prop] = value ?? '';
    node.oninput = e => onChange(e.target.value);
    node.onchange = e => onChange(e.target.value);
    return node;
  };

  bind('#sName', league.name, v => { league.name = v; });
  bind('#sSeason', league.season, v => { league.season = Number(v) || league.season; });
  bind('#sLeagueKey', league.platform.settings.league_id,
       v => { league.platform.settings.league_id = v; });

  renderConnectionPanel(league);
  renderSyncPanel(league.id);

  $('#sPlatform').replaceChildren(...State.boot.platforms.map(p =>
    el('option', { value: p.kind, selected: league.platform.kind === p.kind }, p.label)));
  $('#sPlatform').onchange = e => { league.platform.kind = e.target.value; };

  // -- draft schedule ------------------------------------------------
  const parts = splitIso(league.draft.scheduled_at);
  let whenLocal = parts.local;
  let whenOffset = parts.offset;
  const applyWhen = () => {
    league.draft.scheduled_at = whenLocal ? `${whenLocal}:00${whenOffset}` : null;
    refreshScheduleHint();
  };

  bind('#sWhen', whenLocal, v => { whenLocal = v; applyWhen(); });
  const offsetSel = $('#sOffset');
  offsetSel.value = whenOffset;
  if (offsetSel.value !== whenOffset) {            // offset not in the list
    offsetSel.append(el('option', { value: whenOffset, selected: true }, whenOffset));
  }
  offsetSel.onchange = e => { whenOffset = e.target.value; applyWhen(); };

  bind('#sTzLabel', league.draft.timezone, v => { league.draft.timezone = v; });
  bind('#sArrive', league.draft.arrive_minutes_early,
       v => { league.draft.arrive_minutes_early = Number(v) || 0; });
  bind('#sWhere', league.draft.location, v => { league.draft.location = v; });
  bind('#sNotes', league.draft.notes, v => { league.draft.notes = v; });
  bind('#sRounds', league.draft.rounds, v => { league.draft.rounds = Number(v) || 1; });
  bind('#sClock', league.draft.seconds_per_pick,
       v => { league.draft.seconds_per_pick = Number(v) || 60; });

  const typeSel = $('#sDraftType');
  typeSel.value = league.draft.type || 'snake';
  typeSel.onchange = e => { league.draft.type = e.target.value; };

  function refreshScheduleHint() {
    const box = $('#sCountdown');
    if (!league.draft.scheduled_at) { box.textContent = 'not scheduled'; return; }
    const target = new Date(league.draft.scheduled_at);
    if (Number.isNaN(target.getTime())) { box.textContent = 'unreadable date'; return; }
    const seconds = Math.floor((target - new Date()) / 1000);
    box.textContent = seconds > 0
      ? `${formatCountdown(seconds)} away`
      : 'in the past';
  }
  refreshScheduleHint();

  // -- draft order ---------------------------------------------------
  function renderOrder() {
    const ids = (league.draft.order && league.draft.order.length)
      ? league.draft.order.filter(id => league.teams.some(t => t.id === id))
      : league.teams.map(t => t.id);
    // Any team missing from a stale saved order goes on the end.
    for (const team of league.teams) if (!ids.includes(team.id)) ids.push(team.id);
    league.draft.order = ids;

    const move = (from, to) => {
      if (to < 0 || to >= ids.length) return;
      [ids[from], ids[to]] = [ids[to], ids[from]];
      league.draft.order = ids;
      renderOrder();
    };

    $('#orderEditor').replaceChildren(...ids.map((id, i) => {
      const team = league.teams.find(t => t.id === id);
      return el('div', { class: 'order-row' },
        el('span', { class: 'order-num' }, `${i + 1}`),
        el('div', {}, team ? team.name : id,
          team?.manager ? el('span', { class: 'meta' }, ` · ${team.manager}`) : null),
        el('button', { class: 'btn ghost', type: 'button', title: 'Move up',
          onclick: () => move(i, i - 1) }, '▲'),
        el('button', { class: 'btn ghost', type: 'button', title: 'Move down',
          onclick: () => move(i, i + 1) }, '▼'));
    }));
  }

  // -- projections ---------------------------------------------------
  async function refreshFiles() {
    const { files } = await api('/api/projections');
    $('#fileList').replaceChildren(...(files.length
      ? files.map(f => el('div', { class: 'filerow' },
          el('span', {}, f.name),
          el('span', { class: 'meta' }, `${(f.size / 1024).toFixed(0)} KB`),
          el('button', { type: 'button', title: 'Remove', onclick: async () => {
            await api(`/api/projections/${encodeURIComponent(f.name)}`, { method: 'DELETE' });
            refreshFiles();
          } }, '×')))
      : [el('div', { class: 'hint' }, 'No projection files yet.')]));
  }
  refreshFiles();

  $('#csvFile').onchange = async e => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const result = await api('/api/projections/import', {
        method: 'POST',
        body: { csv: await file.text(), filename: file.name },
      });
      toast(`Imported ${result.imported} players (${result.with_stats} with full stat lines).`);
      refreshFiles();
    } catch (err) { toast(err.message, true); }
    e.target.value = '';
  };

  // -- scoring -------------------------------------------------------
  const scoringBox = $('#scoringEditor');
  scoringBox.replaceChildren();
  for (const [group, stats] of Object.entries(State.boot.stat_groups)) {
    scoringBox.append(el('div', { class: 'grp' }, group));
    for (const stat of stats) {
      scoringBox.append(el('label', {}, stat.label,
        el('input', {
          type: 'number', step: 'any', value: league.scoring[stat.key] ?? '',
          oninput: e => {
            const v = e.target.value;
            if (v === '') delete league.scoring[stat.key];
            else league.scoring[stat.key] = Number(v);
          },
        })));
    }
  }

  // -- scoring bonuses -----------------------------------------------
  league.scoring_bonuses = league.scoring_bonuses || [];
  const statOptions = Object.values(State.boot.stat_groups).flat();

  function renderBonuses() {
    $('#bonusEditor').replaceChildren(...league.scoring_bonuses.map((bonus, i) =>
      el('div', { class: 'band-row' },
        el('select', { onchange: e => { bonus.stat = e.target.value; } },
          ...statOptions.map(s =>
            el('option', { value: s.key, selected: bonus.stat === s.key }, s.label))),
        el('input', { type: 'number', step: 'any', value: bonus.threshold,
          title: 'Threshold',
          oninput: e => { bonus.threshold = Number(e.target.value) || 0; } }),
        el('div', { class: 'row', style: 'margin:0;gap:6px' },
          el('input', { type: 'number', step: 'any', value: bonus.points,
            title: 'Bonus points', style: 'width:80px',
            oninput: e => { bonus.points = Number(e.target.value) || 0; } }),
          el('button', { class: 'btn ghost', type: 'button', title: 'Remove',
            onclick: () => { league.scoring_bonuses.splice(i, 1); renderBonuses(); } }, '×')))));
  }
  renderBonuses();
  $('#btnAddBonus').onclick = () => {
    league.scoring_bonuses.push({ stat: 'rush_yd', threshold: 100, points: 3 });
    renderBonuses();
  };

  // -- points-allowed bands -------------------------------------------
  league.scoring_bands = league.scoring_bands || {};
  function renderBands() {
    const bands = league.scoring_bands.dst_pa || [];
    $('#bandEditor').replaceChildren(
      ...bands.map((band, i) => el('div', { class: 'band-row' },
        el('span', { class: 'meta' },
          band.max === null || band.max === undefined
            ? `${(bands[i - 1]?.max ?? 0) + 1}+ points allowed`
            : (i === 0 ? `0 to ${band.max}` : `${(bands[i - 1].max ?? 0) + 1} to ${band.max}`)),
        el('input', { type: 'number', step: 'any', value: band.points,
          oninput: e => { band.points = Number(e.target.value) || 0; } }),
        el('button', { class: 'btn ghost', type: 'button', title: 'Remove band',
          onclick: () => { bands.splice(i, 1); renderBands(); } }, '×'))),
      el('button', { class: 'btn ghost', type: 'button', onclick: () => {
        if (!league.scoring_bands.dst_pa) league.scoring_bands.dst_pa = [];
        const list = league.scoring_bands.dst_pa;
        const last = list[list.length - 1];
        if (last && last.max === null) list.splice(list.length - 1, 0,
          { max: (list[list.length - 2]?.max ?? 0) + 7, points: 0 });
        else list.push({ max: null, points: 0 });
        renderBands();
      } }, '+ Add band'));
  }
  renderBands();

  // -- waivers / playoffs ---------------------------------------------
  league.waivers = league.waivers || {};
  league.playoffs = league.playoffs || {};
  const wTypeSel = $('#wType');
  wTypeSel.value = league.waivers.type || 'continual_rolling';
  wTypeSel.onchange = e => { league.waivers.type = e.target.value; };
  bind('#wDays', league.waivers.waiver_days, v => { league.waivers.waiver_days = Number(v) || 0; });
  bind('#wProcess', league.waivers.process, v => { league.waivers.process = v; });
  bind('#pTeams', league.playoffs.teams, v => { league.playoffs.teams = Number(v) || 0; });
  bind('#pWeeks', (league.playoffs.weeks || []).join(', '), v => {
    league.playoffs.weeks = v.split(',').map(s => Number(s.trim())).filter(Boolean);
  });
  bind('#tDeadline', league.trades.deadline_week,
       v => { league.trades.deadline_week = Number(v) || null; });

  // -- roster slots --------------------------------------------------
  $('#rosterEditor').replaceChildren(...league.roster.slots.map(slot =>
    el('div', { class: 'slotrow' },
      el('b', {}, slot.slot),
      el('input', { type: 'number', min: '0', value: slot.count,
        oninput: e => { slot.count = Number(e.target.value) || 0; } }),
      el('span', { class: 'meta' }, (slot.eligible || []).join(' / ')))),
    el('div', { class: 'slotrow' },
      el('b', {}, 'Bench'),
      el('input', { type: 'number', min: '0', value: league.roster.bench,
        oninput: e => { league.roster.bench = Number(e.target.value) || 0; } }),
      el('span', { class: 'meta' }, 'reserve spots')));

  // -- trade rules ---------------------------------------------------
  const t = league.trades;
  $('#tradeEditor').replaceChildren(
    el('label', {}, 'Approval',
      el('select', { onchange: e => { t.approval = e.target.value; } },
        ...['commissioner', 'league_vote', 'instant'].map(v =>
          el('option', { value: v, selected: t.approval === v }, v.replace('_', ' '))))),
    el('label', {}, 'Trade deadline (week)',
      el('input', { type: 'number', min: '0', max: '18', value: t.deadline_week ?? '',
        oninput: e => { t.deadline_week = Number(e.target.value) || null; } })),
    el('label', {}, 'Max players per side',
      el('input', { type: 'number', min: '1', max: '10', value: t.max_players_per_side,
        oninput: e => { t.max_players_per_side = Number(e.target.value) || 1; } })),
    el('label', {}, 'Max value gap (%)',
      el('input', { type: 'number', min: '0', max: '100', value: t.fairness.max_value_gap_pct,
        oninput: e => { t.fairness.max_value_gap_pct = Number(e.target.value) || 0; } })),
    el('label', {}, 'Allow uneven (2-for-1)',
      el('select', { onchange: e => { t.allow_uneven = e.target.value === 'yes'; } },
        el('option', { value: 'yes', selected: t.allow_uneven }, 'yes'),
        el('option', { value: 'no', selected: !t.allow_uneven }, 'no'))),
    el('label', {}, 'Both teams must improve',
      el('select', { onchange: e => { t.fairness.require_both_improve = e.target.value === 'yes'; } },
        el('option', { value: 'yes', selected: t.fairness.require_both_improve }, 'yes'),
        el('option', { value: 'no', selected: !t.fairness.require_both_improve }, 'no'))));

  // -- teams ---------------------------------------------------------
  const teamBox = $('#teamEditor');
  const myTeamSel = $('#myTeam');

  function renderTeams() {
    teamBox.replaceChildren(...league.teams.map((team, i) =>
      el('div', { class: 'team-row' },
        el('input', { type: 'text', value: team.name,
          oninput: e => { team.name = e.target.value; } }),
        el('input', { type: 'text', value: team.manager || '', placeholder: 'Manager',
          oninput: e => { team.manager = e.target.value; } }),
        el('button', { type: 'button', title: 'Remove', onclick: () => {
          league.teams.splice(i, 1); renderTeams();
        } }, '×'))),
      el('button', { class: 'btn ghost', type: 'button', onclick: () => {
        league.teams.push({ id: `team-${league.teams.length + 1}`, name: '', manager: '' });
        renderTeams();
      } }, '+ Add team'));

    myTeamSel.replaceChildren(el('option', { value: '' }, '—'),
      ...league.teams.map(t2 => el('option', { value: t2.id, selected: league.my_team_id === t2.id }, t2.name)));
  }
  renderTeams();
  renderOrder();
  myTeamSel.onchange = e => { league.my_team_id = e.target.value || null; };

  // -- save ----------------------------------------------------------
  $('#btnSave').onclick = async () => {
    // Ids are derived from names, so keep them in sync for new rows.
    league.teams.forEach(team => {
      if (!team.id || team.id.startsWith('team-')) {
        team.id = team.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
                  || team.id;
      }
    });
    league.team_count = league.teams.length || league.team_count;

    try {
      const { league: saved } = await api(`/api/league/${league.id}`, {
        method: 'PUT', body: { league },
      });
      State.league = saved;
      showProblems('#saveProblems', []);
      startCountdown();
      toast('League saved.');
    } catch (err) {
      showProblems('#saveProblems', [err.message]);
    }
  };
}

boot().catch(err => {
  $('#main').replaceChildren(el('div', { class: 'empty' },
    el('h3', {}, 'Could not start'), el('p', {}, err.message)));
});
