/* Equity Universe dashboard - plain JavaScript, no build step, no external libraries.
   Reads the static JSON files the daily job writes under ./data/. */
(function () {
  'use strict';

  var DAY = 86400000;
  var $ = function (id) { return document.getElementById(id); };

  var state = {
    index: null, status: null, runs: [],
    snap: null, week: null, cache: {},
    view: 'universe',
    sort: { key: null, dir: 1 },
    filters: { q: '', country: '', sector: '', exch: '', missing: '', issues: false },
    groups: loadGroups(),
    selected: null,
    cols: [],
    rowCache: { layout: null, els: {} }
  };

  var GROUPS = [
    { id: 'class', label: 'Classification' },
    { id: 'price', label: 'Price' },
    { id: 'eps', label: 'Earnings per share' },
    { id: 'val', label: 'Valuation' },
    { id: 'div', label: 'Dividends' },
    { id: 'score', label: 'Scores' }
  ];

  var DATASETS = {
    q: { label: 'Price & dividends', source: 'Yahoo Finance', none: 'Yahoo Finance has no price history for this symbol (renamed or delisted?)' },
    f: { label: 'Fundamentals & valuation', source: 'Yahoo Finance', none: 'Yahoo Finance has no financial statements for this symbol' },
    p: { label: 'Sector & industry', source: 'Yahoo Finance', none: 'Yahoo Finance lists no sector for this symbol' },
    e: { label: 'EPS estimates', source: 'Nasdaq', none: 'Not covered - Nasdaq publishes analyst forecasts for exchange-listed stocks only' },
    a: { label: 'Adjusted EPS, last fiscal year', source: 'Nasdaq', none: 'Not available - Nasdaq publishes analyst figures for exchange-listed stocks only' },
    n: { label: 'Listing & country', source: 'Nasdaq', none: 'Not listed on NYSE or Nasdaq' }
  };

  var UNIT_NOTE = 'Check before relying on this: the per-share figures for this ticker do not reconcile with Yahoo\u2019s own P/E (a one-off item, or an ADR-ratio or currency mismatch in the source).';
  var DIV_NOTE = 'Check before relying on this: the last 12 months of dividends look very different from the year before (a one-off special payment, or holes in Yahoo\u2019s dividend history).';
  var Z_EST_NOTE = 'This company does not report retained earnings separately in the source, so total reserves (equity less paid-in capital) were used. That tends to raise the score slightly.';
  function cfYieldNote(row) {
    return 'From the cash-flow statement: dividends paid (' + (row.dy_cf_b ? basisWords(row.dy_cf_b) : 'latest year') + ') \u00f7 market value. Used because Yahoo\u2019s per-share dividend history for this ticker stops on ' +
      (row.div_last ? fmtDay(row.div_last) : 'an earlier date') + '.';
  }
  function oldGrowthNote(row) {
    return 'Measured up to the last dividend on record (' + (row.div_last ? fmtDay(row.div_last) : 'over a year ago') + '). Yahoo has nothing more recent for this ticker.';
  }
  var STATUS_WORDS = { ok: 'Healthy', degraded: 'Needs a look', failed: 'Failing', down: 'Down', stale: 'Out of date', missing: 'Missing' };
  var F_TESTS = ['Positive net income', 'Positive operating cash flow', 'Return on assets improved', 'Cash flow exceeds net income',
    'Long-term debt ratio fell', 'Current ratio improved', 'No new shares issued', 'Gross margin improved', 'Asset turnover improved'];
  var Z_PARTS = ['Working capital / total assets', 'Retained earnings / total assets', 'EBIT / total assets',
    'Market value of equity / total liabilities', 'Revenue / total assets'];

  // ------------------------------------------------------------------ DOM helpers
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v == null || v === false) return;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'on') Object.keys(v).forEach(function (ev) { el.addEventListener(ev, v[ev]); });
        else el.setAttribute(k, v === true ? '' : v);
      });
    }
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, kid) {
    if (kid == null || kid === false) return;
    if (Array.isArray(kid)) kid.forEach(function (k) { add(el, k); });
    else el.appendChild(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function svg(tag, attrs) {
    var el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attrs || {}).forEach(function (k) { if (attrs[k] != null) el.setAttribute(k, attrs[k]); });
    for (var i = 2; i < arguments.length; i++) if (arguments[i] != null) el.appendChild(arguments[i].nodeType ? arguments[i] : document.createTextNode(String(arguments[i])));
    return el;
  }

  // ------------------------------------------------------------------ formatting
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function fixed(v, d) { return v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function fmtNum(v, d) { return isNum(v) ? fixed(v, d == null ? 2 : d) : null; }
  function fmtPct(v, d) { return isNum(v) ? fixed(v * 100, d == null ? 2 : d) + '%' : null; }
  function fmtMoney(v, cur) {
    if (!isNum(v)) return null;
    var a = Math.abs(v), sign = v < 0 ? '−' : '', sym = !cur || cur === 'USD' ? '$' : cur + ' ';
    var out;
    if (a >= 1e12) out = fixed(a / 1e12, 2) + 'T';
    else if (a >= 1e10) out = fixed(a / 1e9, 1) + 'B';
    else if (a >= 1e9) out = fixed(a / 1e9, 2) + 'B';
    else if (a >= 1e6) out = fixed(a / 1e6, a >= 1e8 ? 0 : 1) + 'M';
    else out = fixed(a, 0);
    return sign + sym + out;
  }
  function fmtSigned(v, d) { return isNum(v) ? (v > 0 ? '+' : v < 0 ? '−' : '') + fixed(Math.abs(v) * 100, d == null ? 1 : d) + '%' : null; }
  function parseDay(iso) { var p = String(iso).slice(0, 10).split('-'); return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])); }
  function fmtDay(iso, withWeekday) {
    var o = { timeZone: 'UTC', month: 'short', day: 'numeric', year: 'numeric' };
    if (withWeekday) o.weekday = 'short';
    return parseDay(iso).toLocaleDateString('en-US', o);
  }
  function fmtStamp(ts) {
    var d = new Date(ts);
    if (isNaN(d)) return 'unknown';
    return d.toLocaleString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }
  function ago(ts) {
    var ms = Date.now() - new Date(ts).getTime();
    if (!isFinite(ms)) return '';
    var hrs = ms / 3600000;
    if (hrs < 1) return 'less than an hour ago';
    if (hrs < 36) return Math.round(hrs) + ' hours ago';
    return Math.round(hrs / 24) + ' days ago';
  }
  function isoDay(d) { return d.toISOString().slice(0, 10); }

  // ------------------------------------------------------------------ row accessors
  function override(row, key) { return row.ovr && row.ovr[key] ? row.ovr[key] : null; }
  function get(row, key) { var o = override(row, key); return o ? o[0] : row[key]; }
  function epsAt(row, year) {
    var o = override(row, 'eps_' + year);
    if (o) return { v: o[0], kind: 'm', note: o[1] };
    if (row.eps_a && isNum(row.eps_a[year])) return { v: row.eps_a[year], kind: 'a' };
    if (row.eps_e && row.eps_e[year]) return { v: row.eps_e[year][0], kind: 'e', n: row.eps_e[year][1] };
    return null;
  }
  function adjEps(row) {
    var years = Object.keys(row.eps_adj || {});
    return years.length ? { year: years[0], v: row.eps_adj[years[0]] } : null;
  }
  function stCode(row, ds) { return (row.st && row.st[ds]) || ''; }
  function isStale(row, ds) { return stCode(row, ds).indexOf('s:') === 0; }
  function hasIssue(row) {
    if (!row.st) return false;
    return Object.keys(row.st).some(function (k) { var c = row.st[k]; return c === 'm' || c.indexOf('s:') === 0; });
  }
  function staleTitle(row, ds) {
    return 'Carried over from ' + fmtDay(stCode(row, ds).slice(2), true) + ' - the latest fetch from ' + DATASETS[ds].source + ' failed.';
  }

  // ------------------------------------------------------------------ columns
  function buildColumns(snap) {
    var cur = function (r) { return get(r, 'cur') || 'USD'; };
    var cols = [
      { key: 's', label: 'Symbol', group: 'id', cls: 'sym sticky', text: true },
      { key: 'name', label: 'Company', group: 'id', cls: 'name sticky sticky-2', text: true, ds: 'q' },
      { key: 'country', label: 'Country', group: 'class', text: true, cls: 'clip' },
      { key: 'sector', label: 'Sector', group: 'class', text: true, cls: 'clip', ds: 'p' },
      { key: 'industry', label: 'Industry', group: 'class', text: true, cls: 'clip', ds: 'p' },
      { key: 'exch', label: 'Exchange', group: 'class', text: true, ds: 'q' },
      { key: 'price', label: 'Price', group: 'price', ds: 'q', fmt: function (v) { return fmtNum(v, 2); },
        title: 'Closing price for the day (in the currency the stock trades in)' },
      { key: 'chg1d', label: '1 day', group: 'price', ds: 'q', delta: true, fmt: function (v) { return fmtSigned(v, 1); },
        title: 'Price change since the previous trading day\u2019s close' },
      { key: 'chg1w', label: '1 wk', group: 'price', ds: 'q', delta: true, fmt: function (v) { return fmtSigned(v, 1); },
        title: 'Price change over the last week (since the close a week earlier)' }
    ];
    (snap.eps_years || []).forEach(function (y) {
      cols.push({ key: 'eps_' + y, label: String(y), group: 'eps', eps: y,
        title: 'Earnings per share for the fiscal year ending in ' + y + '. Upright = reported (GAAP diluted). Italic = analyst consensus.' });
    });
    cols.push(
      { key: 'eps_adj', label: 'Last FY adj.', group: 'eps', ds: 'a', adj: true,
        title: 'The latest reported fiscal year\u2019s EPS on the analysts\u2019 (adjusted) basis - the like-for-like starting point for the estimates. Exchange-listed tickers only; for REITs analysts quote funds from operations.' },
      { key: 'feps', label: 'Next FY*', group: 'eps', ds: 'f', fmt: function (v) { return fmtNum(v, 2); }, est: true,
        title: 'Next-fiscal-year consensus EPS implied by Yahoo’s forward P/E (price ÷ forward P/E). Covers OTC tickers that have no Nasdaq forecast.' },
      { key: 'mcap', label: 'Market cap', group: 'val', ds: 'f', fmt: function (v, r) { return fmtMoney(v, cur(r)); } },
      { key: 'ev', label: 'Enterprise value', group: 'val', ds: 'f', fmt: function (v, r) { return fmtMoney(v, cur(r)); } },
      { key: 'ebitda', label: 'EBITDA', group: 'val', ds: 'f', fmt: function (v, r) { return fmtMoney(v, cur(r)); },
        title: 'Trailing twelve months, excluding unusual items' },
      { key: 'fcf', label: 'Free cash flow', group: 'val', ds: 'f', fmt: function (v, r) { return fmtMoney(v, cur(r)); },
        title: 'Trailing twelve months: operating cash flow minus capital expenditure' },
      { key: 'dy', label: 'Div. yield', group: 'div', ds: 'q', fmt: function (v) { return fmtPct(v, 2); },
        title: 'Dividends paid over the last twelve months ÷ price' },
      { key: 'dg5', label: '5-yr growth', group: 'div', ds: 'q', fmt: function (v) { return fmtSigned(v, 1); },
        title: 'Annualised growth of the dividend over the last five years' },
      { key: 'z', label: 'Altman Z', group: 'score', ds: 'f', zone: true, fmt: function (v) { return fmtNum(v, 2); },
        title: 'Bankruptcy-risk score calculated from the financial statements. Above 2.99 safe, below 1.81 distress. Not meaningful for banks and insurers.' },
      { key: 'f', label: 'Piotroski F', group: 'score', ds: 'f', fmt: function (v) { return fmtNum(v, 0); },
        title: 'Nine pass/fail tests of financial strength (0-9), latest fiscal year against the one before' }
    );
    return cols;
  }
  function visibleCols() {
    return state.cols.filter(function (c) { return c.group === 'id' || state.groups[c.group] !== false; });
  }
  function sortValue(row, col) {
    if (col.eps) { var e = epsAt(row, col.eps); return e ? e.v : null; }
    if (col.adj) { var a = adjEps(row); return a ? a.v : null; }
    var v = get(row, col.key);
    return v == null || v === '' ? null : v;
  }
  function loadGroups() {
    try { return JSON.parse(localStorage.getItem('eu.groups') || '{}') || {}; } catch (e) { return {}; }
  }
  function saveGroups() { try { localStorage.setItem('eu.groups', JSON.stringify(state.groups)); } catch (e) { /* private mode */ } }

  // ------------------------------------------------------------------ data loading
  function getJSON(path, bust) {
    return fetch(path + '?v=' + encodeURIComponent(bust || Date.now()), { cache: 'no-store' }).then(function (res) {
      if (!res.ok) throw new Error(path + ' (HTTP ' + res.status + ')');
      return res.json();
    });
  }
  function indexEntry(week) {
    return ((state.index && state.index.snapshots) || []).filter(function (s) { return s.as_of === week; })[0];
  }
  function loadSnapshot(week) {
    if (state.cache[week]) return Promise.resolve(state.cache[week]);
    var entry = indexEntry(week) || {};
    return getJSON('data/snapshots/' + week + '.json', entry.updated_at || week).then(function (snap) {
      state.cache[week] = snap;
      return snap;
    });
  }

  function init() {
    wire();
    Promise.all([
      getJSON('data/index.json').catch(function () { return null; }),
      getJSON('data/status.json').catch(function () { return null; }),
      getJSON('data/runs.json').catch(function () { return []; })
    ]).then(function (res) {
      state.index = res[0];
      state.status = res[1];
      state.runs = Array.isArray(res[2]) ? res[2] : [];
      var weeks = ((state.index && state.index.snapshots) || []).map(function (s) { return s.as_of; });
      renderHealthPill();
      if (!weeks.length) {
        $('subtitle').textContent = 'No data published yet';
        renderBanners();
        renderHealth();
        clear($('tiles'));
        ['.toolbar', '.key', '#week-control'].forEach(function (sel) { document.querySelector(sel).hidden = true; });
        $('empty').hidden = false;
        $('empty').textContent = 'No snapshot has been published yet. The first scheduled refresh will create one.';
        return;
      }
      var wanted = (location.hash.match(/(?:day|week)=(\d{4}-\d{2}-\d{2})/) || [])[1];
      fillWeekSelect(weeks);
      showWeek(weeks.indexOf(wanted) >= 0 ? wanted : weeks[0]);
      if (/view=health/.test(location.hash)) setView('health');
    }).catch(fatal);
  }
  function fatal(err) {
    $('subtitle').textContent = 'Could not load data';
    clear($('banners')).appendChild(banner('critical', 'The dashboard could not load its data.',
      String(err && err.message || err) + ' - reload the page; if this keeps happening the data files may be missing or damaged.'));
  }

  function showWeek(week) {
    $('table-wrap').style.opacity = '0.5';   // hold the previous frame while loading
    return loadSnapshot(week).then(function (snap) {
      state.week = week;
      state.snap = snap;
      state.cols = buildColumns(snap);
      $('week-select').value = week;
      var weeks = state.index.snapshots.map(function (s) { return s.as_of; });
      var i = weeks.indexOf(week);
      $('week-next').disabled = i <= 0;
      $('week-prev').disabled = i >= weeks.length - 1;
      setHash();
      fillFilterOptions();
      renderSubtitle();
      renderBanners();
      renderTiles();
      renderGroupToggles();
      renderTable();
      renderHealth();
      if (state.selected) {
        var row = snap.rows.filter(function (r) { return r.s === state.selected; })[0];
        if (row) openDrawer(row); else closeDrawer();
      }
      $('table-wrap').style.opacity = '';
    }).catch(function (err) { $('table-wrap').style.opacity = ''; fatal(err); });
  }
  function setHash() {
    var parts = [];
    if (state.index && state.week && state.week !== state.index.latest) parts.push('day=' + state.week);
    if (state.view === 'health') parts.push('view=health');
    history.replaceState(null, '', parts.length ? '#' + parts.join('&') : location.pathname + location.search);
  }

  // ------------------------------------------------------------------ header, banners, tiles
  function fillWeekSelect(weeks) {
    var sel = clear($('week-select'));
    weeks.forEach(function (w, i) {
      sel.appendChild(h('option', { value: w, text: 'Close of ' + fmtDay(w, true) + (i === 0 ? '  (latest)' : '') }));
    });
  }
  function renderSubtitle() {
    var snap = state.snap;
    var text = snap.rows.length.toLocaleString('en-US') + ' tickers \u00b7 close of ' + fmtDay(snap.as_of, true);
    if (snap.updated_at) text += ' · data fetched ' + fmtStamp(snap.updated_at);
    $('subtitle').textContent = text;
  }
  function banner(level, title, detail, action) {
    var icon = level === 'critical' ? '✖' : level === 'info' ? 'ℹ' : '⚠';
    var parts = detail ? String(detail).split(' | ') : [];
    var body = parts.length > 1 ? h('ul', { class: 'plain' }, parts.map(function (p) { return h('li', { text: p }); }))
      : detail ? h('p', { text: detail }) : null;
    return h('div', { class: 'banner ' + level, role: level === 'info' ? 'status' : 'alert' },
      h('span', { class: 'icon', 'aria-hidden': 'true', text: icon }),
      h('div', null, h('strong', { text: title }), body, action || null));
  }
  function healthLink(text) {
    return h('button', { type: 'button', class: 'link-btn', text: text || 'See what failed and how to fix it', on: { click: function () { setView('health'); } } });
  }
  /* Checks the page can make without the job's help: a job that has stopped cannot report its own death. */
  function clientChecks() {
    var out = [], st = state.status, now = new Date();
    if (st && st.last_check) {
      var hours = (now - new Date(st.last_check)) / 3600000;
      if (hours > 26) {
        out.push({ level: hours > 50 ? 'critical' : 'warning',
          title: 'The scheduled job has not checked in for ' + (hours < 48 ? Math.round(hours) + ' hours.' : Math.round(hours / 24) + ' days.'),
          detail: 'It should run three times every morning (last seen ' + fmtStamp(st.last_check) + '). The schedule may be paused or the job may be failing to start.' });
      }
    }
    var latest = state.index && state.index.latest;
    var next = st && st.next_session;
    if (latest && next && latest < next) {
      // the close of `next` should be on the page by 10:00 New York (14:00/15:00 UTC) the following morning
      var due = parseDay(next).getTime() + DAY + 15 * 3600000;
      var overdue = (now.getTime() - due) / 3600000;
      if (overdue > 0) {
        out.push({ level: overdue > 24 ? 'critical' : 'warning',
          title: 'No data yet for the close of ' + fmtDay(next, true) + '.',
          detail: 'The newest snapshot is for ' + fmtDay(latest, true) + '. The morning refresh has not completed.' });
      }
    }
    return out;
  }
  function handFiles() {
    var m = (state.status || {}).manual || {}, limit = m.stale_after_days || 35;
    return (m.files || []).map(function (f) {
      var from = f.oldest || f.as_of;        // judge a file by its oldest figure
      var days = from ? Math.floor((Date.now() - parseDay(from).getTime()) / DAY) : null;
      return { file: f.file, values: f.values, tickers: f.tickers, fields: f.fields || [], as_of: f.as_of, oldest: from, undated: f.undated,
        days: days, old: days != null && days > limit, limit: limit };
    });
  }
  function overallLevel() {
    var st = state.status || {};
    var checks = clientChecks();
    if (st.overall === 'failed' || checks.some(function (c) { return c.level === 'critical'; })) return 'failed';
    if (st.overall === 'degraded' || checks.length) return 'degraded';
    return st.overall ? 'ok' : 'unknown';
  }
  function renderBanners() {
    var box = clear($('banners')), st = state.status;
    clientChecks().forEach(function (c) { box.appendChild(banner(c.level, c.title, c.detail, healthLink('Open data health'))); });
    var viewingLatest = !state.index || !state.week || state.week === state.index.latest;
    var tail = viewingLatest ? ' - some figures below are from an earlier day.' : ' (this affects the latest day, not the one shown).';
    if (st && st.overall === 'failed') {
      box.appendChild(banner('critical', 'The last refresh failed' + tail, st.headline, healthLink()));
    } else if (st && st.overall === 'degraded') {
      box.appendChild(banner('warning', 'The last refresh was incomplete' + tail, st.headline, healthLink()));
    }
    if (state.index && state.week && state.week !== state.index.latest) {
      box.appendChild(banner('info', 'You are looking at an earlier day: the close of ' + fmtDay(state.week, true) + '.', null,
        h('button', { type: 'button', class: 'link-btn', text: 'Back to the latest day', on: { click: function () { showWeek(state.index.latest); } } })));
    }
  }
  function renderHealthPill() {
    var level = overallLevel(), pill = $('health-pill');
    if (level === 'unknown') { pill.hidden = true; return; }
    pill.hidden = false;
    pill.className = 'pill s-' + level;
    clear(pill);
    add(pill, [h('span', { class: 'dot' }), level === 'ok' ? 'OK' : level === 'failed' ? 'Failing' : 'Check']);
  }

  function tile(label, value, small, notes, staleSince) {
    var el = h('div', { class: 'tile' }, h('div', { class: 'label', text: label }));
    var val = h('div', { class: 'value' }, value == null ? '—' : value);
    if (small) val.appendChild(h('small', { text: small }));
    el.appendChild(val);
    (notes || []).forEach(function (n) { if (n) el.appendChild(h('div', { class: 'note' }, n)); });
    if (staleSince) {
      el.appendChild(h('div', { class: 'note' }, h('span', { class: 'mark stale-mark', 'aria-hidden': 'true', text: '◷ ' }),
        'Not refreshed today - value from ' + fmtDay(staleSince, true)));
    }
    return el;
  }
  function renderTiles() {
    var box = clear($('tiles')), m = state.snap.macro || {}, st = m.st || {};
    var since = function (k) { return st[k] && st[k].indexOf('s:') === 0 ? st[k].slice(2) : null; };

    var sp = m.sp500;
    if (sp && sp.headline) {
      var price = (sp.cagr && sp.cagr.price) || {}, tr = (sp.cagr && sp.cagr.tr) || {}, hy = String(sp.headline.years);
      var other = Object.keys(price).filter(function (y) { return y !== hy; }).sort(function (a, b) { return a - b; })
        .map(function (y) { return y + '-yr ' + fmtPct(price[y], 1); }).join(' · ');
      box.appendChild(tile('S&P 500 growth (' + hy + '-year CAGR)', fmtPct(sp.headline.value, 1), 'a year',
        [isNum(tr[hy]) ? ['With dividends reinvested: ', h('b', { text: fmtPct(tr[hy], 1) })] : null, other,
          'Index ' + fmtNum(sp.level, 0) + ' on ' + fmtDay(sp.date)], since('sp500')));
    } else box.appendChild(tile('S&P 500 growth (CAGR)', null, null, ['Not available'], null));

    var t = m.treasury_10y;
    box.appendChild(tile('10-year Treasury yield', t ? fmtNum(t.value, 2) + '%' : null, null,
      t ? [fmtDay(t.date) + ' · ' + t.source] : ['Not available'], since('treasury_10y')));

    var c = m.cpi;
    box.appendChild(tile('Inflation (CPI, year over year)', c ? fmtPct(c.yoy, 1) : null, null,
      c ? [parseDay(c.month + '-01').toLocaleDateString('en-US', { timeZone: 'UTC', month: 'long', year: 'numeric' }) + ' · ' + c.source, c.note || null]
        : ['Not available'], since('cpi')));

    var level = overallLevel(), s = state.status || {};
    var health = h('div', { class: 'tile' }, h('div', { class: 'label', text: 'Data health' }),
      h('div', { class: 'status-line s-' + level }, h('span', { class: 'dot' }),
        level === 'ok' ? 'All sources healthy' : level === 'failed' ? 'Refresh is failing' : level === 'degraded' ? 'Needs a look' : 'Unknown'),
      h('div', { class: 'note', text: s.last_check ? 'Job last ran ' + fmtStamp(s.last_check) + ' (' + ago(s.last_check) + ')' : 'No run recorded yet' }),
      (s.attention || []).length ? h('div', { class: 'note', text: s.attention.length + ' ticker' + (s.attention.length === 1 ? ' needs' : 's need') + ' a look (renamed or no longer trading?): ' +
        s.attention.slice(0, 4).map(function (a) { return a.symbol; }).join(', ') + (s.attention.length > 4 ? '\u2026' : '') }) : null,
      (function () {
        var old = handFiles().filter(function (f) { return f.old; });
        return old.length ? h('div', { class: 'note', text: 'Hand-entered figures are getting old: ' + old.map(function (f) { return f.file.replace('manual/', '') + ' (' + f.days + ' days)'; }).join(', ') }) : null;
      })(),
      h('div', { class: 'note' }, healthLink('Open data health')));
    box.appendChild(health);
  }

  // ------------------------------------------------------------------ filters + table
  function fillFilterOptions() {
    var rows = state.snap.rows;
    function fill(id, key, all) {
      var sel = $(id), keep = state.filters[key];
      var counts = {};
      rows.forEach(function (r) { var v = get(r, key === 'exch' ? 'exch' : key); if (v) counts[v] = (counts[v] || 0) + 1; });
      clear(sel).appendChild(h('option', { value: '', text: all }));
      Object.keys(counts).sort().forEach(function (v) { sel.appendChild(h('option', { value: v, text: v + ' (' + counts[v] + ')' })); });
      if (rows.some(function (r) { return !get(r, key); })) sel.appendChild(h('option', { value: '__none', text: 'Not classified' }));
      sel.value = keep;
      if (sel.value !== keep) state.filters[key] = '';
    }
    var miss = clear($('f-missing'));
    MISSING.forEach(function (m) { miss.appendChild(h('option', { value: m[0], text: m[1] })); });
    miss.value = state.filters.missing;
    fill('f-country', 'country', 'All countries');
    fill('f-sector', 'sector', 'All sectors');
    fill('f-exch', 'exch', 'All exchanges');
  }
  var MISSING = [
    ['', 'Any row'], ['eps_e', 'No EPS estimates'], ['eps_adj', 'No adjusted EPS'], ['z', 'No Altman Z'], ['f', 'No Piotroski F'],
    ['dy', 'No dividend yield'], ['dg5', 'No dividend growth'], ['ev', 'No enterprise value'], ['ebitda', 'No EBITDA'],
    ['country', 'No country'], ['check', 'Marked ? to check'], ['hand', 'Has hand-entered figures']
  ];
  function isMissing(row, what) {
    if (what === 'check') return !!(row.unit_flag || row.div_odd);
    if (what === 'hand') return !!row.ovr;
    if (what === 'eps_e') {
      var y0 = +state.snap.as_of.slice(0, 4);
      return !(state.snap.eps_years || []).some(function (y) { var e = epsAt(row, y); return y >= y0 && e && e.kind !== 'a'; });
    }
    if (what === 'eps_adj') return !adjEps(row);
    var v = get(row, what);
    return v == null || v === '';
  }
  function filteredRows() {
    var f = state.filters, q = f.q.trim().toLowerCase();
    var rows = state.snap.rows.filter(function (r) {
      if (q && r.s.toLowerCase().indexOf(q) < 0 && String(get(r, 'name') || '').toLowerCase().indexOf(q) < 0) return false;
      var ok = ['country', 'sector', 'exch'].every(function (k) {
        if (!f[k]) return true;
        var v = get(r, k);
        return f[k] === '__none' ? !v : v === f[k];
      });
      if (!ok) return false;
      if (f.missing && !isMissing(r, f.missing)) return false;
      if (manage.hiddenNow[symbolKey(r.s)]) return false;      // just removed; the data catches up in a few minutes
      return !f.issues || hasIssue(r);
    });
    var col = state.cols.filter(function (c) { return c.key === state.sort.key; })[0];
    if (col) {
      var dir = state.sort.dir;
      rows = rows.map(function (r, i) { return { r: r, i: i, v: sortValue(r, col) }; }).sort(function (a, b) {
        if (a.v == null && b.v == null) return a.i - b.i;
        if (a.v == null) return 1;      // blanks always last
        if (b.v == null) return -1;
        if (a.v < b.v) return -dir;
        if (a.v > b.v) return dir;
        return a.i - b.i;
      }).map(function (x) { return x.r; });
    }
    return rows;
  }
  function renderGroupToggles() {
    var box = clear($('group-toggles'));
    GROUPS.forEach(function (g) {
      var on = state.groups[g.id] !== false;
      box.appendChild(h('button', { type: 'button', 'aria-pressed': String(on), text: g.label, title: (on ? 'Hide' : 'Show') + ' the ' + g.label + ' columns',
        on: { click: function () { state.groups[g.id] = !on; saveGroups(); renderGroupToggles(); renderTable(); } } }));
    });
  }
  function renderHead(cols) {
    var head = clear($('grid-head'));
    var groupsRow = h('tr', { class: 'groups' }), colsRow = h('tr', { class: 'cols' });
    var spans = [];
    cols.forEach(function (c) {
      var last = spans[spans.length - 1];
      if (last && last.group === c.group) last.n++; else spans.push({ group: c.group, n: 1 });
    });
    spans.forEach(function (s) {
      var g = GROUPS.filter(function (x) { return x.id === s.group; })[0];
      var label = s.group === 'eps' ? 'Earnings per share - reported, then analyst estimates' : g ? g.label : '';
      groupsRow.appendChild(h('th', { colspan: s.n, scope: 'colgroup', class: (s.group === 'id' ? 'sticky' : 'g-start'), text: label }));
    });
    var prev = null;
    cols.forEach(function (c) {
      var numeric = !c.text;
      var active = state.sort.key === c.key;
      var th = h('th', { scope: 'col', class: [(numeric ? 'num' : ''), c.cls && c.cls.indexOf('sticky') >= 0 ? c.cls.replace(/\b(sym|name|clip)\b/g, '') : '',
        prev !== null && prev !== c.group ? 'g-start' : ''].join(' ').trim(),
        'aria-sort': active ? (state.sort.dir > 0 ? 'ascending' : 'descending') : 'none' });
      th.appendChild(h('button', { type: 'button', class: 'sort', title: c.title || ('Sort by ' + c.label),
        on: { click: function () { toggleSort(c); } } },
        c.label, h('span', { class: 'arrow', 'aria-hidden': 'true', text: active ? (state.sort.dir > 0 ? '▲' : '▼') : '' })));
      colsRow.appendChild(th);
      prev = c.group;
    });
    head.appendChild(groupsRow);
    head.appendChild(colsRow);
  }
  function toggleSort(col) {
    if (state.sort.key !== col.key) state.sort = { key: col.key, dir: col.text ? 1 : -1 };   // numbers: biggest first
    else if (state.sort.dir === (col.text ? 1 : -1)) state.sort.dir = -state.sort.dir;
    else state.sort = { key: null, dir: 1 };                                                // third click: workbook order
    renderTable();
  }
  function cell(row, c, firstOfGroup) {
    var cls = [], kids = [], title = null;
    if (!c.text) cls.push('num');
    if (c.cls) cls.push(c.cls);
    if (firstOfGroup) cls.push('g-start');
    var manual = null, stale = false, check = false;

    if (c.eps) {
      var e = epsAt(row, c.eps);
      if (!e) { cls.push('na'); kids.push('—'); }
      else {
        kids.push(fmtNum(e.v, 2));
        if (e.kind === 'e') {
          cls.push('est');
          title = 'Analyst consensus' + (e.n ? ' (' + e.n + ' analyst' + (e.n === 1 ? '' : 's') + ')' : '') + ' · Nasdaq';
          stale = isStale(row, 'e');
          if (stale) title += '\n' + staleTitle(row, 'e');
        } else if (e.kind === 'a') {
          title = 'Reported (GAAP diluted) · Yahoo Finance' + (row.fcur ? ' · converted from ' + row.fcur : '');
          stale = isStale(row, 'f');
          if (stale) title += '\n' + staleTitle(row, 'f');
          if (row.unit_flag) { check = true; title += '\n' + UNIT_NOTE; }
        } else manual = e.note || '';
      }
    } else if (c.adj) {
      var adj = adjEps(row);
      if (!adj) { cls.push('na'); kids.push('\u2014'); }
      else {
        kids.push(fmtNum(adj.v, 2));
        title = 'Fiscal ' + adj.year + ', analysts\u2019 (adjusted) basis \u00b7 worked out from Nasdaq\u2019s forecast growth figures';
        stale = isStale(row, 'a');
        if (stale) title += '\n' + staleTitle(row, 'a');
      }
    } else {
      var o = override(row, c.key);
      var v = o ? o[0] : row[c.key];
      if (o) manual = o[1] || '';
      var text = v == null || v === '' ? null : c.fmt ? c.fmt(v, row) : String(v);
      if (text == null) {
        cls.push('na');
        kids.push('—');
        if (c.key === 'dy' && row.div_gap) title = 'Yahoo has no dividend on record since ' + fmtDay(row.div_last) + ' and none in the cash-flow statement, so no yield is shown (the company may still be paying).';
        else if (c.key === 'z' && row.z_miss) title = 'Cannot be calculated: the source has no ' + row.z_miss.join(', ') + ' for this company.';
        else if (c.key === 'f' && row.ft) title = 'Only ' + row.ft.replace(/-/g, '').length + ' of the 9 tests can be calculated for this company.';
      } else {
        if (c.zone && isNum(v)) {
          var zone = v > 2.99 ? 'ok' : v >= 1.81 ? 'degraded' : 'failed';
          kids.push(h('span', { class: 'dot zone s-' + zone, 'aria-hidden': 'true' }));
          title = v > 2.99 ? 'Safe zone (above 2.99)' : v >= 1.81 ? 'Grey zone (1.81 to 2.99)' : 'Distress zone (below 1.81)';
        }
        if (c.delta && isNum(v)) { cls.push(v > 0 ? 'up' : v < 0 ? 'down' : ''); text = (v > 0 ? '▲ ' : v < 0 ? '▼ ' : '') + text; }
        if (c.est) cls.push('est');
        kids.push(text);
        if (c.text) title = text;
        if (c.key === 'price' && row.plag) title = 'No trade on this day: this is the close from ' + (row.pdate ? fmtDay(row.pdate, true) : row.plag + ' trading days earlier') + '.';
        if (c.key === 'dy' && row.div_lapsed) title = 'Payments have slowed or stopped: this counts only what was paid in the last 12 months.';
        if ((c.key === 'dy' || c.key === 'dg5') && row.div_odd && !o) { check = true; title = DIV_NOTE; }
        if (!o && c.key === 'dy' && row.dy_src === 'cf') { cls.push('alt'); title = cfYieldNote(row); }
        if (!o && c.key === 'dg5' && row.dg5_old) { cls.push('alt'); title = oldGrowthNote(row); }
        if (!o && c.key === 'z' && row.z_est) { cls.push('alt'); title = (title ? title + '\n' : '') + Z_EST_NOTE; }
        if (c.key === 'mcap' && row.mcap_src === 'nq') title = 'From Nasdaq (Yahoo had no figure)';
        if (c.ds && !o && isStale(row, c.ds) && !(c.key === 'name' && !row.yname)) {
          stale = true;
          title = (title && !c.text ? title + '\n' : '') + staleTitle(row, c.ds);
        }
      }
    }
    if (check) kids.push(h('span', { class: 'mark check-mark', 'aria-label': 'check against the source', text: '?' }));
    if (stale) { cls.push('stale'); kids.push(h('span', { class: 'mark stale-mark', 'aria-label': 'carried over from an earlier day', text: '◷' })); }
    if (manual !== null) {
      kids.push(h('span', { class: 'mark', 'aria-label': 'typed in by hand', text: '✎' }));
      title = 'Entered by hand' + (manual ? ' \u00b7 ' + manual : '') + '\nThis figure is not refreshed automatically.';
    }
    return h('td', { class: cls.join(' '), title: title }, kids);
  }
  function renderTable() {
    if (!state.snap) return;
    var cols = visibleCols();
    renderHead(cols);
    var layout = state.week + '|' + cols.map(function (c) { return c.key; }).join(',');
    if (state.rowCache.layout !== layout) state.rowCache = { layout: layout, els: {} };   // new week or different columns
    var rows = filteredRows(), body = clear($('grid-body')), frag = document.createDocumentFragment();
    rows.forEach(function (row) {
      var tr = state.rowCache.els[row.s];
      if (!tr) {
        tr = h('tr', { 'data-s': row.s, tabindex: '0' });
        var prev = null;
        cols.forEach(function (c) {
          tr.appendChild(cell(row, c, prev !== null && prev !== c.group));
          prev = c.group;
        });
        state.rowCache.els[row.s] = tr;
      }
      tr.classList.toggle('selected', row.s === state.selected);
      frag.appendChild(tr);
    });
    body.appendChild(frag);
    $('empty').hidden = rows.length > 0;
    var total = state.snap.rows.length;
    $('row-count').textContent = rows.length === total ? total.toLocaleString('en-US') + ' tickers'
      : rows.length.toLocaleString('en-US') + ' of ' + total.toLocaleString('en-US') + ' tickers';
  }

  // ------------------------------------------------------------------ CSV export (workbook column order)
  function exportCsv() {
    var snap = state.snap, m = snap.macro || {}, years = snap.eps_years || [];
    var asOfYear = +snap.as_of.slice(0, 4);
    var head = ['Country', 'Sector', 'Industry', 'Symbol', 'Company Name', 'Price', 'CAGR of S&P 500', '10 Yr Treasury Yield', 'Inflation (CPI)']
      .concat(years.map(function (y) { return y + ' EPS' + (y >= asOfYear ? ' (estimate)' : ''); }))
      .concat(['Last FY EPS (adjusted)', 'Next FY EPS (implied)', 'Market Capitalization', 'Enterprise Value', 'EBITDA', 'Free Cash Flow', 'Exchange', 'Dividend Yield',
        '5 Year Dividend Growth Rate', 'Altman Z-Score', 'Piotroski F-Score', 'As of', 'Notes']);
    var sp = m.sp500 && m.sp500.headline ? m.sp500.headline.value : '';
    var lines = [head];
    filteredRows().forEach(function (r) {
      var notes = [];
      Object.keys(r.st || {}).forEach(function (ds) {
        var c = r.st[ds];
        if (c.indexOf('s:') === 0) notes.push(DATASETS[ds].label + ' carried over from ' + c.slice(2));
        else if (c === 'm') notes.push(DATASETS[ds].label + ' unavailable');
      });
      if (r.ovr) notes.push('Entered by hand: ' + Object.keys(r.ovr).join(' '));
      if (r.unit_flag) notes.push('Check per-share figures against the source');
      if (r.div_odd) notes.push('Check dividend figures against the source');
      if (r.dy_src === 'cf') notes.push('Dividend yield from the cash-flow statement');
      else if (r.div_gap) notes.push('No dividend on record at Yahoo since ' + r.div_last);
      if (r.dg5_old) notes.push('Dividend growth measured to ' + r.div_last);
      if (r.z_est) notes.push('Z-score uses total reserves for retained earnings');
      if (adjEps(r)) notes.push('Adjusted EPS is for fiscal ' + adjEps(r).year);
      if (r.plag) notes.push('Last traded ' + (r.pdate || (r.plag + ' trading days earlier')));
      var kinds = years.map(function (y) { var e = epsAt(r, y); return e ? e.kind : ''; });
      if (kinds.some(function (k, i) { return k === 'a' && years[i] >= asOfYear; })) notes.push('Reported EPS through ' + years[kinds.lastIndexOf('a')]);
      lines.push([get(r, 'country'), get(r, 'sector'), get(r, 'industry'), r.s, get(r, 'name'), get(r, 'price'), sp,
        m.treasury_10y ? +(m.treasury_10y.value / 100).toFixed(6) : '', m.cpi ? m.cpi.yoy : '']
        .concat(years.map(function (y) { var e = epsAt(r, y); return e ? e.v : ''; }))
        .concat([adjEps(r) ? adjEps(r).v : '', get(r, 'feps'), get(r, 'mcap'), get(r, 'ev'), get(r, 'ebitda'), get(r, 'fcf'), get(r, 'exch'), get(r, 'dy'), get(r, 'dg5'),
          get(r, 'z'), get(r, 'f'), snap.as_of, notes.join('; ')]));
    });
    saveCsv(lines, 'equity-universe-' + snap.as_of + '.csv');
  }
  function saveCsv(lines, filename) {
    var csv = lines.map(function (line) {
      return line.map(function (v) {
        if (v == null) return '';
        var s = String(v);
        if (/^[=+\-@\t\r]/.test(s) && !isNum(v)) s = "'" + s;       // never let a cell be read as a formula
        return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
      }).join(',');
    }).join('\r\n');
    var url = URL.createObjectURL(new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' }));
    var a = h('a', { href: url, download: filename });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
  }
  /* A sheet to fill in by hand: the tickers currently shown, with empty columns. Filled cells win once the
     file is uploaded to the repository's manual/ folder; empty cells change nothing. */
  function blankSheet() {
    var snap = state.snap, years = snap.eps_years || [];
    var head = ['Symbol', 'Company Name', 'As of', 'Source'].concat(years.map(function (y) { return y + ' EPS'; }))
      .concat(['Market Capitalization', 'Enterprise Value', 'EBITDA', 'Free Cash Flow', 'Dividend Yield', '5 Year Dividend Growth Rate',
        'Altman Z-Score', 'Piotroski F-Score', 'Country', 'Sector', 'Industry']);
    var today = new Date(), stamp = today.getFullYear() + '-' + ('0' + (today.getMonth() + 1)).slice(-2) + '-' + ('0' + today.getDate()).slice(-2);
    var lines = [head];
    filteredRows().forEach(function (r) {
      var line = [r.s, get(r, 'name') || '', stamp, ''];
      while (line.length < head.length) line.push('');
      lines.push(line);
    });
    saveCsv(lines, 'figures-' + stamp + '.csv');
  }

  // ------------------------------------------------------------------ tooltip (charts)
  function showTip(target, title, detail, evt) {
    var tip = clear($('tooltip'));
    add(tip, [h('b', { text: title }), detail]);
    tip.hidden = false;
    var box = target.getBoundingClientRect();
    var x = evt && evt.clientX != null ? evt.clientX : box.left + box.width / 2;
    var y = evt && evt.clientY != null ? evt.clientY : box.top;
    var w = tip.offsetWidth, ht = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, x - w / 2)) + 'px';
    tip.style.top = Math.max(8, y - ht - 12) + 'px';
  }
  function hideTip() { $('tooltip').hidden = true; }

  // ------------------------------------------------------------------ EPS chart
  function niceTicks(lo, hi) {
    var span = hi - lo || 1, raw = span / 3, mag = Math.pow(10, Math.floor(Math.log10(raw))), norm = raw / mag;
    var step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag, ticks = [];
    for (var t = Math.ceil(lo / step) * step; t <= hi + step * 1e-6; t += step) ticks.push(Math.abs(t) < step * 1e-6 ? 0 : t);
    return ticks;
  }
  function epsChart(points) {
    var have = points.filter(function (p) { return p.e; });
    if (have.length < 2) return null;
    var W = 420, H = 176, L = 38, R = 10, T = 18, B = 22, pw = W - L - R, ph = H - T - B;
    var vals = have.map(function (p) { return p.e.v; });
    var lo = Math.min(0, Math.min.apply(null, vals)), hi = Math.max(0, Math.max.apply(null, vals));
    if (hi === lo) hi = lo + 1;
    var pad = (hi - lo) * 0.12;
    hi += hi > 0 ? pad : 0;
    lo -= lo < 0 ? pad : 0;
    var y = function (v) { return T + ph * (hi - v) / (hi - lo); };
    var root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'group', 'aria-label': 'Earnings per share by fiscal year' });
    niceTicks(lo, hi).forEach(function (t) {
      root.appendChild(svg('line', { class: t === 0 ? 'axis' : 'grid', x1: L, x2: W - R, y1: y(t), y2: y(t) }));
      root.appendChild(svg('text', { x: L - 6, y: y(t) + 4, 'text-anchor': 'end' }, fixed(t, Math.abs(t) < 10 && t % 1 ? 1 : 0)));
    });
    var band = pw / points.length, bw = Math.min(24, band * 0.62);
    var lastOf = {};
    have.forEach(function (p) { lastOf[p.e.kind] = p.year; });
    points.forEach(function (p, i) {
      var cx = L + band * (i + 0.5);
      root.appendChild(svg('text', { x: cx, y: H - 6, 'text-anchor': 'middle' }, String(p.year)));
      if (!p.e) return;
      var v = p.e.v, y0 = y(0), y1 = y(v), top = Math.min(y0, y1), ht = Math.max(1.5, Math.abs(y1 - y0)), r = Math.min(4, ht, bw / 2), x = cx - bw / 2;
      var d = v >= 0
        ? 'M' + x + ',' + (top + ht) + ' V' + (top + r) + ' Q' + x + ',' + top + ' ' + (x + r) + ',' + top + ' H' + (x + bw - r) + ' Q' + (x + bw) + ',' + top + ' ' + (x + bw) + ',' + (top + r) + ' V' + (top + ht) + ' Z'
        : 'M' + x + ',' + top + ' V' + (top + ht - r) + ' Q' + x + ',' + (top + ht) + ' ' + (x + r) + ',' + (top + ht) + ' H' + (x + bw - r) + ' Q' + (x + bw) + ',' + (top + ht) + ' ' + (x + bw) + ',' + (top + ht - r) + ' V' + top + ' Z';
      var kind = p.e.kind === 'a' ? 'actual' : p.e.kind === 'e' ? 'estimate' : 'manual';
      var words = p.e.kind === 'a' ? 'Reported (GAAP diluted)' : p.e.kind === 'e' ? 'Analyst consensus' + (p.e.n ? ', ' + p.e.n + ' analyst' + (p.e.n === 1 ? '' : 's') : '') : 'Typed in by hand';
      var bar = svg('path', { d: d, class: 'bar ' + kind, tabindex: '0', role: 'img', 'aria-label': p.year + ': ' + fmtNum(v, 2) + ', ' + words });
      var show = function (evt) { showTip(bar, fmtNum(v, 2), p.year + ' · ' + words, evt); };
      bar.addEventListener('pointermove', show);
      bar.addEventListener('pointerleave', hideTip);
      bar.addEventListener('focus', function () { show(null); });
      bar.addEventListener('blur', hideTip);
      // generous hit area around a thin bar
      var hit = svg('rect', { x: cx - band / 2, y: T, width: band, height: ph, fill: 'transparent' });
      hit.addEventListener('pointermove', show);
      hit.addEventListener('pointerleave', hideTip);
      root.appendChild(hit);
      root.appendChild(bar);
      if (lastOf[p.e.kind] === p.year) {   // label only the latest reported and the furthest estimate
        root.appendChild(svg('text', { class: 'val', x: cx, y: v >= 0 ? top - 5 : top + ht + 13, 'text-anchor': 'middle' }, fmtNum(v, 2)));
      }
    });
    var kinds = {};
    have.forEach(function (p) { kinds[p.e.kind] = true; });
    var legend = h('div', { class: 'legend' });
    if (kinds.a) legend.appendChild(h('span', null, h('i', { class: 'actual' }), 'Reported'));
    if (kinds.e) legend.appendChild(h('span', null, h('i', { class: 'estimate' }), 'Analyst estimate'));
    if (kinds.m) legend.appendChild(h('span', null, h('i', { class: 'manual' }), 'Typed in by hand'));
    return h('div', { class: 'chart' }, Object.keys(kinds).length > 1 ? legend : null, root);
  }

  // ------------------------------------------------------------------ ticker drawer
  function kv(pairs) {
    var dl = h('dl', { class: 'kv' });
    pairs.forEach(function (p) {
      if (!p) return;
      dl.appendChild(h('dt', { text: p[0] }));
      dl.appendChild(h('dd', null, p[1] == null ? '—' : p[1], p[2] ? [' ', h('small', { text: p[2] })] : null));
    });
    return dl;
  }
  function freshness(row, ds) {
    var c = stCode(row, ds);
    if (!c) return 'Fresh';
    if (c === 'n') return DATASETS[ds].none;
    if (c === 'm') return 'Unavailable - the latest fetch failed and there is no earlier value';
    if (c.indexOf('c:') === 0) return 'Checked on ' + fmtDay(c.slice(2)) + ' (re-checked every few weeks)';
    return 'Carried over from ' + fmtDay(c.slice(2), true) + ' - the latest fetch failed';
  }
  function openDrawer(row) {
    state.selected = row.s;
    Array.prototype.forEach.call($('grid-body').children, function (tr) { tr.classList.toggle('selected', tr.getAttribute('data-s') === row.s); });
    var snap = state.snap, cur = get(row, 'cur') || 'USD', money = function (v) { return fmtMoney(v, cur); };
    var d = clear($('drawer'));
    d.hidden = false;
    var close = h('button', { type: 'button', class: 'icon-btn close', 'aria-label': 'Close detail', text: '×', on: { click: closeDrawer } });
    d.appendChild(h('header', null, h('div', null, h('h2', { text: row.s }), h('div', { class: 'who', text: get(row, 'name') || '' })), close));
    d.appendChild(h('div', { class: 'fine', text: [get(row, 'exch'), get(row, 'country'), get(row, 'sector'), get(row, 'industry')].filter(Boolean).join(' · ') }));

    // price
    d.appendChild(h('h3', { text: 'Price' }));
    d.appendChild(kv([
      ['Close', fmtNum(get(row, 'price'), 2), row.price != null ? cur + (row.plag && row.pdate ? ' \u00b7 last traded ' + fmtDay(row.pdate, true) : '') : null],
      ['Change on the day', fmtSigned(row.chg1d, 1)],
      ['Change over a week', fmtSigned(row.chg1w, 1)],
      ['One-year change', fmtSigned(row.ret1y, 1)],
      ['52-week range', isNum(row.lo52) && isNum(row.hi52) ? fmtNum(row.lo52, 2) + ' – ' + fmtNum(row.hi52, 2) : null]
    ]));

    // EPS
    d.appendChild(h('h3', { text: 'Earnings per share' }));
    var points = (snap.eps_years || []).map(function (y) { return { year: y, e: epsAt(row, y) }; });
    var chart = epsChart(points);
    if (chart) d.appendChild(chart);
    var table = h('table', { class: 'mini' }, h('thead', null, h('tr', null,
      h('th', { scope: 'col', text: 'Fiscal year' }), h('th', { scope: 'col', class: 'num', text: 'EPS' }), h('th', { scope: 'col', text: 'Basis' }))));
    var tb = h('tbody');
    points.forEach(function (p) {
      var none = p.year >= +snap.as_of.slice(0, 4) && stCode(row, 'e') === 'n'
        ? 'No analyst forecast (' + (get(row, 'exch') === 'OTC' ? 'OTC tickers are not covered by Nasdaq' : 'none published by Nasdaq') + ')' : 'Not available';
      var basis = !p.e ? none : p.e.kind === 'a' ? 'Reported, GAAP diluted (Yahoo Finance)'
        : p.e.kind === 'e' ? 'Analyst consensus' + (p.e.n ? ', ' + p.e.n + ' analyst' + (p.e.n === 1 ? '' : 's') : '') + ' (Nasdaq)'
          : 'Entered by hand' + (p.e.note ? ': ' + p.e.note : '');
      if (p.e && p.e.kind === 'a' && row.eps_ar && isNum(row.eps_ar[p.year])) basis += ' · ' + fmtNum(row.eps_ar[p.year], 2) + ' ' + row.fcur + ' as reported';
      tb.appendChild(h('tr', null, h('td', { text: String(p.year) }), h('td', { class: 'num' + (p.e && p.e.kind === 'e' ? ' est' : ''), text: p.e ? fmtNum(p.e.v, 2) : '—' }),
        h('td', { class: 'fine', text: basis })));
    });
    table.appendChild(tb);
    d.appendChild(table);
    d.appendChild(kv([
      (function () { var a = adjEps(row); return a ? ['Fiscal ' + a.year + ', adjusted', fmtNum(a.v, 2), 'analysts\u2019 basis (Nasdaq) - compare with the estimates'] : null; })(),
      ['Last twelve months', fmtNum(row.eps_ttm, 2), row.eps_ttm != null ? 'GAAP' : null],
      ['P/E · forward P/E', (fmtNum(row.pe, 1) || '—') + ' · ' + (fmtNum(row.fpe, 1) || '—')],
      ['Next-year EPS implied', fmtNum(get(row, 'feps'), 2), row.feps != null ? 'price ÷ Yahoo forward P/E' : null],
      row.fy_end ? ['Latest fiscal year ended', fmtDay(row.fy_end)] : null
    ]));
    var caveats = [];
    if (row.fcur) caveats.push('Reports in ' + row.fcur + '. Per-share and money figures are converted to ' + cur + ': each fiscal year at that year’s average rate, trailing figures at the trailing-year average.');
    if (row.unit_flag) caveats.push(UNIT_NOTE);
    if (points.some(function (p) { return p.e && p.e.kind === 'e'; })) caveats.push('Reported figures are GAAP; analyst estimates are usually on an adjusted basis, so the step from the last reported year to the first estimate can overstate growth.');
    caveats.forEach(function (c) { d.appendChild(h('p', { class: 'fine', text: c })); });

    // valuation
    d.appendChild(h('h3', { text: 'Valuation' }));
    d.appendChild(kv([
      ['Market cap', money(get(row, 'mcap')), row.mcap_src === 'nq' ? 'from Nasdaq' : row.val_date ? 'as of ' + fmtDay(row.val_date) : null],
      ['Enterprise value', money(get(row, 'ev'))],
      ['EBITDA', money(get(row, 'ebitda')), row.ebitda_b ? basisWords(row.ebitda_b) + ', excl. unusual items' : null],
      ['Free cash flow', money(get(row, 'fcf')), row.fcf_b ? basisWords(row.fcf_b) + ', operating cash flow − capex' : null],
      ['EV / EBITDA', isNum(row.ev) && isNum(row.ebitda) && row.ebitda > 0 ? fmtNum(row.ev / row.ebitda, 1) + '×' : null]
    ]));

    // dividends
    d.appendChild(h('h3', { text: 'Dividends' }));
    var freq = { 1: 'once a year', 2: 'twice a year', 4: 'quarterly', 12: 'monthly' }[row.div_freq] || (row.div_freq ? row.div_freq + ' times a year' : null);
    d.appendChild(kv([
      ['Yield', fmtPct(get(row, 'dy'), 2), row.div_ttm ? fmtNum(row.div_ttm, 3) + ' ' + cur + ' paid in the last 12 months' : null],
      ['5-year growth', fmtSigned(get(row, 'dg5'), 1), row.dg5 != null ? 'a year' : null],
      ['Paid', freq, row.div_last ? 'last ex-dividend date ' + fmtDay(row.div_last) : null],
      row.dy_cf != null && row.dy_src !== 'cf' ? ['Cross-check', fmtPct(row.dy_cf, 2), 'dividends paid per the cash-flow statement \u00f7 market value'] : null
    ]));
    if (row.dy_src === 'cf') d.appendChild(h('p', { class: 'fine', text: cfYieldNote(row) }));
    if (row.dg5_old) d.appendChild(h('p', { class: 'fine', text: '5-year growth: ' + oldGrowthNote(row) }));
    if (row.div_gap && row.dy == null) d.appendChild(h('p', { class: 'fine', text: 'Yahoo has no dividend on record since ' + fmtDay(row.div_last) + ' and no dividend figure in the cash-flow statement, so no yield is shown. The company may still be paying.' }));
    if (row.div_odd) d.appendChild(h('p', { class: 'fine', text: DIV_NOTE }));
    if (row.div_lapsed) d.appendChild(h('p', { class: 'fine', text: 'Payments have slowed or stopped; the yield counts only what was paid in the last 12 months.' }));

    // scores
    d.appendChild(h('h3', { text: 'Financial strength scores' }));
    var z = get(row, 'z'), f = get(row, 'f');
    d.appendChild(kv([
      ['Altman Z-Score', isNum(z) ? fmtNum(z, 2) : null, isNum(z) ? (z > 2.99 ? 'safe zone (above 2.99)' : z >= 1.81 ? 'grey zone (1.81 to 2.99)' : 'distress zone (below 1.81)')
        : row.z_miss ? 'source has no ' + row.z_miss.join(', ') : null],
      ['Piotroski F-Score', isNum(f) ? f + ' of 9' : null, !isNum(f) && row.ft ? row.ft.replace(/[-0]/g, '').length + ' of ' + row.ft.replace(/-/g, '').length + ' calculable tests passed' : null]
    ]));
    if (row.zx) {
      d.appendChild(h('p', { class: 'fine', text: 'Z = 1.2×' + row.zx[0] + ' + 1.4×' + row.zx[1] + ' + 3.3×' + row.zx[2] + ' + 0.6×' + row.zx[3] + ' + 1.0×' + row.zx[4] +
        '  (' + Z_PARTS.map(function (p) { return p.toLowerCase(); }).join('; ') + ')' }));
    }
    if (row.z_est) d.appendChild(h('p', { class: 'fine', text: Z_EST_NOTE }));
    if (row.ft) {
      var ul = h('ul', { class: 'tests' });
      row.ft.split('').forEach(function (t, i) {
        ul.appendChild(h('li', { class: t === '1' ? 'pass' : t === '0' ? 'fail' : 'unk' },
          h('span', { class: 't', 'aria-hidden': 'true', text: t === '1' ? '✓' : t === '0' ? '✗' : '–' }),
          h('span', { text: F_TESTS[i] + (t === '1' ? '' : t === '0' ? ' - no' : ' - not calculable from the source data') })));
      });
      d.appendChild(ul);
    }
    d.appendChild(h('p', { class: 'fine', text: 'Both scores are calculated here from Yahoo Finance statement data, so they can differ slightly from GuruFocus (which cannot be read automatically).' }));

    // freshness + sources
    d.appendChild(h('h3', { text: 'Where this row came from' }));
    d.appendChild(kv(Object.keys(DATASETS).map(function (ds) { return [DATASETS[ds].label, freshness(row, ds), DATASETS[ds].source]; })));
    var src = row.src || {}, names = { wb: 'the workbook', y: 'Yahoo Finance', nq: 'Nasdaq' };
    var srcLine = ['country', 'sector', 'industry'].filter(function (k) { return src[k]; }).map(function (k) { return k + ' from ' + names[src[k]]; }).join('; ');
    if (srcLine) d.appendChild(h('p', { class: 'fine', text: srcLine.charAt(0).toUpperCase() + srcLine.slice(1) + '.' + (row.country_alt ? ' Nasdaq lists the country as ' + row.country_alt + '.' : '') }));
    if (row.ovr) d.appendChild(h('p', { class: 'fine', text: 'Entered by hand, not refreshed automatically: ' +
      Object.keys(row.ovr).map(function (k) { return k.replace(/^eps_/, 'EPS ') + (row.ovr[k][1] ? ' (' + row.ovr[k][1] + ')' : ''); }).join('; ') + '.' }));

    // history across days
    d.appendChild(h('h3', { text: 'Earlier days' }));
    var histBox = h('div');
    d.appendChild(histBox);
    renderHistory(histBox, row.s);

    // the pages the workbook points at
    var y = row.s.replace('/', '-'), dot = row.s.replace('/', '.');
    d.appendChild(h('h3', { text: 'Open the source pages' }));
    d.appendChild(h('div', { class: 'links' },
      extLink('Yahoo Finance', 'https://finance.yahoo.com/quote/' + encodeURIComponent(y) + '/'),
      extLink('GuruFocus', 'https://www.gurufocus.com/stock/' + encodeURIComponent(dot) + '/summary'),
      extLink('MarketWatch estimates', 'https://www.marketwatch.com/investing/stock/' + encodeURIComponent(dot.toLowerCase()) + '/analystestimates')));
    if (state.index && state.week === state.index.latest) {
      d.appendChild(h('h3', { text: 'Ticker list' }));
      d.appendChild(h('button', { type: 'button', class: 'btn', text: 'Remove ' + row.s + ' from the list',
        on: { click: function () { setHidden(row.s, true); } } }));
      d.appendChild(h('p', { class: 'fine', text: 'Stops fetching it and takes it off the table. Earlier days keep it, and it can be restored from Manage tickers.' }));
    }
    close.focus();
  }
  function basisWords(code) {
    var parts = String(code).split(' ');
    return (parts[0] === 'TTM' ? '12 months to ' : 'fiscal year to ') + fmtDay(parts[1]);
  }
  function extLink(text, href) { return h('a', { href: href, target: '_blank', rel: 'noopener noreferrer', text: text + ' ↗' }); }
  function closeDrawer() {
    state.selected = null;
    $('drawer').hidden = true;
    hideTip();
    Array.prototype.forEach.call($('grid-body').children, function (tr) { tr.classList.remove('selected'); });
  }
  function renderHistory(box, symbol) {
    var weeks = state.index.snapshots.map(function (s) { return s.as_of; }).filter(function (w) { return w <= state.week; }).slice(0, 13);
    if (weeks.length < 2) { box.appendChild(h('p', { class: 'fine', text: 'History builds up from here: each trading day adds a row.' })); return; }
    var loaded = weeks.filter(function (w) { return state.cache[w]; });
    var draw = function () {
      clear(box);
      var years = (state.snap.eps_years || []), y0 = +state.snap.as_of.slice(0, 4);
      var estYear = years.filter(function (y) { return y >= y0; })[1] || y0 + 1;
      var t = h('table', { class: 'mini' }, h('thead', null, h('tr', null, h('th', { scope: 'col', text: 'Close of' }), h('th', { scope: 'col', class: 'num', text: 'Price' }),
        h('th', { scope: 'col', class: 'num', text: estYear + ' EPS' }), h('th', { scope: 'col', class: 'num', text: 'Market cap' }), h('th', { scope: 'col', class: 'num', text: 'Z' }), h('th', { scope: 'col', class: 'num', text: 'F' }))));
      var tb = h('tbody');
      weeks.forEach(function (w) {
        var snap = state.cache[w];
        if (!snap) return;
        var r = snap.rows.filter(function (x) { return x.s === symbol; })[0];
        var e = r && epsAt(r, estYear);
        tb.appendChild(h('tr', null, h('td', { text: fmtDay(w) }),
          h('td', { class: 'num', text: r ? fmtNum(get(r, 'price'), 2) || '—' : '—' }),
          h('td', { class: 'num' + (e && e.kind === 'e' ? ' est' : ''), text: e ? fmtNum(e.v, 2) : '—' }),
          h('td', { class: 'num', text: r ? fmtMoney(get(r, 'mcap'), get(r, 'cur')) || '—' : '—' }),
          h('td', { class: 'num', text: r ? fmtNum(get(r, 'z'), 2) || '—' : '—' }),
          h('td', { class: 'num', text: r && isNum(get(r, 'f')) ? String(get(r, 'f')) : '—' })));
      });
      t.appendChild(tb);
      box.appendChild(t);
      if (loaded.length < weeks.length) {
        box.appendChild(h('p', { class: 'fine' }, h('button', { type: 'button', class: 'link-btn', text: weeks.length === 2 ? 'Load the previous day' : 'Load the previous ' + (weeks.length - 1) + ' snapshots',
          on: { click: function (ev) {
            ev.target.disabled = true;
            ev.target.textContent = 'Loading…';
            Promise.all(weeks.map(function (w) { return loadSnapshot(w).catch(function () { return null; }); })).then(function () {
              loaded = weeks.filter(function (w) { return state.cache[w]; });
              if (state.selected === symbol) draw();
            });
          } } })));
      }
    };
    draw();
  }

  // ------------------------------------------------------------------ data health view
  function badge(status, words) {
    return h('span', { class: 'badge s-' + status }, h('span', { class: 'dot' }), words || STATUS_WORDS[status] || status);
  }
  function renderHealth() {
    var view = clear($('view-health')), st = state.status;
    var wrap = h('div', { class: 'health' });
    view.appendChild(wrap);
    if (!st) {
      wrap.appendChild(h('div', { class: 'card' }, h('h2', { text: 'No health report yet' }),
        h('p', { class: 'lead', text: 'The refresh job writes a report every time it runs. None has been published so far.' })));
      return;
    }
    var level = overallLevel();
    var words = level === 'ok' ? 'All sources healthy' : level === 'failed' ? 'Refresh is failing' : 'Needs a look';
    var links = st.links || {};
    var head = h('div', { class: 'card' },
      h('div', { class: 'overall' },
        h('div', { class: 'big s-' + level }, h('span', { class: 'dot' }), words),
        h('div', { class: 'facts' },
          h('span', null, 'Job last ran ', h('b', { text: st.last_check ? fmtStamp(st.last_check) : 'never' }), st.last_check ? ' (' + ago(st.last_check) + ')' : ''),
          h('span', null, 'Data last fetched ', h('b', { text: st.last_refresh ? fmtStamp(st.last_refresh) : 'never' })),
          h('span', null, 'Reporting the close of ', h('b', { text: st.as_of ? fmtDay(st.as_of, true) : '\u2014' })),
          st.duration_s != null ? h('span', null, 'Took ', h('b', { text: Math.max(1, Math.round(st.duration_s / 60)) + ' min' })) : null)),
      h('p', { class: 'lead', style: 'margin-top:10px', text: st.headline || '' }),
      h('p', { class: 'fine', text: 'The job runs three times every morning before the open and reports the previous trading day\u2019s close (weekends and market holidays are skipped). The first run does the full refresh; the later ones retry anything that failed, or simply check in. ' +
        'Daily snapshots are kept for 90 days, then one per week. When a source fails, the previous day\u2019s figures stay on screen marked \u25f7 until a retry succeeds.' }),
      links.actions ? h('div', { class: 'links' }, extLink('Job runs and logs', links.actions), links.run ? extLink('Latest run', links.run) : null, links.issues ? extLink('Open issues', links.issues) : null) : null);
    wrap.appendChild(head);

    clientChecks().forEach(function (c) { wrap.insertBefore(banner(c.level, c.title, c.detail), head); });

    // sources
    var srcCard = h('div', { class: 'card' }, h('h2', { text: 'Sources' }),
      h('p', { class: 'lead', text: 'One row per feed. \u201cCarried over\u201d means the latest fetch failed and the previous value is being shown.' }));
    var t = h('table', { class: 'htable' }, h('thead', null, h('tr', null,
      h('th', { scope: 'col', text: 'Feed' }), h('th', { scope: 'col', text: 'Status' }), h('th', { scope: 'col', class: 'n', text: 'Fresh' }),
      h('th', { scope: 'col', class: 'n', text: 'Carried over' }), h('th', { scope: 'col', class: 'n', text: 'Unavailable' }),
      h('th', { scope: 'col', class: 'n', text: 'Not covered' }), h('th', { scope: 'col', text: 'What went wrong' }))));
    var tb = h('tbody');
    (st.sources || []).forEach(function (s) {
      var c = s.counts || {};
      var problems = h('td');
      if (s.errors && s.errors.length) {
        var ul = h('ul', { class: 'errors' });
        s.errors.forEach(function (e) {
          ul.appendChild(h('li', { text: e.count + ' × ' + e.message + (e.examples && e.examples.length ? '  (e.g. ' + e.examples.slice(0, 5).join(', ') + ')' : '') }));
        });
        problems.appendChild(ul);
      } else problems.appendChild(h('span', { class: 'what', text: s.status === 'ok' ? 'Nothing' : 'No detail recorded' }));
      if (s.fix) problems.appendChild(h('div', { class: 'fix' }, h('b', { text: 'How to fix: ' }), s.fix));
      tb.appendChild(h('tr', null,
        h('td', null, h('div', null, h('b', { text: s.label })), h('div', { class: 'what', text: s.source }), h('div', { class: 'what', text: s.fields })),
        h('td', null, badge(s.status)),
        h('td', { class: 'n', text: String((c.fresh || 0) + (c.cached || 0)) }),
        h('td', { class: 'n', text: String(c.stale || 0) }), h('td', { class: 'n', text: String(c.missing || 0) }), h('td', { class: 'n', text: String(c.nodata || 0) }),
        problems));
    });
    var macro = st.macro || {};
    Object.keys(macro.items || {}).forEach(function (k) {
      var it = macro.items[k];
      var td = h('td');
      if (it.errors && it.errors.length) { var ul = h('ul', { class: 'errors' }); it.errors.forEach(function (e) { ul.appendChild(h('li', { text: e })); }); td.appendChild(ul); }
      else td.appendChild(h('span', { class: 'what', text: 'Nothing' }));
      if (it.status === 'ok' && it.errors) td.appendChild(h('div', { class: 'what', text: 'The backup source filled in.' }));
      tb.appendChild(h('tr', null,
        h('td', null, h('div', null, h('b', { text: it.label })), h('div', { class: 'what', text: 'Market-wide figure (official source with a backup)' })),
        h('td', null, badge(it.status === 'ok' ? 'ok' : it.status, it.status === 'ok' ? null : it.status === 'stale' ? 'Carried over' + (it.since ? ' from ' + fmtDay(it.since) : '') : 'Unavailable')),
        h('td', { class: 'n', colspan: '4' }), td));
    });
    var fx = st.fx || {};
    if ((fx.currencies || []).length) {
      tb.appendChild(h('tr', null,
        h('td', null, h('div', null, h('b', { text: 'Exchange rates' })), h('div', { class: 'what', text: 'Yahoo Finance · converts ' + fx.currencies.join(', ') + ' into USD' })),
        h('td', null, badge((fx.failed || []).length ? 'degraded' : 'ok')),
        h('td', { class: 'n', text: String(fx.currencies.length - (fx.failed || []).length) }), h('td', { class: 'n', colspan: '3' }),
        h('td', null, h('span', { class: 'what', text: (fx.failed || []).length ? 'No rate for ' + fx.failed.join(', ') + ': companies reporting in these currencies keep the previous fundamentals.' : 'Nothing' }))));
    }
    t.appendChild(tb);
    srcCard.appendChild(h('div', { class: 'scroll-x' }, t));
    wrap.appendChild(srcCard);

    // alerts / warnings / attention
    var notes = (st.coverage_alerts || []).concat(st.warnings || []);
    if (st.error) notes.unshift('The job crashed: ' + st.error);
    if (notes.length || (st.attention || []).length) {
      var card = h('div', { class: 'card' }, h('h2', { text: 'Needs attention' }));
      if (notes.length) { var ul2 = h('ul', { class: 'plain' }); notes.forEach(function (n) { ul2.appendChild(h('li', { text: n })); }); card.appendChild(ul2); }
      if ((st.attention || []).length) {
        card.appendChild(h('p', { class: 'lead', style: 'margin-top:10px', text: 'Tickers the source does not recognise (fix the symbol in the workbook, then rebuild universe.csv):' }));
        var ul3 = h('ul', { class: 'plain' });
        st.attention.forEach(function (a) { ul3.appendChild(h('li', null, h('b', { text: a.symbol }), ' - ' + a.issue)); });
        card.appendChild(ul3);
      }
      wrap.appendChild(card);
    }

    // figures entered by hand
    var hand = handFiles(), typed = ((st.manual || {}).overrides_csv) || 0;
    if (hand.length || typed) {
      var hc = h('div', { class: 'card' }, h('h2', { text: 'Figures entered by hand' }),
        h('p', { class: 'lead', text: 'These came from files uploaded to the repository\u2019s manual/ folder. They win over fetched figures, show with a \u270e mark, and do not refresh by themselves: export and upload again when they get old.' }));
      if (hand.length) {
        var ht = h('table', { class: 'htable' }, h('thead', null, h('tr', null, h('th', { scope: 'col', text: 'File' }), h('th', { scope: 'col', class: 'n', text: 'Figures' }),
          h('th', { scope: 'col', class: 'n', text: 'Tickers' }), h('th', { scope: 'col', text: 'As of' }), h('th', { scope: 'col', text: 'State' }))));
        var hb = h('tbody');
        hand.forEach(function (f) {
          hb.appendChild(h('tr', null, h('td', null, h('b', { text: f.file }), h('div', { class: 'what', text: f.fields.map(function (k) { return k.replace(/^eps_/, 'EPS '); }).join(', ') })),
            h('td', { class: 'n', text: String(f.values) }), h('td', { class: 'n', text: String(f.tickers) }),
            h('td', { text: f.as_of ? (f.oldest && f.oldest !== f.as_of ? fmtDay(f.oldest) + ' to ' + fmtDay(f.as_of) : fmtDay(f.as_of)) +
              ' (' + (f.days === 0 ? 'today' : f.days + ' day' + (f.days === 1 ? '' : 's') + ' ago') + ')' : 'no date given' }),
            h('td', null, !f.as_of ? badge('blocked', 'Add an \u201cAs of\u201d column') : f.old ? badge('degraded', 'Older than ' + f.limit + ' days') : badge('ok', 'Current'))));
        });
        ht.appendChild(hb);
        hc.appendChild(h('div', { class: 'scroll-x' }, ht));
      }
      if (typed) hc.appendChild(h('p', { class: 'fine', text: 'Plus ' + typed + ' single figure' + (typed === 1 ? '' : 's') + ' in overrides.csv.' }));
      wrap.appendChild(hc);
    }

    // coverage meters
    var cov = st.coverage || {};
    var labels = { price: 'Price', country: 'Country', sector: 'Sector', industry: 'Industry', exch: 'Exchange', eps_a: 'EPS (reported)', eps_adj: 'EPS, adjusted (last year)', eps_e: 'EPS estimates',
      mcap: 'Market cap', ev: 'Enterprise value', ebitda: 'EBITDA', fcf: 'Free cash flow', dy: 'Dividend yield', dg5: '5-year dividend growth', z: 'Altman Z', f: 'Piotroski F' };
    if (Object.keys(cov).length) {
      var covCard = h('div', { class: 'card' }, h('h2', { text: 'How much of each column is filled in' }),
        h('p', { class: 'lead', text: 'Share of the ' + (st.tickers || '') + ' tickers with a value. A sudden drop here means a source changed or went quiet even if nothing reported an error.' }));
      var meters = h('div', { class: 'meters' });
      Object.keys(labels).forEach(function (k) {
        if (cov[k] == null) return;
        var pct = Math.round(cov[k] * 100);
        meters.appendChild(h('div', { class: 'meter', role: 'img', 'aria-label': labels[k] + ': ' + pct + '% filled in' },
          h('span', { text: labels[k] }), h('span', { class: 'pct', text: pct + '%' }),
          h('div', { class: 'track' }, h('div', { class: 'fill', style: 'width:' + pct + '%' }))));
      });
      covCard.appendChild(meters);
      wrap.appendChild(covCard);
    }

    // sources that cannot be automated + run log
    var two = h('div', { class: 'two' });
    if ((st.unavailable || []).length) {
      var un = h('div', { class: 'card' }, h('h2', { text: 'What this dashboard cannot show, and why' }),
        h('p', { class: 'lead', text: 'Some of the workbook\u2019s sources only serve people using a browser, and some figures are not published anywhere open. Nothing here is a fault to fix; it is the standing list of gaps and what stands in for each.' }));
      st.unavailable.forEach(function (u) {
        un.appendChild(h('div', { style: 'margin-top:10px' }, badge('blocked', u.name),
          h('div', { class: 'what', text: 'Wanted: ' + u.wanted }), h('div', { class: 'what', text: 'Why not: ' + u.reason }),
          h('div', { class: 'what', text: 'Shown instead: ' + u.instead }),
          u.option ? h('div', { class: 'what', text: 'To get the original: ' + u.option }) : null));
      });
      un.appendChild(h('p', { class: 'fine', text: 'Any of these can be filled in by hand: download a blank sheet from the Universe tab, fill in what you have, and upload it to the repository\u2019s manual/ folder. Those figures then show with a \u270e mark.' }));
      two.appendChild(un);
    }
    var runs = h('div', { class: 'card' }, h('h2', { text: 'Recent runs' }));
    if (!state.runs.length) runs.appendChild(h('p', { class: 'lead', text: 'No runs recorded yet.' }));
    else {
      var rt = h('table', { class: 'htable' }, h('thead', null, h('tr', null, h('th', { scope: 'col', text: 'When' }), h('th', { scope: 'col', text: 'Kind' }), h('th', { scope: 'col', text: 'Result' }), h('th', { scope: 'col', text: 'Summary' }))));
      var rb = h('tbody');
      var kinds = { full: 'Full refresh', retry: 'Retry', heartbeat: 'Check-in', crash: 'Crashed', manual: 'Hand-entered figures', update: 'List or figures updated' };
      state.runs.slice(0, 25).forEach(function (r) {
        rb.appendChild(h('tr', null, h('td', { style: 'white-space:nowrap' }, r.run ? extLink(fmtStamp(r.ts), r.run) : fmtStamp(r.ts)),
          h('td', { style: 'white-space:nowrap', text: kinds[r.mode] || r.mode || '' }), h('td', null, badge(r.overall || 'ok')),
          h('td', { class: 'what', text: r.headline || '' })));
      });
      rt.appendChild(rb);
      runs.appendChild(h('div', { class: 'scroll-x' }, rt));
    }
    two.appendChild(runs);
    wrap.appendChild(two);
  }

  // ------------------------------------------------------------------ ticker list editing (through GitHub)
  /* The dashboard is static, so changes to the ticker list are saved by committing universe.csv to the
     repository through GitHub's API, with a token the person pastes in once. The commit starts the data
     job, which fetches only what changed; this panel follows that run and reloads the page when it ends.
     The token stays in this browser and is sent only to api.github.com. */
  var GH = { api: 'https://api.github.com', file: 'universe.csv', branch: 'main', workflow: 'refresh.yml' };
  var TOKEN_KEY = 'eu.gh.token';
  var TICKER_RE = /^[A-Z0-9][A-Z0-9./-]{0,9}$/;
  var manage = { user: null, universe: null, loading: false, jobs: [], message: null, hiddenNow: {} };

  function repoSlug() {
    var link = ((state.status || {}).links || {}).repo || '';
    var m = link.match(/github\.com\/([^/]+\/[^/#?]+)/);
    if (m) return m[1];
    var host = location.hostname.match(/^([^.]+)\.github\.io$/), first = location.pathname.split('/').filter(Boolean)[0];
    return host && first ? host[1] + '/' + first : null;
  }
  function getToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }
  function setToken(token, remember) {
    try {
      sessionStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(TOKEN_KEY);
      if (token) (remember ? localStorage : sessionStorage).setItem(TOKEN_KEY, token);
    } catch (e) { /* storage blocked: the token then lasts only until the page is reloaded */ manage.memoryToken = token; }
  }
  function token() { return getToken() || manage.memoryToken || ''; }

  function gh(method, path, body) {
    var headers = { Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' };
    if (token()) headers.Authorization = 'Bearer ' + token();
    if (body) headers['Content-Type'] = 'application/json';
    return fetch(GH.api + path, { method: method, headers: headers, cache: 'no-store', body: body ? JSON.stringify(body) : undefined })
      .then(function (res) {
        return res.text().then(function (text) {
          var data = null;
          try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
          if (!res.ok) {
            var err = new Error(ghMessage(res.status, data));
            err.status = res.status;
            throw err;
          }
          return data;
        });
      });
  }
  function ghMessage(status, data) {
    var msg = (data && data.message) || ('GitHub answered HTTP ' + status);
    if (status === 401) return 'GitHub did not accept the token - it may have expired or been revoked. Connect again with a new one.';
    if (status === 403 && /not accessible|permission/i.test(msg)) return 'The token is missing a permission. It needs Contents “Read and write” (and Actions “Read and write” for Refresh now) on ' + repoSlug() + '.';
    if (status === 403 && /rate limit/i.test(msg)) return 'GitHub’s request limit was reached - try again in a few minutes.';
    if (status === 404) return 'GitHub could not find ' + repoSlug() + ' with this token - check the token was given access to that repository.';
    return msg;
  }
  function b64decode(b64) {
    var bin = atob(String(b64).replace(/\s/g, '')), bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder('utf-8').decode(bytes);
  }
  function b64encode(text) {
    var bytes = new TextEncoder().encode(text), bin = '';
    for (var i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  // universe.csv, edited line by line so untouched rows stay byte-for-byte the same
  function csvSplit(line) {
    var out = [], cur = '', quoted = false;
    for (var i = 0; i < line.length; i++) {
      var c = line[i];
      if (quoted) {
        if (c === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else quoted = false; } else cur += c;
      } else if (c === '"') quoted = true;
      else if (c === ',') { out.push(cur); cur = ''; } else cur += c;
    }
    out.push(cur);
    return out;
  }
  function csvField(value) {
    var v = value == null ? '' : String(value);
    return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }
  function parseUniverse(text) {
    var eol = text.indexOf('\r\n') >= 0 ? '\r\n' : '\n';
    var lines = text.split(/\r?\n/), trailing = lines.length > 1 && lines[lines.length - 1] === '';
    if (trailing) lines.pop();
    var header = csvSplit(lines[0] || '').map(function (x) { return x.trim().toLowerCase(); });
    if (header.indexOf('symbol') < 0) throw new Error('universe.csv in the repository has no symbol column - it needs fixing by hand.');
    if (header.indexOf('hidden') < 0) { header.push('hidden'); lines[0] += ',hidden'; }
    return { eol: eol, header: header, lines: lines, trailing: trailing };
  }
  function universeRows(u) {
    return u.lines.slice(1).map(function (line, i) {
      var f = csvSplit(line), row = { _i: i + 1 };
      u.header.forEach(function (key, j) { row[key] = (f[j] || '').trim(); });
      return row;
    }).filter(function (r) { return r.symbol; });
  }
  function setUniverseRow(u, index, row) {
    u.lines[index] = u.header.map(function (key) { return csvField(row[key] || ''); }).join(',');
  }
  function universeText(u) { return u.lines.join(u.eol) + (u.trailing ? u.eol : ''); }
  function isHiddenValue(v) { return /^(1|y|yes|true|x|hidden)$/i.test(String(v || '').trim()); }
  function symbolKey(s) { return String(s || '').toUpperCase().replace(/[.\-]/g, '/'); }
  function normaliseSymbol(text) {
    var s = String(text || '').trim().toUpperCase();
    if (/^[A-Z]{1,6}[.\-][A-Z]$/.test(s)) s = s.replace(/[.\-]/, '/');   // share classes: BRK.B / BRK-B -> BRK/B, as in the workbook
    return s;
  }

  function addMutation(symbol, extra) {
    return function (u) {
      var rows = universeRows(u), key = symbolKey(symbol);
      var found = rows.filter(function (r) { return symbolKey(r.symbol) === key; })[0];
      if (found) {
        if (!isHiddenValue(found.hidden)) throw new Error(found.symbol + ' is already in the list.');
        found.hidden = '';
        ['name', 'country', 'sector', 'industry'].forEach(function (k) { if (extra[k] && !found[k]) found[k] = extra[k]; });
        setUniverseRow(u, found._i, found);
        return { kind: 'restored', symbol: found.symbol };
      }
      var row = { symbol: symbol, name: extra.name || '', country: extra.country || '', sector: extra.sector || '', industry: extra.industry || '', hidden: '' };
      var after = rows.filter(function (r) { return r.symbol > symbol; })[0];        // keep the file in symbol order
      var index = after ? after._i : u.lines.length;
      u.lines.splice(index, 0, '');
      setUniverseRow(u, index, row);
      return { kind: 'added', symbol: symbol };
    };
  }
  function hideMutation(symbol, hide) {
    return function (u) {
      var rows = universeRows(u), key = symbolKey(symbol);
      var found = rows.filter(function (r) { return symbolKey(r.symbol) === key; })[0];
      if (!found) throw new Error(symbol + ' is not in the ticker list.');
      if (isHiddenValue(found.hidden) === hide) throw new Error(found.symbol + (hide ? ' has already been removed.' : ' is already in the list.'));
      if (hide && rows.filter(function (r) { return !isHiddenValue(r.hidden); }).length <= 1) throw new Error('At least one ticker has to stay in the list.');
      found.hidden = hide ? '1' : '';
      setUniverseRow(u, found._i, found);
      return { kind: hide ? 'removed' : 'restored', symbol: found.symbol };
    };
  }
  /* Read universe.csv, apply the change, write it back. If someone else changed the file in between,
     GitHub refuses the write; the change is then re-applied to the new version once. */
  function editUniverse(mutate, message, retried) {
    var slug = repoSlug();
    return gh('GET', '/repos/' + slug + '/contents/' + GH.file + '?ref=' + GH.branch).then(function (file) {
      var u = parseUniverse(b64decode(file.content));
      var result = mutate(u);
      return gh('PUT', '/repos/' + slug + '/contents/' + GH.file,
        { message: message, content: b64encode(universeText(u)), sha: file.sha, branch: GH.branch }).then(function (res) {
        manage.universe = u;
        result.commit = res && res.commit && res.commit.sha;
        return result;
      });
    }).catch(function (err) {
      if ((err.status === 409 || err.status === 422) && !retried) return editUniverse(mutate, message, true);
      throw err;
    });
  }
  function loadUniverse() {
    manage.loading = true;
    return gh('GET', '/repos/' + repoSlug() + '/contents/' + GH.file + '?ref=' + GH.branch).then(function (file) {
      manage.universe = parseUniverse(b64decode(file.content));
    }).catch(function (err) { manage.message = { level: 'error', text: err.message }; })
      .then(function () { manage.loading = false; renderManage(); });
  }

  // following the data job a change starts
  function startJob(label, result) {
    var job = { label: label, symbol: result && result.symbol, kind: result && result.kind, commit: result && result.commit,
      started: Date.now(), state: 'queued', note: 'Saved. Waiting for GitHub to start the update…' };
    manage.jobs.unshift(job);
    manage.jobs = manage.jobs.slice(0, 8);
    renderManage();
    pollJob(job);
    return job;
  }
  function pollJob(job) {
    var slug = repoSlug();
    var path = job.runId ? '/actions/runs/' + job.runId
      : job.commit ? '/actions/runs?head_sha=' + job.commit + '&per_page=1'
        : '/actions/workflows/' + GH.workflow + '/runs?branch=' + GH.branch + '&per_page=5';
    gh('GET', '/repos/' + slug + path).then(function (data) {
      var run = job.runId ? data : ((data && data.workflow_runs) || []).filter(function (r) {
        return job.commit || new Date(r.created_at).getTime() >= job.started - 60000;
      })[0];
      if (run) {
        job.runId = run.id;
        job.url = run.html_url;
        if (run.status !== 'completed') {
          job.state = run.status === 'in_progress' ? 'running' : 'queued';
          job.note = run.status === 'in_progress' ? 'Updating the data… (usually 2–5 minutes)' : 'Queued behind another update…';
        } else if (run.conclusion === 'cancelled' || run.conclusion === 'skipped') {
          // superseded: a newer queued run (which starts from the latest commit, so includes this change) took its place
          job.runId = null;
          job.commit = null;
          job.started = new Date(run.updated_at || Date.now()).getTime();
          job.note = 'Handed over to a newer update that includes this change…';
        } else {
          job.state = run.conclusion === 'success' ? 'done' : 'failed';
          job.note = run.conclusion === 'success' ? 'Done.' : 'The update run did not finish cleanly - see Data health.';
          renderManage();
          return reloadData().then(function () { describeOutcome(job); renderManage(); });
        }
      }
      renderManage();
      if (Date.now() - job.started > 25 * 60000) {
        job.state = 'failed';
        job.note = 'No result after 25 minutes - check the run log on GitHub.';
        renderManage();
        return;
      }
      setTimeout(function () { pollJob(job); }, 10000);
    }).catch(function (err) {
      job.note = 'Could not check progress (' + err.message + '). Retrying…';
      renderManage();
      setTimeout(function () { pollJob(job); }, 20000);
    });
  }
  function describeOutcome(job) {
    if (!job.symbol || job.state !== 'done') return;
    var row = state.snap && state.snap.rows.filter(function (r) { return symbolKey(r.s) === symbolKey(job.symbol); })[0];
    if (job.kind === 'removed') {
      job.note = row ? 'Saved, but ' + job.symbol + ' is still in the latest data - reload in a minute.' : job.symbol + ' is no longer shown.';
    } else if (!row) {
      job.note = job.symbol + ' was saved but is not in the data yet - reload in a minute.';
    } else if ((row.st || {}).q === 'n') {
      job.state = 'failed';
      job.note = 'Yahoo Finance has no prices for ' + job.symbol + '. Check the symbol (as Yahoo shows it; OTC ADRs usually end in Y or F) and remove it if it is wrong.';
    } else {
      job.note = job.symbol + ' is in the table' + (row.price != null ? ' (close ' + fmtNum(row.price, 2) + ')' : '') + '.';
    }
  }
  /* Reload index, status and the latest snapshot after the job has run. */
  function reloadData() {
    state.cache = {};
    return Promise.all([getJSON('data/index.json'), getJSON('data/status.json').catch(function () { return state.status; }),
      getJSON('data/runs.json').catch(function () { return state.runs; })]).then(function (res) {
      state.index = res[0];
      state.status = res[1];
      state.runs = Array.isArray(res[2]) ? res[2] : [];
      manage.hiddenNow = {};
      fillWeekSelect(state.index.snapshots.map(function (s) { return s.as_of; }));
      renderHealthPill();
      return showWeek(state.index.latest);
    });
  }

  // actions
  function connect(tokenText, remember) {
    var tok = String(tokenText || '').trim();
    if (!tok) return Promise.resolve(showManageMessage('error', 'Paste the token first.'));
    setToken(tok, remember);
    manage.memoryToken = tok;
    showManageMessage('info', 'Checking the token…');
    return gh('GET', '/repos/' + repoSlug()).then(function (repo) {
      if (repo.permissions && !repo.permissions.push) throw new Error('This GitHub account can see ' + repoSlug() + ' but cannot change it. Ask the owner to add it as a collaborator.');
      return gh('GET', '/user');
    }).then(function (user) {
      manage.user = user && user.login;
      manage.message = null;
      return loadUniverse();
    }).catch(function (err) {
      setToken('', false);
      manage.memoryToken = '';
      manage.user = null;
      showManageMessage('error', err.message);
    });
  }
  function disconnect() {
    setToken('', false);
    manage.memoryToken = '';
    manage.user = null;
    manage.universe = null;
    showManageMessage('info', 'Disconnected. The token is no longer stored in this browser (it still works until you revoke it on GitHub).');
  }
  function showManageMessage(level, text) {
    manage.message = { level: level, text: text };
    renderManage();
  }
  function addTicker(form) {
    var symbol = normaliseSymbol(form.symbol.value);
    if (!TICKER_RE.test(symbol)) return showManageMessage('error', '“' + form.symbol.value + '” is not a valid ticker symbol.');
    var extra = { name: form.company.value.trim(), country: form.country.value.trim() };
    form.querySelector('button[type=submit]').disabled = true;
    showManageMessage('info', 'Saving ' + symbol + '…');
    return editUniverse(addMutation(symbol, extra), 'tickers: add ' + symbol + ' (from the dashboard)').then(function (result) {
      manage.message = null;
      form.reset();
      startJob((result.kind === 'restored' ? 'Restore ' : 'Add ') + result.symbol, result);
    }).catch(function (err) { showManageMessage('error', err.message); })
      .then(function () { var b = form.querySelector('button[type=submit]'); if (b) b.disabled = false; });
  }
  function setHidden(symbol, hide) {
    if (!token()) { openManage(); return showManageMessage('info', 'Connect this page to GitHub first (below), then try again.'); }
    if (hide && !window.confirm('Remove ' + symbol + ' from the list?\n\nIt stops being fetched and disappears from the table within a few minutes. Earlier days still show it, and you can restore it from Manage tickers.')) return;
    showManageMessage('info', (hide ? 'Removing ' : 'Restoring ') + symbol + '…');
    return editUniverse(hideMutation(symbol, hide), 'tickers: ' + (hide ? 'remove ' : 'restore ') + symbol + ' (from the dashboard)').then(function (result) {
      manage.message = null;
      if (hide) {
        manage.hiddenNow[symbolKey(symbol)] = true;   // take it off the table straight away
        if (state.selected && symbolKey(state.selected) === symbolKey(symbol)) closeDrawer();
        state.rowCache = { layout: null, els: {} };
        renderTable();
      }
      startJob((hide ? 'Remove ' : 'Restore ') + result.symbol, result);
    }).catch(function (err) { showManageMessage('error', err.message); });
  }
  function refreshNow() {
    showManageMessage('info', 'Asking GitHub to run the update…');
    return gh('POST', '/repos/' + repoSlug() + '/actions/workflows/' + GH.workflow + '/dispatches', { ref: GH.branch, inputs: { force: 'false' } })
      .then(function () { manage.message = null; startJob('Refresh now', null); })
      .catch(function (err) { showManageMessage('error', err.message); });
  }

  // the panel
  function openManage() {
    closeDrawer();
    $('manage').hidden = false;
    renderManage();
    if (token() && !manage.user) connect(token(), !!(function () { try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return ''; } })());
    else if (token() && !manage.universe && !manage.loading) loadUniverse();
  }
  function closeManage() { $('manage').hidden = true; }
  function tokenLink() {
    var owner = (repoSlug() || '').split('/')[0];
    return 'https://github.com/settings/personal-access-tokens/new?' + [
      'name=' + encodeURIComponent('Equity dashboard - tickers'),
      'description=' + encodeURIComponent('Lets the dashboard add and remove tickers in ' + repoSlug()),
      'target_name=' + encodeURIComponent(owner), 'expires_in=90', 'contents=write', 'actions=write'].join('&');
  }
  function renderManage() {
    var box = $('manage');
    if (box.hidden) return;
    clear(box);
    var close = h('button', { type: 'button', class: 'icon-btn close', 'aria-label': 'Close', text: '×', on: { click: closeManage } });
    box.appendChild(h('header', null, h('div', null, h('h2', { text: 'Manage tickers' }),
      h('div', { class: 'who', text: 'Changes are saved to the ticker list in the repository and show up here a few minutes later.' })), close));
    if (manage.message) {
      box.appendChild(h('div', { class: 'note-box ' + manage.message.level, role: manage.message.level === 'error' ? 'alert' : 'status', text: manage.message.text }));
    }
    if (!token() || !manage.user) {
      renderConnect(box);
    } else {
      renderConnected(box);
    }
    close.focus({ preventScroll: true });
  }
  function renderConnect(box) {
    box.appendChild(h('h3', { text: 'Connect this page to GitHub' }));
    box.appendChild(h('p', { class: 'fine', text: 'This page is a static website, so it saves changes by editing the ticker list in the GitHub repository on your behalf. That needs a GitHub access token that can edit ' + (repoSlug() || 'the repository') + '. One-time setup:' }));
    box.appendChild(h('ol', { class: 'steps' },
      h('li', null, h('a', { href: tokenLink(), target: '_blank', rel: 'noopener noreferrer', text: 'Open GitHub’s token page ↗' }), ' (signed in as the repository owner). Name, permissions and a 90-day expiry are filled in.'),
      h('li', { text: 'Under “Repository access” choose “Only select repositories” and pick ' + ((repoSlug() || '').split('/')[1] || 'this repository') + '.' }),
      h('li', { text: 'Press “Generate token”, copy it, and paste it here.' })));
    var form = h('form', { class: 'mform', autocomplete: 'off' },
      h('label', { class: 'field' }, h('span', { text: 'GitHub token' }), h('input', { type: 'password', name: 'token', placeholder: 'github_pat_…', spellcheck: 'false', required: true })),
      h('label', { class: 'check' }, h('input', { type: 'checkbox', name: 'remember' }), ' Remember on this browser (otherwise forgotten when the tab closes)'),
      h('div', null, h('button', { type: 'submit', class: 'btn primary', text: 'Connect' })));
    form.addEventListener('submit', function (e) { e.preventDefault(); connect(form.token.value, form.remember.checked); });
    box.appendChild(form);
    box.appendChild(h('p', { class: 'fine', text: 'The token is kept only in this browser and is sent only to GitHub. Anyone who has it can change this repository, so do not share it; it expires after 90 days and can be revoked any time at github.com → Settings → Developer settings → Personal access tokens.' }));
  }
  function renderConnected(box) {
    box.appendChild(h('p', { class: 'fine' }, 'Connected as ', h('b', { text: manage.user }), ' · ',
      h('button', { type: 'button', class: 'link-btn', text: 'Disconnect', on: { click: disconnect } })));

    box.appendChild(h('h3', { text: 'Add a ticker' }));
    var form = h('form', { class: 'mform', autocomplete: 'off' },
      h('div', { class: 'row3' },
        h('label', { class: 'field' }, h('span', { text: 'Symbol' }), h('input', { name: 'symbol', required: true, maxlength: 10, placeholder: 'e.g. NVO', spellcheck: 'false' })),
        h('label', { class: 'field' }, h('span', { text: 'Company (optional)' }), h('input', { name: 'company', maxlength: 80 })),
        h('label', { class: 'field' }, h('span', { text: 'Country (optional)' }), h('input', { name: 'country', maxlength: 40, placeholder: 'e.g. Denmark' }))),
      h('div', null, h('button', { type: 'submit', class: 'btn primary', text: 'Add' })));
    form.addEventListener('submit', function (e) { e.preventDefault(); addTicker(form); });
    box.appendChild(form);
    box.appendChild(h('p', { class: 'fine', text: 'Use the symbol Yahoo Finance shows (OTC ADRs usually end in Y or F; share classes as BRK/B or BRK-B). The job fetches just the new ticker; it appears in the table in about 3–5 minutes. Country is filled in automatically only for NYSE/Nasdaq listings, so give it for OTC tickers.' }));

    if (manage.jobs.length) {
      box.appendChild(h('h3', { text: 'Recent changes' }));
      var ul = h('ul', { class: 'jobs' });
      manage.jobs.forEach(function (job) {
        var mark = job.state === 'done' ? '✓' : job.state === 'failed' ? '!' : '…';
        ul.appendChild(h('li', { class: 'job-' + job.state },
          h('span', { class: 'jmark', 'aria-hidden': 'true', text: mark }),
          h('div', null, h('b', { text: job.label }), h('div', { class: 'fine', text: job.note }),
            job.url ? extLink('Run log', job.url) : null)));
      });
      box.appendChild(ul);
    }

    box.appendChild(h('h3', { text: 'Removed tickers' }));
    if (!manage.universe) {
      box.appendChild(h('p', { class: 'fine', text: manage.loading ? 'Loading the list…' : 'The list could not be loaded.' }));
    } else {
      var hidden = universeRows(manage.universe).filter(function (r) { return isHiddenValue(r.hidden); });
      if (!hidden.length) box.appendChild(h('p', { class: 'fine', text: 'None. Remove a ticker from its detail panel (click its row).' }));
      else {
        var list = h('ul', { class: 'hidden-list' });
        hidden.forEach(function (r) {
          list.appendChild(h('li', null, h('span', null, h('b', { text: r.symbol }), r.name ? ' · ' + r.name : ''),
            h('button', { type: 'button', class: 'btn small', text: 'Restore', on: { click: function () { setHidden(r.symbol, false); } } })));
        });
        box.appendChild(list);
      }
      var visible = universeRows(manage.universe).length - hidden.length;
      box.appendChild(h('p', { class: 'fine', text: visible + ' tickers in the list, ' + hidden.length + ' removed. Removed tickers stay in universe.csv (marked hidden) so they keep their workbook labels when restored.' }));
    }

    box.appendChild(h('h3', { text: 'Refresh' }));
    box.appendChild(h('p', { class: 'fine', text: 'The data updates on its own every morning before the open. Use this to retry now if something failed, or to pick up files you uploaded.' }));
    box.appendChild(h('button', { type: 'button', class: 'btn', text: 'Run the update now', on: { click: refreshNow } }));
  }

  // ------------------------------------------------------------------ view switching + events
  function setView(view) {
    state.view = view;
    ['universe', 'health'].forEach(function (v) {
      $('view-' + v).hidden = v !== view;
      $('tab-' + v).setAttribute('aria-selected', String(v === view));
    });
    $('week-control').style.visibility = view === 'universe' ? '' : 'hidden';
    if (view !== 'universe') closeDrawer();
    setHash();
  }
  function debounce(fn, ms) { var t; return function () { clearTimeout(t); t = setTimeout(fn, ms); }; }
  function wire() {
    $('tab-universe').addEventListener('click', function () { setView('universe'); });
    $('tab-health').addEventListener('click', function () { setView('health'); });
    $('week-select').addEventListener('change', function (e) { showWeek(e.target.value); });
    $('week-prev').addEventListener('click', function () { stepWeek(1); });
    $('week-next').addEventListener('click', function () { stepWeek(-1); });
    $('q').addEventListener('input', debounce(function () { state.filters.q = $('q').value; renderTable(); }, 120));
    [['f-country', 'country'], ['f-sector', 'sector'], ['f-exch', 'exch']].forEach(function (p) {
      $(p[0]).addEventListener('change', function (e) { state.filters[p[1]] = e.target.value; renderTable(); });
    });
    $('f-issues').addEventListener('change', function (e) { state.filters.issues = e.target.checked; renderTable(); });
    $('f-missing').addEventListener('change', function (e) { state.filters.missing = e.target.value; renderTable(); });
    $('export').addEventListener('click', exportCsv);
    $('template').addEventListener('click', blankSheet);
    $('manage-open').addEventListener('click', openManage);
    $('grid-body').addEventListener('click', function (e) { rowFromEvent(e, true); });
    $('grid-body').addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); rowFromEvent(e, false); } });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      if (!$('manage').hidden) closeManage();
      else if (!$('drawer').hidden) closeDrawer();
    });
  }
  function rowFromEvent(e, toggle) {
    var tr = e.target.closest ? e.target.closest('tr') : null;
    if (!tr) return;
    var symbol = tr.getAttribute('data-s');
    if (toggle && state.selected === symbol) { closeDrawer(); return; }
    var row = state.snap.rows.filter(function (r) { return r.s === symbol; })[0];
    if (row) openDrawer(row);
  }
  function stepWeek(delta) {
    var weeks = state.index.snapshots.map(function (s) { return s.as_of; });
    var i = weeks.indexOf(state.week) + delta;
    if (i >= 0 && i < weeks.length) showWeek(weeks[i]);
  }

  init();
})();
