const fs = require('node:fs');
const path = require('node:path');
const { Handler, NotFoundError, ForbiddenError, PRIV } = require('hydrooj');
const { RecordDetailHandler } = require(path.join(path.dirname(require.resolve('hydrooj/package.json')), 'src/handler/record'));
const PROFILE_ROOT = '/data/tt-profiles';
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
  return { scheduler: report.scheduler, quality: report.quality, cases: (report.cases || []).map((item) => {
    const host = item.host || {}, device = item.device || {}, statistics = host.statistics || {};
    const expectedDeviceSamples = device.capture?.sample_count;
    const completeCoreGroups = (device.core_statistics || []).filter((group) =>
      expectedDeviceSamples > 0 && group.count === expectedDeviceSamples && Number.isFinite(group.median_ns));
    // Select one measured core/occurrence for the compact view; do not add parallel stages.
    const slowestCore = device.valid && completeCoreGroups.length
      ? completeCoreGroups.reduce((slowest, group) => group.median_ns > slowest.median_ns ? group : slowest) : null;
    return {
      id: item.id,
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
  ctx.Route('tt_profiling', '/profiling', TTProfilingHandler);
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
    }
    if (Array.isArray(body.pdocs)) body.pdocs = body.pdocs.map(localize);
    if (body.pdict) body.pdict = Object.fromEntries(Object.entries(body.pdict).map(([id, pdoc]) => [id, localize(pdoc)]));
    body.ttEnglish = english;
    body.overrideNav = global.Hydro.ui.getNodes('Nav').filter((item) =>
      ['homepage', 'problem_main', 'record_main', 'domain_dashboard', 'manage_dashboard'].includes(item.name)
      && (item.name !== 'manage_dashboard' || handler.user.hasPriv(PRIV.PRIV_EDIT_SYSTEM)));
    if (handler.response.template === 'record_detail.html' && typeof body.rdoc?.status === 'number') {
      const profile = loadProfile(body.rdoc._id);
      body.ttProfile = profile ? displayProfile(profile) : null;
    }
  });
};
