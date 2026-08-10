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
  ({ draft: viewDraft, team: viewTeam, trades: viewTrades,
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
  const { league } = await api(`/api/league/${id}`);
  State.league = league;
  $('#nav').hidden = false;
  $('#leaguePicker').hidden = false;
  $('#leaguePicker').value = id;
  setView(league.teams?.length ? 'draft' : 'settings');
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

async function viewTeam() {
  mount('#tpl-team');
  const id = State.league.id;
  const picker = $('#teamPicker');
  picker.replaceChildren(...(State.league.teams || [])
    .map(t => el('option', { value: t.id }, t.name)));
  picker.value = State.league.my_team_id || State.league.teams?.[0]?.id || '';
  picker.onchange = load;
  await load();

  async function load() {
    let data;
    try {
      data = await api(`/api/league/${id}/lineup?team=${encodeURIComponent(picker.value)}`);
    } catch (err) { toast(err.message, true); return; }

    $('#lineupTotal').textContent = `${num(data.projected)} pts`;
    $('#lineup').replaceChildren(...data.lineup.map(row =>
      el('div', { class: 'prow' },
        el('span', { class: 'slotname' }, row.slot),
        row.player ? el('span', { class: `pos ${row.player.pos}` }, row.player.pos)
                   : el('span', {}),
        el('div', {}, row.player ? row.player.name : '—'),
        el('span', { class: 'num2' }, row.player ? num(row.player.points, 0) : ''))));

    $('#benchList').replaceChildren(...(data.bench.length
      ? data.bench.map(p => el('div', { class: 'prow' },
          el('span', { class: `pos ${p.pos}` }, p.pos),
          el('div', {}, p.name),
          el('span', { class: 'num2' }, num(p.points, 0)),
          el('span', {})))
      : [el('div', { class: 'hint' }, 'No bench players.')]));
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
 * SETTINGS
 * ================================================================== */

async function viewSettings() {
  mount('#tpl-settings');
  const league = structuredClone(State.league);

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
