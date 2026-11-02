const fs = require('node:fs');
const path = require('node:path');
const { Handler, NotFoundError, ForbiddenError, PRIV } = require('hydrooj');
const { RecordDetailHandler } = require(path.join(path.dirname(require.resolve('hydrooj/package.json')), 'src/handler/record'));
const PROFILE_ROOT = '/data/tt-profiles';
// Per-problem total submission caps (all users, pretests included) from /problems/*/metadata.json.
const SUBMISSION_LIMITS = new Map();
// Leaderboard problems (language ttlang) and their reference solution's per-case device time in µs.
const LEADERBOARD_PROBLEMS = new Map();
// Problem id -> folder under /problems (for the starter code shown in the editor).
const PROBLEM_FOLDERS = new Map();
const STARTER_CACHE = new Map();

function starterFor(pid) {
  if (!STARTER_CACHE.has(pid)) {
    const folder = PROBLEM_FOLDERS.get(pid);
    let text = null;
    if (folder) {
      for (const name of ['starter.py', 'starter.cpp']) {
        const file = path.join(folder, name);
        if (fs.existsSync(file)) { text = fs.readFileSync(file, 'utf8'); break; }
      }
    }
    STARTER_CACHE.set(pid, text);
  }
  return STARTER_CACHE.get(pid);
}

// The site is English only: statements stored as {"zh": ..., "en": ...} render their English text.
function englishContent(content) {
  if (typeof content !== 'string') return content;
  try {
    const parsed = JSON.parse(content);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      if (typeof parsed.en === 'string') return parsed.en;
      const first = Object.values(parsed).find((value) => typeof value === 'string');
      if (first) return first;
    }
  } catch { /* plain markdown */ }
  return content;
}

const pct = (value) => (Number.isFinite(value) ? Math.max(0, Math.min(100, value * 100)) : null);
const COUNTER_LABELS = {
  fpu_busy: 'FPU (matrix unit) busy', sfpu_busy: 'SFPU (vector unit) busy', math_busy: 'Math pipeline busy',
  unpacker0_busy: 'Unpacker 0 busy', unpacker1_busy: 'Unpacker 1 busy', packer_busy: 'Packer busy',
  waiting_for_thcon_idle: 'waiting for ThCon idle', waiting_for_unpack_idle: 'waiting for unpacker idle',
  waiting_for_pack_idle: 'waiting for packer idle', waiting_for_math_idle: 'waiting for math idle',
  waiting_for_move_idle: 'waiting for move idle', waiting_for_cfg_idle: 'waiting for config idle',
  waiting_for_sfpu_idle: 'waiting for SFPU idle', waiting_for_nonzero_semaphore: 'waiting on semaphore (data not ready)',
  waiting_for_nonfull_semaphore: 'waiting on semaphore (buffer full)',
  waiting_for_srca_valid: 'waiting for SrcA valid', waiting_for_srcb_valid: 'waiting for SrcB valid',
  waiting_for_srca_clear: 'waiting for SrcA clear', waiting_for_srcb_clear: 'waiting for SrcB clear',
};
const THREAD_LABELS = { unpack: 'Unpack thread (TRISC0)', math: 'Math thread (TRISC1)', pack: 'Pack thread (TRISC2)' };

function countersView(counters) {
  const summary = counters?.summary;
  if (!summary) return null;
  const units = summary.units || {};
  return {
    valid: !!summary.valid, cores: summary.cores, window_us: Number.isFinite(summary.window_us) ? summary.window_us.toFixed(2) : '—',
    any_stall_pct: pct(summary.any_thread_stalled),
    fpu_pct: pct(units.fpu_busy), sfpu_pct: pct(units.sfpu_busy), pack_pct: pct(units.packer_busy),
    unpack_pct: pct(Math.max(units.unpacker0_busy ?? -1, units.unpacker1_busy ?? -1) >= 0 ? Math.max(units.unpacker0_busy ?? 0, units.unpacker1_busy ?? 0) : NaN),
    units: Object.entries(summary.units || {}).map(([key, value]) => ({ key, label: COUNTER_LABELS[key] || key, pct: pct(value), max_pct: pct((summary.units_busiest_core || {})[key]) })),
    threads: Object.entries(summary.threads || {}).map(([key, thread]) => ({
      key, label: THREAD_LABELS[key] || key, stalled_pct: pct(thread.stalled),
      ipc: Number.isFinite(thread.instructions_per_cycle) ? thread.instructions_per_cycle.toFixed(3) : '—',
      reasons: Object.entries(thread.reasons || {}).filter(([, value]) => value >= 0.005).slice(0, 5)
        .map(([reason, value]) => ({ label: COUNTER_LABELS[reason] || reason, pct: pct(value) })),
    })),
    raw: Object.entries(counters.counters || {}).map(([name, entry]) => ({
      name, unit: entry.unit, mean: entry.unit === 'events_per_cycle' ? entry.mean.toFixed(4) : `${(entry.mean * 100).toFixed(1)}%`,
      max_core: entry.unit === 'events_per_cycle' ? entry.max_core.toFixed(4) : `${(entry.max_core * 100).toFixed(1)}%`,
    })),
    artifacts: counters.artifacts || [], errors: counters.errors || [],
  };
}

// Device kernel time of one testcase: for every measured sample, first kernel start to last kernel end
// over all cores and RISCs (tt-metal's DEVICE KERNEL DURATION definition), summed over the programs in
// that sample; then the median over samples. Host dispatch, JIT and transfers are excluded.
function deviceTimeNs(device) {
  if (!device || !device.valid) return null;
  const samples = device.capture?.sample_count;
  const mhz = device.clock_mhz;
  if (!(samples > 0) || !(mhz > 0)) return null;
  const bySample = new Map();
  for (const span of device.core_spans || []) {
    if (!Number.isInteger(span.sample)) continue;
    const programs = bySample.get(span.sample) || new Map();
    bySample.set(span.sample, programs);
    const key = `${span.device_id}:${span.program_index}`;
    const current = programs.get(key) || { start: Infinity, end: -Infinity };
    current.start = Math.min(current.start, span.start_cycle);
    current.end = Math.max(current.end, span.end_cycle);
    programs.set(key, current);
  }
  if (bySample.size !== samples) return null;
  const cycles = [...bySample.values()]
    .map((programs) => [...programs.values()].reduce((total, p) => total + (p.end - p.start), 0))
    .sort((a, b) => a - b);
  const middle = cycles.length >> 1;
  const median = cycles.length % 2 ? cycles[middle] : (cycles[middle - 1] + cycles[middle]) / 2;
  return median * 1000 / mhz;
}

const geometricMean = (values) => Math.exp(values.reduce((sum, value) => sum + Math.log(value), 0) / values.length);
const formatUs = (ns) => (Number.isFinite(ns) ? (ns >= 1e5 ? (ns / 1000).toFixed(1) : (ns / 1000).toFixed(2)) : '—');

function deviceSummary(report) {
  const cases = report?.cases || [];
  if (!cases.length) return null;
  const times = cases.map((item) => deviceTimeNs(item.device));
  const complete = times.every((value) => value > 0);
  return { complete, times, geomean_ns: complete ? geometricMean(times) : null };
}

// Finished submissions never change except by rejudge, which publishes a new attempt id.
const SUMMARY_CACHE = new Map();
function cachedSummary(rid) {
  if (!/^[a-f0-9]{24}$/.test(rid)) return null;
  let latest;
  try { latest = JSON.parse(fs.readFileSync(path.join(PROFILE_ROOT, rid, 'latest.json'), 'utf8')); }
  catch { return null; }
  if (latest.state !== 'terminal') return null;
  const hit = SUMMARY_CACHE.get(rid);
  if (hit && hit.attempt === latest.attempt_id) return hit.summary;
  const report = loadProfile(rid);
  const summary = report && report.final?.status === 1 ? deviceSummary(report) : null;
  SUMMARY_CACHE.set(rid, { attempt: latest.attempt_id, summary });
  return summary;
}

class SubmissionLimitError extends ForbiddenError {
  constructor(...args) {
    super(...args);
    this.name = 'SubmissionLimitError';
    this.msg = () => 'This problem has reached its submission limit ({0} of {1} submissions used across all accounts). Ask the instructor to raise the limit.';
  }
}

async function submissionsUsed(domainId, docId) {
  // Every record on the problem consumes hardware time: normal submissions and pretests alike.
  // Rejudging reuses the record, so it is not counted twice.
  return global.Hydro.model.record.coll.countDocuments({ domainId, pid: docId });
}

function quotaBanner(content, used, limit) {
  const en = `> **Submission quota:** ${used} / ${limit} used on this problem (all accounts, pretests included).\n\n`;
  const zh = `> **提交配额：** 本题已使用 ${used} / ${limit} 次（所有账号、含 pretest）。\n\n`;
  try {
    const parsed = JSON.parse(content);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      for (const key of Object.keys(parsed)) parsed[key] = (String(key).startsWith('zh') ? zh : en) + parsed[key];
      return JSON.stringify(parsed);
    }
  } catch { /* plain markdown statement */ }
  return en + content;
}
const sourceRef = process.env.TTOJ_SOURCE_REF || 'main';
const SOURCE_URL = `https://github.com/gujialiang123/TTOJ/tree/${encodeURIComponent(sourceRef)}`;

function loadProfile(rid) {
  if (!/^[a-f0-9]{24}$/.test(String(rid))) return null;
  try { return JSON.parse(fs.readFileSync(path.join(PROFILE_ROOT, String(rid), 'report.json'), 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') console.warn('Cannot read TT profile:', error.message); return null; }
}

function displayProfile(report) {
  const nsToUs = (value) => Number.isFinite(value) ? (value / 1000).toFixed(3) : '—';
  const cycles = (value) => Number.isFinite(value) ? String(Number(value.toFixed(3))) : '—';
  const statisticView = (statistic) => ({
    device_id: statistic.device_id, program_index: statistic.program_index, count: statistic.count,
    risc: statistic.risc, zone: statistic.zone,
    display_core: `${statistic.core_x ?? '?'},${statistic.core_y ?? '?'}`,
    median_cycles: cycles(statistic.median_cycles), p95_cycles: cycles(statistic.p95_cycles),
    min_cycles: cycles(statistic.min_cycles), max_cycles: cycles(statistic.max_cycles),
    display_median_us: nsToUs(statistic.median_ns), display_p95_us: nsToUs(statistic.p95_ns),
  });
  // Keep raw samples, telemetry and thousands of events in the download only.
  // The record page receives a bounded view model rather than the full report.
  const summary = deviceSummary(report);
  const cases = report.cases || [];
  const allCounters = cases.map((item) => countersView(item.counters)).filter(Boolean);
  const meanOf = (key) => {
    const values = allCounters.map((c) => c.units.find((u) => u.key === key)?.pct).filter((v) => Number.isFinite(v));
    return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  };
  return { scheduler: report.scheduler, quality: report.quality,
    device_geomean_us: summary?.complete ? formatUs(summary.geomean_ns) : null,
    device_complete: !!summary?.complete,
    counters_available: allCounters.length > 0,
    overall: { fpu_pct: meanOf('fpu_busy'), sfpu_pct: meanOf('sfpu_busy'), unpack_pct: Math.max(meanOf('unpacker0_busy') ?? 0, meanOf('unpacker1_busy') ?? 0), pack_pct: meanOf('packer_busy'),
      cores_max: Math.max(0, ...cases.map((item) => new Set((item.device?.core_spans || []).map((span) => `${span.device_id}:${span.core_x},${span.core_y}`)).size)) },
    cases: cases.map((item) => {
    const host = item.host || {}, device = item.device || {}, statistics = host.statistics || {};
    const expectedDeviceSamples = device.capture?.sample_count;
    const completeCoreGroups = (device.core_statistics || []).filter((group) =>
      expectedDeviceSamples > 0 && group.count === expectedDeviceSamples && Number.isFinite(group.median_ns));
    // Select one measured core/occurrence for the compact view; do not add parallel stages.
    const slowestCore = device.valid && completeCoreGroups.length
      ? completeCoreGroups.reduce((slowest, group) => group.median_ns > slowest.median_ns ? group : slowest) : null;
    return {
      id: item.id,
      status: item.status, status_name: ({ 1: 'AC', 2: 'WA', 3: 'TLE', 4: 'MLE', 5: 'OLE', 6: 'RE', 7: 'CE', 8: 'SE' })[item.status] || (item.status == null ? '—' : String(item.status)),
      correctness: host.correctness || null,
      counters: countersView(item.counters),
      display_device_time_us: formatUs(deviceTimeNs(device)),
      device_summary: (() => {
        const ns = deviceTimeNs(device);
        if (!(ns > 0)) return null;
        const traffic = device.traffic || {};
        const bytes = (traffic.required_read_bytes || 0) + (traffic.required_write_bytes || 0);
        const cores = new Set((device.core_spans || []).map((span) => `${span.device_id}:${span.core_x},${span.core_y}`)).size;
        return {
          display_time_us: formatUs(ns), cores,
          display_mb: bytes ? (bytes / 1e6).toFixed(2) : '—',
          display_bw: bytes ? (bytes / ns).toFixed(1) : '—',
        };
      })(),
      host: {
        available: host.available, reason: host.reason,
        warmup_count: host.warmup_count, sample_count: host.sample_count,
        clock_resolution_ns: host.clock_resolution_ns, timing_scope: host.timing_scope,
        statistics: { median_ci95_ns: statistics.median_ci95_ns },
        display: {
          median: nsToUs(statistics.median_ns), p95: nsToUs(statistics.p95_ns),
          min: nsToUs(statistics.min_ns), max: nsToUs(statistics.max_ns), mad: nsToUs(statistics.mad_ns),
          ci_low: nsToUs(statistics.median_ci95_ns?.[0]), ci_high: nsToUs(statistics.median_ci95_ns?.[1]),
          cv: Number.isFinite(statistics.cv_percent) ? statistics.cv_percent.toFixed(2) : '—',
        },
      },
      device: {
        available: device.available, valid: device.valid, reason: device.reason,
        summary: slowestCore ? statisticView(slowestCore) : null,
        clock_mhz: device.clock_mhz, capture: device.capture, artifacts: device.artifacts,
        display_warmup: device.capture?.warmup_count ?? device.capture?.warmup ?? '—',
        display_samples: device.capture?.sample_count ?? '—',
        statistics_count: (device.statistics || []).length,
        statistics: (device.statistics || []).slice(0, 100).map(statisticView),
        core_statistics_count: (device.core_statistics || []).length,
        core_statistics: (device.core_statistics || []).slice(0, 100).map(statisticView),
        kernel_count: (device.kernels || []).length,
        kernels: (device.kernels || []).slice(0, 100).map((kernel) => ({
          risc: kernel.risc, zone: kernel.zone, duration_cycles: kernel.duration_cycles,
          display_core: typeof kernel.core === 'object' ? JSON.stringify(kernel.core) : (kernel.core || `${kernel.core_x ?? '?'},${kernel.core_y ?? '?'}`),
          display_us: Number.isFinite(kernel.duration_cycles) && device.clock_mhz > 0 ? (kernel.duration_cycles / device.clock_mhz).toFixed(3) : '—',
        })),
      },
    };
  }) };
}

async function leaderboardFor(domainId, pdoc) {
  const { record } = global.Hydro.model;
  const reference = LEADERBOARD_PROBLEMS.get(pdoc.pid);
  const referenceNs = reference && reference.length ? reference.map((us) => us * 1000) : null;
  const referenceGeomean = referenceNs ? geometricMean(referenceNs) : null;
  const accepted = await record.coll.find({ domainId, pid: pdoc.docId, status: 1 })
    .project({ _id: 1, uid: 1, judgeAt: 1 }).toArray();
  const counts = await record.coll.aggregate([
    { $match: { domainId, pid: pdoc.docId } }, { $group: { _id: '$uid', count: { $sum: 1 } } },
  ]).toArray();
  const submissionsByUser = new Map(counts.map((item) => [item._id, item.count]));
  const best = new Map();
  for (const rdoc of accepted) {
    const summary = cachedSummary(rdoc._id.toString());
    if (!summary?.complete) continue;
    const previous = best.get(rdoc.uid);
    if (!previous || summary.geomean_ns < previous.geomean_ns) best.set(rdoc.uid, { ...summary, rid: rdoc._id.toString(), judgeAt: rdoc.judgeAt });
  }
  const rows = [...best.entries()].map(([uid, summary]) => ({
    uid, rid: summary.rid, judgeAt: summary.judgeAt, geomean_ns: summary.geomean_ns,
    display_geomean_us: formatUs(summary.geomean_ns),
    speedup: referenceGeomean ? (referenceGeomean / summary.geomean_ns).toFixed(2) : '—',
    submissions: submissionsByUser.get(uid) || 0,
    cases: summary.times.map((ns, index) => ({
      id: index + 1, display_us: formatUs(ns),
      speedup: referenceNs && referenceNs[index] ? (referenceNs[index] / ns).toFixed(2) : '—',
    })),
  })).sort((a, b) => a.geomean_ns - b.geomean_ns);
  rows.forEach((row, index) => { row.rank = index + 1; });
  const used = await record.coll.countDocuments({ domainId, pid: pdoc.docId });
  return {
    pid: pdoc.pid, rows, used, limit: SUBMISSION_LIMITS.get(pdoc.pid) || null,
    reference_geomean_ns: referenceGeomean, reference_geomean_us: formatUs(referenceGeomean),
    reference_cases: referenceNs ? referenceNs.map(formatUs) : [],
  };
}

class TTLeaderboardHandler extends Handler {
  async get({ domainId }) {
    const { problem, user } = global.Hydro.model;
    const boards = [];
    const uids = new Set();
    for (const pid of [...LEADERBOARD_PROBLEMS.keys()].sort()) {
      const pdoc = await problem.get(domainId, pid);
      if (!pdoc) continue;
      const board = await leaderboardFor(domainId, pdoc);
      board.title = this.ttProblemTitle ? this.ttProblemTitle(pdoc) : pdoc.title;
      board.rows.forEach((row) => uids.add(row.uid));
      boards.push(board);
    }
    this.response.template = 'tt_leaderboard.html';
    this.response.body = { boards, udict: await user.getList(domainId, [...uids]), updatedAt: new Date().toISOString() };
  }
}

class TTProfilingHandler extends Handler {
  async get() { this.response.template = 'tt_profiling.html'; this.response.body = {}; }
}

class TTProfileDownloadHandler extends RecordDetailHandler {
  async get(args) {
    // Reuse all upstream record, contest, teammate and visibility checks.
    await super.get(args.domainId, this.rdoc._id, false);
    if (typeof this.response.body?.rdoc?.status !== 'number') throw new ForbiddenError();
    const artifact = args.artifact || 'report.json';
    if (artifact !== 'report.json' && !/^case-[1-9][0-9]*-(device|counters)\.csv$/.test(artifact)) throw new NotFoundError();
    const filename = path.join(PROFILE_ROOT, this.rdoc._id.toString(), artifact);
    if (!fs.existsSync(filename) || !fs.statSync(filename).isFile()) throw new NotFoundError();
    this.response.template = null;
    this.response.type = artifact.endsWith('.json') ? 'application/json' : 'text/csv';
    this.response.disposition = `attachment; filename="${this.rdoc._id}-${artifact}"`;
    this.response.body = fs.createReadStream(filename);
  }
}

exports.apply = function apply(ctx) {
  // Hydro 5.0.7 parses hydrooj.langs in model/setting before model/system has loaded the database
  // values, so after a cold start the language table is empty and every submission fails with
  // "Invalid problem config." Re-apply the stored table once the app has started.
  const refreshLanguages = () => {
    try {
      const { parseLang } = require('@hydrooj/common');
      const { setting, system } = global.Hydro.model;
      const parsed = parseLang(system.get('hydrooj.langs'));
      for (const key of Object.keys(setting.langs)) delete setting.langs[key];
      Object.assign(setting.langs, parsed);
      console.log(`tt-site: language table loaded (${Object.keys(parsed).join(', ')})`);
    } catch (error) { console.warn('tt-site: could not refresh the language table:', error.message); }
  };
  ctx.on('app/started', refreshLanguages);
  if (process.env.HYDRO_CLI) return;
  const defaultLanguage = (handler) => {
    // Explicit account/session language choices remain authoritative.
    // New visitors use English even when their browser advertises another language.
    if (!handler.user.viewLang && !handler.session.viewLang) handler.session.viewLang = 'en';
  };
  ctx.on('handler/create/http', defaultLanguage);
  ctx.on('handler/create/ws', defaultLanguage);
  // Block both steps, including existing registration links and administrator sessions.
  // New accounts are created through Hydro's authenticated administration tools.
  ctx.on('handler/create/http', (handler) => {
    if (['UserRegisterHandler', 'UserRegisterWithCodeHandler'].includes(handler.constructor.name)) {
      throw new ForbiddenError();
    }
  });
  const titleTranslations = new Map();
  for (const entry of fs.readdirSync('/problems', { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    try {
      const manifest = JSON.parse(fs.readFileSync(path.join('/problems', entry.name, 'metadata.json'), 'utf8'));
      if (manifest.titles) titleTranslations.set(manifest.id, { title: manifest.title, titles: manifest.titles });
      if (Number.isInteger(manifest.submission_limit) && manifest.submission_limit > 0) SUBMISSION_LIMITS.set(manifest.id, manifest.submission_limit);
      if (manifest.language === 'ttlang') LEADERBOARD_PROBLEMS.set(manifest.id, Array.isArray(manifest.reference_device_us) ? manifest.reference_device_us : null);
      PROBLEM_FOLDERS.set(manifest.id, path.join('/problems', entry.name));
    } catch (error) { if (error.code !== 'ENOENT') console.warn(error.message); }
  }
  const englishFor = (handler) => {
    const language = handler.request.query.lang || handler.user.viewLang || handler.session.viewLang || handler.translate('__langname');
    return String(language).startsWith('en') || language === 'English';
  };
  const localizedTitle = (handler, pdoc) => {
    const translation = titleTranslations.get(pdoc?.pid);
    if (!translation || pdoc.title !== translation.title) return pdoc?.title;
    return translation.titles[englishFor(handler) ? 'en' : 'zh'] || pdoc.title;
  };
  ctx.on('handler/create', (handler) => {
    handler.ttProblemTitle = (pdoc) => localizedTitle(handler, pdoc);
  });
  // Hide the exact upstream sample in lists, including administrator and AJAX views.
  // Its database document and submissions remain available for recovery.
  ctx.on('problem/list', (query, handler) => {
    if (handler.args.domainId !== 'system') return;
    query.$nor ||= [];
    query.$nor.push({ pid: 'P1000', owner: 1, title: 'A+B Problem' });
  });
  // Enforce the per-problem cap before Hydro creates the record (runs after prepare, so pdoc is loaded).
  ctx.on('handler/before/ProblemSubmit#post', async (handler) => {
    const limit = SUBMISSION_LIMITS.get(handler.pdoc?.pid);
    if (!limit) return;
    const used = await submissionsUsed(handler.args.domainId, handler.pdoc.docId);
    if (used >= limit) throw new SubmissionLimitError(used, limit);
  });
  ctx.Route('tt_profiling', '/profiling', TTProfilingHandler);
  ctx.Route('tt_leaderboard', '/leaderboard', TTLeaderboardHandler, PRIV.PRIV_USER_PROFILE);
  global.Hydro.ui.inject('Nav', 'tt_leaderboard', { prefix: 'tt_leaderboard', before: 'record_main' }, PRIV.PRIV_USER_PROFILE);
  ctx.Route('tt_profile_json', '/record/:rid/profile', TTProfileDownloadHandler);
  ctx.Route('tt_profile_artifact', '/record/:rid/profile/:artifact', TTProfileDownloadHandler);
  ctx.on('handler/after', async (handler) => {
    if (!handler.response.body || typeof handler.response.body !== 'object') return;
    const body = handler.response.body;
    body.ttSourceUrl = SOURCE_URL;
    const english = englishFor(handler);
    const localize = (pdoc) => {
      const translation = titleTranslations.get(pdoc?.pid);
      const title = translation && pdoc.title === translation.title ? (translation.titles.en || pdoc.title) : pdoc?.title;
      return pdoc && typeof pdoc === 'object' ? { ...pdoc, title, content: englishContent(pdoc.content) } : pdoc;
    };
    body.ttTheme = handler.user?.theme === 'dark' ? 'dark' : 'light';
    body.ttLoggedIn = !!handler.user?.hasPriv?.(PRIV.PRIV_USER_PROFILE);
    if (body.pdoc) {
      const oldTitle = body.pdoc.title;
      body.pdoc = localize(body.pdoc);
      if (handler.UiContext.extraTitleContent === oldTitle) handler.UiContext.extraTitleContent = body.pdoc.title;
      const limit = SUBMISSION_LIMITS.get(body.pdoc.pid);
      if (limit && handler.response.template === 'problem_detail.html' && typeof body.pdoc.content === 'string') {
        const used = await submissionsUsed(handler.args.domainId, body.pdoc.docId);
        body.pdoc = { ...body.pdoc, content: quotaBanner(body.pdoc.content, used, limit) };
        body.ttSubmissionQuota = { used, limit, remaining: Math.max(0, limit - used) };
      }
      if (handler.response.template === 'problem_detail.html' && handler.UiContext) {
        const starter = starterFor(body.pdoc.pid);
        const isTTLang = LEADERBOARD_PROBLEMS.has(body.pdoc.pid);
        // LeetCode-style view: statement on the left, editor with the starter on the right, submissions below.
        // problem_detail.html copies handler.user.codeTemplate/codeLang into UiContext for the editor.
        if (isTTLang && handler.user) {
          if (starter) handler.user.codeTemplate = starter;
          handler.user.codeLang = 'ttlang';
        }
        handler.UiContext.ttWorkspace = { auto: isTTLang && body.ttLoggedIn, hasStarter: !!starter, lang: isTTLang ? 'ttlang' : null };
      }
    }
    if (handler.response.template === 'record_detail.html' && body.rdoc && body.pdoc && LEADERBOARD_PROBLEMS.has(body.pdoc.pid)) {
      try {
        const board = await leaderboardFor(handler.args.domainId, body.pdoc);
        const mine = board.rows.find((row) => row.uid === body.rdoc.uid);
        const own = board.rows.find((row) => row.rid === String(body.rdoc._id));
        body.ttRank = { total: board.rows.length, rank: own ? own.rank : null, best_rank: mine ? mine.rank : null,
          speedup: own ? own.speedup : null, reference_geomean_us: board.reference_geomean_us,
          used: board.used, limit: board.limit, is_best: !!own };
      } catch (error) { console.warn('tt-site: rank lookup failed:', error.message); }
    }
    if (Array.isArray(body.pdocs)) body.pdocs = body.pdocs.map(localize);
    if (body.pdoc && !handler.response.template?.startsWith('problem_detail')) body.pdoc = localize(body.pdoc);
    if (body.pdict) body.pdict = Object.fromEntries(Object.entries(body.pdict).map(([id, pdoc]) => [id, localize(pdoc)]));
    body.ttEnglish = english;
    body.overrideNav = global.Hydro.ui.getNodes('Nav').filter((item) =>
      ['homepage', 'problem_main', 'tt_leaderboard', 'record_main', 'domain_dashboard', 'manage_dashboard'].includes(item.name)
      && (item.name !== 'manage_dashboard' || handler.user.hasPriv(PRIV.PRIV_EDIT_SYSTEM)));
    if (handler.response.template === 'record_detail.html' && typeof body.rdoc?.status === 'number') {
      const profile = loadProfile(body.rdoc._id);
      body.ttProfile = profile ? displayProfile(profile) : null;
    }
  });
};
