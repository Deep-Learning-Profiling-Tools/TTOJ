const fs = require('node:fs');
const path = require('node:path');
const testText = (value) => typeof value === 'string' ? value : `${JSON.stringify(value ?? '')}\n`;
const starterEnglishComment = '    # a, b: device 0 TTNN tensors. Return a device tensor.\n';
const starterChineseComment = '    # a、b 已在 device 0 上；返回设备张量。\n';

// Remove only the known duplicated stock comment; preserve other administrator edits.
function localizeStarterComments(content) {
  try {
    const localized = JSON.parse(content);
    if (typeof localized.en !== 'string' || typeof localized.zh !== 'string') return content;
    let changed = false;
    for (const [language, remove, retain] of [
      ['en', starterChineseComment, starterEnglishComment],
      ['zh', starterEnglishComment, starterChineseComment],
    ]) {
      if (localized[language].includes(remove) && localized[language].includes(retain)) {
        localized[language] = localized[language].replace(remove, '');
        changed = true;
      }
    }
    return changed ? JSON.stringify(localized) : content;
  } catch { return content; }
}

async function migrateStockStarter(doc, folder) {
  if (!['TT1001', 'TT1002'].includes(doc.pid)) return;
  const { problem, storage } = global.Hydro.model;
  const backupFile = '/root/.hydro/tt-starter-localization-backup.json';
  const backup = fs.existsSync(backupFile) ? JSON.parse(fs.readFileSync(backupFile, 'utf8')) : {};
  const preserve = (field, value) => {
    backup[doc.pid] ||= {};
    if (!(field in backup[doc.pid])) backup[doc.pid][field] = value;
    fs.writeFileSync(backupFile, JSON.stringify(backup, null, 2), { mode: 0o600 });
  };
  const content = localizeStarterComments(doc.content);
  if (content !== doc.content) {
    preserve('content', doc.content);
    await problem.edit('system', doc.docId, { content });
    console.log(`Localized stock starter comments in ${doc.pid}`);
  }
  if (!(doc.additional_file || []).some((file) => file.name === 'starter.py')) return;
  const starter = fs.readFileSync(path.join(folder, 'starter.py'), 'utf8');
  const oldStarter = starter.replace(starterEnglishComment, starterEnglishComment + starterChineseComment);
  const stream = await storage.get(`problem/system/${doc.docId}/additional_file/starter.py`);
  const chunks = [];
  for await (const chunk of stream) chunks.push(Buffer.from(chunk));
  const existing = Buffer.concat(chunks).toString('utf8');
  // An attachment changed by an administrator must never be replaced.
  if (existing === oldStarter && existing !== starter) {
    preserve('starter.py', existing);
    await problem.addAdditionalFile('system', doc.docId, 'starter.py', Buffer.from(starter), 3);
    console.log(`Localized stock starter attachment in ${doc.pid}`);
  }
}

module.exports = async function bootstrap({ reseedProblems = false } = {}) {
  const { user, system, domain, problem } = global.Hydro.model;
  const { PRIV, PERM } = require('hydrooj');
  const accounts = [
    { uid: 3, uname: process.env.ADMIN_USERNAME || 'admin', password: process.env.ADMIN_PASSWORD, mail: 'admin@tt.local', admin: true },
    { uid: 2, uname: process.env.JUDGE_USERNAME || 'judge', password: process.env.JUDGE_PASSWORD, mail: 'judge@tt.local', admin: false },
  ];
  for (const account of accounts) {
    if (!account.password) throw new Error(`Missing password for ${account.uname}`);
    const existing = await user.getById('system', account.uid);
    if (!existing) await user.create(account.mail, account.uname, account.password, account.uid);
    else if (existing.uname !== account.uname) throw new Error(`UID ${account.uid} is already used by another account`);
    if (account.admin) await user.setSuperAdmin(account.uid);
    else await user.setJudge(account.uid);
  }

  const policyBackup = '/root/.hydro/tt-access-policy-backup.json';
  const guest = await user.getById('system', 0);
  const currentRoles = await domain.getRoles('system');
  if (!fs.existsSync(policyBackup)) fs.writeFileSync(policyBackup, JSON.stringify({
    defaultPriv: system.get('default.priv'), guestPriv: guest?.priv,
    roles: Object.fromEntries(currentRoles.map((role) => [role._id, role.perm.toString()])),
  }, null, 2), { mode: 0o600 });
  await system.set('default.priv', system.get('default.priv') & ~PRIV.PRIV_REGISTER_USER);
  if (guest) await user.setPriv(0, guest.priv & ~PRIV.PRIV_REGISTER_USER);
  const problemManagement = PERM.PERM_CREATE_PROBLEM | PERM.PERM_EDIT_PROBLEM
    | PERM.PERM_EDIT_PROBLEM_SELF | PERM.PERM_REJUDGE_PROBLEM | PERM.PERM_REJUDGE;
  await domain.setRoles('system', Object.fromEntries(currentRoles
    .filter((role) => ['guest', 'default'].includes(role._id))
    .map((role) => [role._id, role.perm & ~problemManagement])));

  const backupFile = '/root/.hydro/tt-site-cleanup-backup.json';
  if (!fs.existsSync(backupFile)) fs.writeFileSync(backupFile, JSON.stringify({
    homepage: system.get('hydrooj.homepage'),
    about: system.get('ui-default.about'),
    domain: await domain.get('system'),
  }, null, 2), { mode: 0o600 });
  await system.set('server.name', 'Tenstorrent OJ');
  await system.set('server.host', '0.0.0.0');
  await system.set('server.language', 'en');
  await system.set('hydrooj.homepage', JSON.stringify([{ width: 12, recent_problems: 4 }]));
  await system.set('ui-default.about', '# about Tenstorrent OJ\n本地 Blackhole 代码评测与性能实验平台。\nLocal Blackhole code evaluation and profiling.\nPowered by Hydro; AGPL-3.0-or-later.\n');
  await system.set('hydrooj.langs', JSON.stringify({
    ttnn: { display: 'Tenstorrent TTNN (Python)', code_file: 'submission.py', execute: 'python3 submission.py', highlight: 'python', monaco: 'python', comment: '#', process_limit: 128 },
    ttlang: { display: 'Tenstorrent TT-Lang (Python)', code_file: 'submission.py', execute: 'python3 submission.py', highlight: 'python', monaco: 'python', comment: '#', process_limit: 128, pretest: false },
    'metalium-riscv': { display: 'Tenstorrent Metalium (RISC-V C++)', code_file: 'submission.cpp', execute: '/w/submission', highlight: 'cpp', monaco: 'cpp', comment: '//', process_limit: 128 },
    'metalium-compute': { display: 'Tenstorrent Metalium (Compute C++)', code_file: 'submission.cpp', execute: '/w/submission', highlight: 'cpp', monaco: 'cpp', comment: '//', process_limit: 128 },
  }, null, 2));
  await domain.edit('system', {
    name: 'Tenstorrent OJ',
    bulletin: JSON.stringify({ zh: '在 Blackhole 上评测 TTNN 与 Metalium，查看正确性、日志与性能剖析。', en: 'Evaluate TTNN and Metalium on Blackhole; inspect correctness, logs, and profiling.' }),
  });
  const welcomeProblem = await problem.get('system', 'P1000');
  if (welcomeProblem?.owner === 1 && welcomeProblem.title === 'A+B Problem') {
    await problem.edit('system', welcomeProblem.docId, { hidden: true });
    await global.Hydro.model.task.deleteMany({ domainId: 'system', pid: welcomeProblem.docId, uid: 1 });
    await global.Hydro.model.record.coll.updateMany(
      { domainId: 'system', pid: welcomeProblem.docId, uid: 1, status: 0 },
      { $set: { status: 9, judgeAt: new Date() } },
    );
  }

  const problemsDir = '/problems';
  let seeded = 0;
  if (fs.existsSync(problemsDir)) {
    for (const entry of fs.readdirSync(problemsDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const folder = path.join(problemsDir, entry.name);
      const manifestPath = path.join(folder, 'metadata.json');
      if (!fs.existsSync(manifestPath)) continue;
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      if (!manifest.id || !manifest.title || !manifest.language) throw new Error(`Invalid problem metadata: ${entry.name}`);
      const contentPath = path.join(folder, 'statement.md');
      const content = manifest.content || (fs.existsSync(contentPath) ? fs.readFileSync(contentPath, 'utf8') : manifest.title);
      let doc = await problem.get('system', manifest.id);
      // Preserve administrator edits to statements, test data and attachments across restarts.
      // Replacing existing bundled problems requires the explicit reseed command.
      if (doc && !reseedProblems) {
        await migrateStockStarter(doc, folder);
        continue;
      }
      const pid = doc?.docId || await problem.add('system', manifest.id, manifest.title, content, 3, ['Tenstorrent', manifest.language]);
      await problem.edit('system', pid, { title: manifest.title, content, hidden: false });
      const cases = [];
      for (const [index, test] of (manifest.tests || []).entries()) {
        const input = `${index + 1}.in`;
        const output = `${index + 1}.out`;
        await problem.addTestdata('system', pid, input, Buffer.from(testText(test.input ?? '')), 3);
        await problem.addTestdata('system', pid, output, Buffer.from(testText(test.output ?? '')), 3);
        cases.push({ input, output, score: 100 / manifest.tests.length });
      }
      const config = {
        type: 'default', langs: [manifest.language],
        time: `${manifest.time_ms || 120000}ms`, memory: '8g',
        cases,
      };
      await problem.addTestdata('system', pid, 'manifest.json', Buffer.from(JSON.stringify(manifest)), 3);
      await problem.addTestdata('system', pid, 'config.yaml', Buffer.from(JSON.stringify(config)), 3);
      // Make starter code downloadable without displaying hidden test answers.
      const starterName = ['ttnn', 'ttlang'].includes(manifest.language) ? 'starter.py' : 'starter.cpp';
      const starterPath = path.join(folder, starterName);
      const starter = manifest.starter || (fs.existsSync(starterPath) ? fs.readFileSync(starterPath) : null);
      if (starter) {
        await problem.addAdditionalFile('system', pid, starterName, Buffer.from(starter), 3);
      }
      seeded++;
      console.log(`Seeded ${manifest.id} (internal pid ${pid})`);
    }
  }
  // Problems listed in TT_OJ_HIDDEN_PROBLEMS stay invisible to students (hidden, never unhidden here).
  const hiddenIds = (process.env.TT_OJ_HIDDEN_PROBLEMS || '').split(',').map((id) => id.trim()).filter(Boolean);
  for (const id of hiddenIds) {
    const doc = await problem.get('system', id);
    if (doc && !doc.hidden) {
      await problem.edit('system', doc.docId, { hidden: true });
      console.log(`Hidden ${id} (TT_OJ_HIDDEN_PROBLEMS)`);
    }
  }
  console.log(`Tenstorrent OJ bootstrap complete: ${seeded} problems, ${hiddenIds.length} hidden.`);
};
