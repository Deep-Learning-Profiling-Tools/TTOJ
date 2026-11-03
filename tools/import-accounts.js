// Create OJ accounts from a CSV roster (email,username,password[,displayName]).
// Run inside the site container through the Hydro CLI; the roster never passes through argv:
//   docker cp tools/import-accounts.js <site-container>:/tmp/import-accounts.js
//   docker cp roster.csv <site-container>:/tmp/roster.csv
//   docker exec <site-container> sh -c 'cd /opt/hydro && hydrooj cli execute "await require(\"/tmp/import-accounts.js\")(\"/tmp/roster.csv\")"'
//   docker exec <site-container> rm /tmp/roster.csv /tmp/import-accounts.js
// Existing usernames are left untouched (their passwords are not reset). Prints username -> uid only.
const fs = require('fs');
module.exports = async function importAccounts(file, domainId = 'system') {
  const { user, domain } = global.Hydro.model;
  const lines = fs.readFileSync(file, 'utf8').split(/\r?\n/).filter((l) => l.trim() && !l.startsWith('#'));
  if (/^email\s*,\s*username/i.test(lines[0])) lines.shift();
  const summary = { created: [], existing: [], failed: [] };
  for (const line of lines) {
    const [email, uname, password, displayName] = line.split(',').map((s) => s.trim());
    if (!email || !uname || !password) { summary.failed.push({ uname, reason: 'missing field' }); continue; }
    try {
      const existing = await user.getByUname(domainId, uname);
      if (existing) { summary.existing.push(`${uname} -> ${existing._id}`); continue; }
      const uid = await user.create(email, uname, password);
      if (displayName && domain.setUserInDomain) await domain.setUserInDomain(domainId, uid, { displayName });
      summary.created.push(`${uname} -> ${uid}`);
    } catch (error) {
      summary.failed.push({ uname, reason: String(error && error.message || error) });
    }
  }
  console.log(JSON.stringify(summary, null, 2));
  return summary;
};
