const fs = require('node:fs');
const path = require('node:path');
const { Handler, NotFoundError, ForbiddenError, PRIV } = require('hydrooj');
const { RecordDetailHandler } = require(path.join(path.dirname(require.resolve('hydrooj/package.json')), 'src/handler/record'));
const PROFILE_ROOT = '/data/tt-profiles';
// Per-problem total submission caps (all users, pretests included) from /problems/*/metadata.json.
const SUBMISSION_LIMITS = new Map();
// Leaderboard problems (language ttlang) and their reference solution's per-case device time in µs.
const LEADERBOARD_PROBLEMS = new Map();

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
  return { scheduler: report.scheduler, quality: report.quality,
    device_geomean_us: summary?.complete ? formatUs(summary.geomean_ns) : null,
    cases: (report.cases || []).map((item) => {
    const host = item.host || {}, device = item.device || {}, statistics = host.statistics || {};
    const expectedDeviceSamples = device.capture?.sample_count;
    const completeCoreGroups = (device.core_statistics || []).filter((group) =>
      expectedDeviceSamples > 0 && group.count === expectedDeviceSamples && Number.isFinite(group.median_ns));
    // Select one measured core/occurrence for the compact view; do not add parallel stages.
    const slowestCore = device.valid && completeCoreGroups.length
      ? completeCoreGroups.reduce((slowest, group) => group.median_ns > slowest.median_ns ? group : slowest) : null;
    return {
      id: item.id,
      correctness: host.correctness || null,
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

class TTLeaderboardHandler extends Handler {
  async get({ domainId }) {
    const { problem, record, user } = global.Hydro.model;
    const boards = [];
    const uids = new Set();
    for (const pid of [...LEADERBOARD_PROBLEMS.keys()].sort()) {
      const pdoc = await problem.get(domainId, pid);
      if (!pdoc) continue;
      const reference = LEADERBOARD_PROBLEMS.get(pid);
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
      rows.forEach((row, index) => { row.rank = index + 1; uids.add(row.uid); });
      const used = await record.coll.countDocuments({ domainId, pid: pdoc.docId });
      boards.push({
        pid, title: this.ttProblemTitle ? this.ttProblemTitle(pdoc) : pdoc.title, rows, used,
        limit: SUBMISSION_LIMITS.get(pid) || null,
        reference_geomean_us: formatUs(referenceGeomean),
        reference_cases: referenceNs ? referenceNs.map(formatUs) : [],
      });
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
    if (artifact !== 'report.json' && !/^case-[1-9][0-9]*-device\.csv$/.test(artifact)) throw new NotFoundError();
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
      return translation && pdoc.title === translation.title
        ? { ...pdoc, title: translation.titles[english ? 'en' : 'zh'] || pdoc.title } : pdoc;
    };
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
    }
    if (Array.isArray(body.pdocs)) body.pdocs = body.pdocs.map(localize);
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
