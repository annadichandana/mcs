'use strict';
const $ = (s, r = document) => r.querySelector(s);
const view = $('#view');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch('/api' + path, { headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
  return data;
};

/* ---------- formatting ---------- */
function fmtVal(v, unit, basis, metric) {
  if (v == null) return '–';
  if (basis && basis !== 'absolute' || unit === 'percent') return `${(+v).toFixed(1)}%`;
  if (metric === 'eps' || unit === 'usd_per_share') return `$${(+v).toFixed(2)}`;
  const a = Math.abs(v), s = v < 0 ? '-' : '';
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(1)}M`;
  return `${s}$${a.toLocaleString()}`;
}
const METRIC = { revenue: 'Revenue', gross_profit: 'Gross profit', gross_margin: 'Gross margin', operating_income: 'Operating income',
  operating_margin: 'Operating margin', net_income: 'Net income', net_margin: 'Net margin', eps: 'EPS (diluted)', ebitda: 'EBITDA',
  capex: 'Capex', free_cash_flow: 'Free cash flow', other: 'Other' };
const BASIS = { yoy_growth_pct: ' growth, year over year', qoq_growth_pct: ' growth, quarter over quarter' };
const metricLabel = r => (METRIC[r.metric] || r.metric) + (BASIS[r.basis] || '');
const OUT = { within: 'Within range', above: 'Above range', below: 'Below range' };
const STATUS = { pending: 'Not yet reported', missing_actual: 'No actual found', unresolved_period: 'Period unclear',
  unverifiable_metric: 'Not checkable', qualitative: 'Qualitative' };
const CSTATUS = { delivered: 'Delivered', partially_delivered: 'Partly delivered', not_delivered: 'Not delivered',
  no_evidence: 'No evidence', pending: 'Not yet due', superseded: 'Superseded' };
const chip = (cls, text) => `<span class="chip ${esc(cls)}">${esc(text)}</span>`;
const pretty = k => k.replace(/^FY(\d{4})(Q\d|FY)$/, 'FY$1 $2');
const fmtScore = s => s == null ? '–' : Math.round(s);

/* ---------- job runner ---------- */
let pollTimer;
function runJob(title, jobId, onDone) {
  const box = $('#job'), log = $('#job-log');
  box.hidden = false; $('#job-title').textContent = title; $('#job-close').hidden = true; log.textContent = '';
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const j = await api('/jobs/' + jobId);
      log.textContent = j.log.join('\n'); log.scrollTop = log.scrollHeight;
      if (j.status !== 'running') {
        clearInterval(pollTimer);
        $('#job-title').textContent = j.status === 'done' ? `${title}: finished` : `${title}: failed`;
        if (j.error) log.textContent += `\n\n${j.error}`;
        $('#job-close').hidden = false;
        if (j.status === 'done' && onDone) onDone(j.result);
        refreshReviewCount();
      }
    } catch (e) { clearInterval(pollTimer); log.textContent += '\n' + e.message; $('#job-close').hidden = false; }
  }, 1500);
}
$('#job-close').onclick = () => { $('#job').hidden = true; };

async function refreshReviewCount() {
  try {
    const q = await api('/review-queue'), el = $('#review-count');
    el.textContent = q.length; el.hidden = !q.length;
  } catch { /* ignore */ }
}

/* ---------- pages ---------- */
async function home() {
  const [cfg, list] = await Promise.all([api('/config'), api('/companies')]);
  const msgs = [!cfg.anthropic && 'Claude is not available: add ANTHROPIC_API_KEY to .env, or install the Claude Code CLI (<code>claude</code>) and log in.', !cfg.fmp && 'FMP_API_KEY is missing in .env.'].filter(Boolean);
  $('#banner').innerHTML = msgs.length ? `<div class="warn">${msgs.join(' ')} Restart the server after changing .env.</div>` : '';
  view.innerHTML = `
  <div class="stack">
    <section>
      <h1>How reliable is what management says?</h1>
      <p class="muted" style="max-width:62ch">Pull a company's earnings calls, log every guidance figure and commitment with its quote, and compare each one with what was actually reported.</p>
    </section>
    <section class="panel">
      <h2>Analyze a company</h2>
      <form id="run" class="row">
        <label>Ticker<input name="ticker" required maxlength="12" placeholder="e.g. MSFT" style="width:8rem;text-transform:uppercase"></label>
        <label>Most recent calls<select name="last_n"><option>4</option><option selected>6</option><option>8</option><option>12</option></select></label>
        <label class="check"><input type="checkbox" name="cc" checked> Check commitments against later calls</label>
        <button type="submit">Pull calls and analyze</button>
      </form>
      <details style="margin-top:1rem"><summary>Paste a transcript instead</summary>
        <form id="manual" style="margin-top:.75rem">
          <div class="row">
            <label>Ticker<input name="ticker" required maxlength="12" style="width:7rem;text-transform:uppercase"></label>
            <label>Fiscal year<input name="fy" type="number" required min="2000" max="2100" style="width:7rem"></label>
            <label>Quarter<select name="q"><option>1</option><option>2</option><option>3</option><option>4</option></select></label>
            <label>Call date<input name="date" type="date"></label>
          </div>
          <label style="margin-top:.75rem">Transcript<textarea name="text" required minlength="500"></textarea></label>
          <div class="actions"><button type="submit">Extract claims</button></div>
        </form>
      </details>
    </section>
    <section>
      <h2>Companies</h2>
      ${list.length ? `<div class="panel" style="padding:0;overflow-x:auto"><table>
        <thead><tr><th>Company</th><th>Credibility score</th><th>Confidence</th><th>Calls</th><th>Claims</th><th>Open flags</th></tr></thead>
        <tbody>${list.map(c => `<tr class="click" data-t="${esc(c.ticker)}">
          <td><strong>${esc(c.ticker)}</strong></td>
          <td><span class="big" style="font-size:1.6rem">${fmtScore(c.score)}</span> <span class="muted small">${esc(c.grade || 'Not analyzed')}</span></td>
          <td>${c.confidence ? chip(c.confidence === 'high' ? 'within' : c.confidence === 'medium' ? 'medium' : 'high', c.confidence) : '–'}</td>
          <td class="num">${c.calls}</td><td class="num">${c.claims}</td><td class="num">${c.flags}</td></tr>`).join('')}
        </tbody></table></div>`
        : `<div class="panel empty">No companies yet. Enter a ticker above to pull its recent earnings calls.</div>`}
    </section>
  </div>`;
  view.querySelectorAll('tr.click').forEach(tr => tr.onclick = () => location.hash = '#/company/' + tr.dataset.t);
  $('#run').onsubmit = async e => {
    e.preventDefault(); const f = new FormData(e.target);
    try {
      const { job_id } = await api('/pipeline', { method: 'POST', body: { ticker: f.get('ticker'), last_n: +f.get('last_n'), check_commitments: !!f.get('cc') } });
      const t = f.get('ticker').toUpperCase();
      runJob(`Analyzing ${t}`, job_id, () => location.hash = '#/company/' + t);
    } catch (err) { alert(err.message); }
  };
  $('#manual').onsubmit = async e => {
    e.preventDefault(); const f = new FormData(e.target);
    try {
      const { job_id } = await api('/ingest/manual', { method: 'POST', body: { ticker: f.get('ticker'), fiscal_year: +f.get('fy'), quarter: +f.get('q'), call_date: f.get('date') || '', transcript: f.get('text') } });
      const t = f.get('ticker').toUpperCase();
      runJob(`Extracting ${t}`, job_id, () => location.hash = '#/company/' + t);
    } catch (err) { alert(err.message); }
  };
}

function rangeBar(r) {
  const g = r.guidance, a = r.actual?.value;
  const nums = [g.low, g.high, a, g.band_low, g.band_high].filter(v => v != null);
  let min = Math.min(...nums), max = Math.max(...nums);
  const pad = (max - min) * 0.3 || Math.abs(max) * 0.1 || 1; min -= pad; max += pad;
  const pos = v => ((v - min) / (max - min) * 100).toFixed(2);
  const lo = g.band_low ?? g.low, hi = g.band_high ?? g.high;
  const left = lo != null ? pos(lo) : pos(min + pad * .5), right = hi != null ? pos(hi) : 100;
  const open = lo == null || hi == null;
  const f = v => fmtVal(v, r.unit, r.basis, r.metric);
  return `<div><div class="track" role="img" aria-label="Guided ${f(g.low)} to ${f(g.high)}; actual ${f(a)}">
      <div class="band ${open ? 'open' : ''}" style="left:${left}%;width:${Math.max(1, right - left)}%"></div>
      <div class="pt ${esc(r.outcome)}" style="left:${pos(a)}%"></div></div>
    <div class="scale"><span>${f(g.low)}${g.low !== g.high && g.high != null ? ' to ' + f(g.high) : ''} guided</span><span>${f(a)} reported</span></div></div>`;
}

function guidanceTab(res) {
  const rows = (res.reconciliation || []);
  if (!rows.length) return `<div class="empty">No guidance has been logged yet.</div>`;
  const evald = rows.filter(r => r.status === 'evaluated'), other = rows.filter(r => r.status !== 'evaluated' && r.status !== 'qualitative');
  const groups = {}; evald.forEach(r => (groups[r.target_period] ||= []).push(r));
  const keys = Object.keys(groups).sort().reverse();
  return `<div class="legend"><span><i></i>Range management guided</span><span><b></b>Reported actual</span><span>Marker is green inside the range, blue above, red below.</span></div>
  ${keys.map(k => `<section class="g-group"><h3>${esc(pretty(k))} <span class="muted small">guided on ${[...new Set(groups[k].map(r => pretty(r.call_period)))].join(', ')}</span></h3>
    ${groups[k].map(r => `<div class="g-row">
      <div><div class="g-title">${esc(metricLabel(r))}</div><div class="muted small">${esc(r.speaker || '')}${r.update_type && r.update_type !== 'unspecified' ? ' · ' + esc(r.update_type) : ''}</div></div>
      ${rangeBar(r)}
      <div>${chip(r.outcome, OUT[r.outcome])}<div class="small muted num" style="margin-top:.3rem">Accuracy ${fmtScore(r.accuracy_score)}${r.deviation_pct ? ` · ${r.deviation_pct > 0 ? '+' : ''}${r.deviation_pct}% vs. range` : ''}</div>
      ${r.actual.source === 'human_override' ? '<div class="small muted">Actual set by reviewer</div>' : ''}</div></div>`).join('')}</section>`).join('')}
  ${other.length ? `<section class="g-group"><h3>Not scored</h3>${other.map(r => `<div class="g-row"><div><div class="g-title">${esc(metricLabel(r))}</div><div class="muted small">${esc(pretty(r.call_period))}</div></div>
    <div class="small">${esc(r.summary || '')}</div><div>${chip(r.status === 'pending' ? 'pending' : 'medium', STATUS[r.status] || r.status)}</div></div>`).join('')}</section>` : ''}`;
}

function commitmentsTab(res) {
  const cs = res.commitments || [];
  if (!cs.length) return `<div class="empty">No commitments have been assessed. Run the analysis with “Check commitments” turned on.</div>`;
  return cs.slice().sort((a, b) => b.call_period.localeCompare(a.call_period)).map(c => `<div class="claim">
    <div class="meta">${chip(c.status, CSTATUS[c.status] || c.status)}<strong>${esc(pretty(c.call_period))}</strong>
      <span class="muted small">${esc(c.speaker || '')}${c.deadline_text ? ' · deadline: ' + esc(c.deadline_text) : ''}</span></div>
    <div>${esc(c.summary)}</div>
    <blockquote>${esc(c.quote)}</blockquote>
    ${c.evidence_quote ? `<div class="small muted">Evidence from ${esc(pretty(c.evidence_period || ''))} ${c.evidence_verified ? '(quote verified)' : '(quote NOT verified)'}</div><blockquote>${esc(c.evidence_quote)}</blockquote>` : ''}
    <div class="small muted">${esc(c.rationale || '')}</div></div>`).join('');
}

async function ledgerTab(t, calls, host) {
  const sel = calls.length - 1;
  host.innerHTML = `<div class="row" style="margin-bottom:.75rem"><label>Call<select id="lcall">${calls.map((c, i) => `<option value="${i}" ${i === sel ? 'selected' : ''}>FY${c.fiscal_year} Q${c.quarter}${c.call_date ? ' (' + esc(c.call_date) + ')' : ''}</option>`).join('')}</select></label>
    <a class="small" id="ldl" download>Download JSON</a></div>
    <div class="filters" id="lfil"></div><div id="lbody"></div>`;
  let entry, type = 'all';
  const draw = () => {
    const claims = entry.claims.filter(c => type === 'all' || c.claim_type === type);
    $('#lfil').innerHTML = ['all', 'guidance', 'commitment', 'risk', 'statement'].map(k => `<button data-k="${k}" class="${k === type ? 'on' : ''}">${k === 'all' ? 'All' : k[0].toUpperCase() + k.slice(1)} (${k === 'all' ? entry.claims.length : entry.claims.filter(c => c.claim_type === k).length})</button>`).join('');
    $('#lfil').querySelectorAll('button').forEach(b => b.onclick = () => { type = b.dataset.k; draw(); });
    $('#lbody').innerHTML = claims.length ? claims.map(c => `<div class="claim" data-id="${esc(c.claim_id)}">
      <div class="meta">${chip(c.claim_type === 'risk' ? 'medium' : c.claim_type === 'guidance' ? 'above' : 'neutral', c.claim_type)}
        <span class="small muted">${esc(c.speaker)}${c.role ? ' (' + esc(c.role) + ')' : ''} · ${esc(c.section === 'qa' ? 'Q&A' : 'prepared remarks')} · confidence ${(c.confidence * 100).toFixed(0)}%</span>
        ${c.quote_verified ? '' : chip('high', 'quote not verified')}
        ${c.review?.status && c.review.status !== 'none' ? chip(c.review.status === 'pending' ? 'pending_review' : 'neutral', 'review: ' + c.review.status) : ''}</div>
      <div>${esc(c.summary)}${c.quantitative ? ` <span class="muted num">(${esc(METRIC[c.metric] || c.metric)}: ${esc(c.value_low)}${c.value_high != null && c.value_high !== c.value_low ? '–' + esc(c.value_high) : ''} ${esc(c.scale || '')} ${esc(c.unit || '')}${c.resolved_period ? ', ' + pretty('FY' + c.resolved_period.fiscal_year + c.resolved_period.period) : ''})</span>` : ''}</div>
      <blockquote>${esc(c.quote)}</blockquote>
      ${c.source ? `<button class="link small" data-ctx="${c.source.char_start}-${c.source.char_end}">Show in transcript</button><div class="ctx-host"></div>` : ''}
    </div>`).join('') : `<div class="empty">Nothing of this type on this call.</div>`;
    $('#lbody').querySelectorAll('[data-ctx]').forEach(b => b.onclick = async () => {
      const h = b.nextElementSibling; if (h.innerHTML) { h.innerHTML = ''; return; }
      const [s, e] = b.dataset.ctx.split('-'), c = calls[+$('#lcall').value];
      try { const x = await api(`/companies/${t}/transcript/${c.fiscal_year}/${c.quarter}?start=${s}&end=${e}`);
        h.innerHTML = `<div class="ctx">…${esc(x.before)}<mark>${esc(x.match)}</mark>${esc(x.after)}…</div>`; } catch (err) { h.textContent = err.message; }
    });
  };
  const load = async () => {
    const c = calls[+$('#lcall').value];
    entry = await api(`/companies/${t}/ledger/${c.fiscal_year}/${c.quarter}`);
    const blob = new Blob([JSON.stringify(entry, null, 2)], { type: 'application/json' });
    $('#ldl').href = URL.createObjectURL(blob); $('#ldl').download = `${t}_FY${c.fiscal_year}Q${c.quarter}.json`;
    draw();
  };
  $('#lcall').onchange = load; await load();
}

async function company(t) {
  let d;
  try { d = await api('/companies/' + t); } catch (e) { view.innerHTML = `<div class="panel empty">${esc(e.message)}. <a href="#/">Back to companies</a></div>`; return; }
  const res = d.results || {}, m = res.mcs;
  const C = m?.components || {};
  const comp = (key, label, hint) => { const c = C[key] || {}; return `<div><div class="muted small">${label}</div>
    <div class="val num">${fmtScore(c.score)}</div><div class="bar"><i style="width:${c.score ?? 0}%"></i></div>
    <div class="small muted">${hint(c)}</div></div>`; };
  view.innerHTML = `<div class="stack">
    <div><a class="small" href="#/">All companies</a><h1 style="margin-top:.25rem">${esc(d.ticker)}</h1></div>
    <section class="panel"><div class="score-head">
      <div class="score-box ${m?.provisional ? 'prov' : ''}"><div class="muted small">Management credibility score</div>
        <div class="big">${fmtScore(m?.score)}</div>
        <div style="margin-top:.4rem"><strong>${esc(m?.grade || 'Not analyzed')}</strong> ${m ? chip(m.confidence === 'high' ? 'within' : m.confidence === 'medium' ? 'medium' : 'high', m.confidence + ' confidence') : ''}</div>
        ${m?.provisional ? `<div class="small muted" style="margin-top:.4rem">Provisional: too little verified data for a firm reading.</div>` : ''}</div>
      <div><div class="comp">
        ${comp('forecast_accuracy', 'Forecast accuracy', c => `${c.n || 0} guidance items scored${c.coverage != null ? ` · ${(c.coverage * 100).toFixed(0)}% verifiable` : ''}`)}
        ${comp('commitment_delivery', 'Commitment delivery', c => `${c.n || 0} scored · ${c.unassessed || 0} without evidence · ${c.pending || 0} not yet due`)}
        ${comp('evidence_quality', 'Evidence quality', c => `${c.n || 0} claims${c.quote_verified_rate != null ? ` · ${(c.quote_verified_rate * 100).toFixed(0)}% quotes verified` : ''}`)}
      </div>
      <div class="actions"><button id="reanalyze" class="ghost">Re-run analysis</button><span class="small muted" style="align-self:center">${res.updated_at ? 'Updated ' + esc(res.updated_at.replace('T', ' ').slice(0, 16)) + ' UTC' : ''}</span></div></div>
    </div>
    ${m?.flags?.length ? `<hr style="border:0;border-top:1px solid var(--line);margin:1.25rem 0"><h3 style="margin-bottom:.6rem">Flagged for human review</h3>
      <ul class="flags">${m.flags.map(f => `<li>${chip(f.severity, f.severity)}<span>${esc(f.message)}</span></li>`).join('')}</ul>
      <p class="small" style="margin-bottom:0"><a href="#/review">Open the review queue</a></p>` : ''}
    </section>
    <div><div class="tabs" role="tablist">
      <button data-tab="g" class="on">Guidance vs. actual</button><button data-tab="c">Commitments</button><button data-tab="l">Ledger</button></div>
      <div id="tabbody"></div></div></div>`;
  const show = async tab => {
    view.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
    const body = $('#tabbody');
    if (tab === 'g') body.innerHTML = guidanceTab(res);
    if (tab === 'c') body.innerHTML = commitmentsTab(res);
    if (tab === 'l') await ledgerTab(d.ticker, d.calls, body);
  };
  view.querySelectorAll('.tabs button').forEach(b => b.onclick = () => show(b.dataset.tab));
  $('#reanalyze').onclick = async () => {
    try { const { job_id } = await api(`/companies/${t}/analyze`, { method: 'POST', body: { check_commitments: true } });
      runJob(`Re-analyzing ${t}`, job_id, () => company(t)); } catch (e) { alert(e.message); }
  };
  show('g');
}

const sel = (name, opts, cur) => `<label>${name}<select data-f="${name}">${opts.map(o => `<option value="${o}" ${o === cur ? 'selected' : ''}>${o || '(none)'}</option>`).join('')}</select></label>`;

async function review() {
  const q = await api('/review-queue');
  view.innerHTML = `<div class="stack"><section><h1>Review queue</h1>
    <p class="muted" style="max-width:62ch">Items the system could not confirm on its own: quotes it could not match, uncertain readings, and guidance with no reported actual. Approve what is right, correct what is wrong, reject what should not count.</p></section>
    <div id="rq">${q.length ? '' : `<div class="panel empty">Nothing needs review right now.</div>`}</div></div>`;
  const host = $('#rq');
  q.forEach(it => {
    const el = document.createElement('div'); el.className = 'panel review-card';
    const rec = it.reconciliation;
    el.innerHTML = `<div class="meta" style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center"><strong>${esc(it.ticker)}</strong><span>${esc(pretty(it.call_period))}</span>${chip('neutral', it.claim_type)}
      <span class="muted small">${esc(it.speaker || '')} · confidence ${((it.confidence || 0) * 100).toFixed(0)}%</span></div>
      <p style="margin:.5rem 0 0">${esc(it.summary)}</p><blockquote>${esc(it.quote)}</blockquote>
      <ul class="flags">${it.reasons.map(r => `<li>${chip(r.severity, r.severity)}<span>${esc(r.message)}</span></li>`).join('')}</ul>
      <details style="margin-top:.75rem"><summary>Correct values</summary>
        <div class="edit-grid">
          ${sel('claim_type', ['guidance', 'commitment', 'risk', 'statement'], it.claim_type)}
          ${sel('metric', ['', ...Object.keys(METRIC)], it.metric || '')}
          ${sel('basis', ['', 'absolute', 'yoy_growth_pct', 'qoq_growth_pct', 'level_pct'], it.basis || '')}
          <label>value_low<input data-f="value_low" type="number" step="any" value="${it.value_low ?? ''}"></label>
          <label>value_high<input data-f="value_high" type="number" step="any" value="${it.value_high ?? ''}"></label>
          ${sel('unit', ['', 'usd', 'percent', 'usd_per_share', 'other'], it.unit || '')}
          ${sel('scale', ['', 'units', 'thousand', 'million', 'billion', 'trillion'], it.scale || '')}
          <label>target fiscal year<input data-f="fy" type="number" value="${it.resolved_period?.fiscal_year ?? ''}"></label>
          ${sel('target period', ['', 'Q1', 'Q2', 'Q3', 'Q4', 'FY'], it.resolved_period?.period || '')}
          ${rec && ['missing_actual', 'evaluated'].includes(rec.status) ? `<label>Actual (as reported, same units)<input data-f="actual" type="number" step="any" placeholder="override FMP"></label>` : ''}
        </div></details>
      <div class="actions"><button data-a="approve">Approve as is</button><button data-a="edit" class="ghost">Save corrections</button><button data-a="reject" class="danger">Exclude from score</button></div>
      <div class="small muted" data-msg></div>`;
    el.querySelectorAll('[data-a]').forEach(b => b.onclick = async () => {
      const action = b.dataset.a, f = k => el.querySelector(`[data-f="${k}"]`)?.value;
      const num = k => f(k) === '' || f(k) == null ? null : +f(k);
      const edits = { claim_type: f('claim_type'), metric: f('metric') || null, basis: f('basis') || null, value_low: num('value_low'),
        value_high: num('value_high'), unit: f('unit') || null, scale: f('scale') || null };
      if (f('fy') && f('target period')) edits.target_period = { relation: 'explicit', fiscal_year: +f('fy'), period: f('target period') };
      try {
        await api(`/review/${it.ticker}/${encodeURIComponent(it.claim_id)}`, { method: 'POST',
          body: { action, edits: action === 'edit' ? edits : null, actual_override: action === 'edit' ? num('actual') : null, note: '' } });
        el.querySelector('[data-msg]').textContent = 'Saved. The score has been recalculated.';
        el.querySelectorAll('button').forEach(x => x.disabled = true);
        refreshReviewCount();
      } catch (e) { el.querySelector('[data-msg]').textContent = e.message; }
    });
    host.appendChild(el);
  });
}

/* ---------- router ---------- */
async function route() {
  const h = location.hash || '#/';
  document.querySelectorAll('nav a').forEach(a => a.classList.toggle('on', (h.startsWith('#/review') ? 'review' : 'home') === a.dataset.nav));
  view.innerHTML = '<div class="empty">Loading…</div>';
  try {
    if (h.startsWith('#/company/')) { $('#banner').innerHTML = ''; await company(decodeURIComponent(h.split('/')[2])); }
    else if (h.startsWith('#/review')) { $('#banner').innerHTML = ''; await review(); }
    else await home();
  } catch (e) { view.innerHTML = `<div class="panel empty">${esc(e.message)}</div>`; }
  view.focus({ preventScroll: true }); window.scrollTo(0, 0);
}
addEventListener('hashchange', route);
route(); refreshReviewCount();
