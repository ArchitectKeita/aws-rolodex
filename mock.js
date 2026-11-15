/* Preview only: a pretend backend so the page runs without AWS. Two "containers" in two AZs, heartbeats,
   round-robin answers, a kill that ECS "replaces" after a few seconds. The owner key here is: preview */
(function () {
  const SEED = /*__SEED__*/[];
  const DAY = 864e5, today = new Date().toISOString().slice(0, 10);
  const iso = d => new Date(d).toISOString().slice(0, 19) + "Z";
  const hex = n => Array.from({ length: n }, () => "0123456789abcdef"[Math.random() * 16 | 0]).join("");
  const people = new Map(), events = new Map();
  for (const s of SEED) {
    const last = new Date(Date.now() - s.daysAgo * DAY).toISOString().slice(0, 10);
    people.set(s.id, { id: s.id, name: s.name, title: s.title, company: s.company, city: s.city, tags: s.tags, cadence: s.cadence,
                       notes: s.notes, last_contact: last, added: last, demo: true });
    events.set(s.id, s.events.map(([kind, text, ago]) => ({ kind, text, at: iso(Date.now() - ago * DAY) })));
  }
  const byName = n => [...people.values()].find(p => p.name === n).id;
  const k = byName("Kia Johnson"), m = byName("Marcus Reed"), t12 = iso(Date.now() - 12 * DAY);
  events.get(k).push({ kind: "intro", text: "Introduced to Marcus Reed · looking for a tech role", at: t12, with: m });
  events.get(m).push({ kind: "intro", text: "Introduced to Kia Johnson · looking for a tech role", at: t12, with: k });

  const task = az => ({ task: hex(32), az, ip: `10.80.${az.endsWith("a") ? 10 : 11}.${20 + (Math.random() * 200 | 0)}`, state: "alive", started: iso(Date.now() - 3600e3 * (1 + Math.random() * 5)), served: 40 + (Math.random() * 60 | 0) });
  const fleet = [task("us-east-1a"), task("us-east-1b")];
  let rr = 0, owner = false;
  const pick = () => { const alive = fleet.filter(t => t.state === "alive"); const t = alive[rr++ % alive.length]; t.served++; return { task: t.task, az: t.az, ip: t.ip }; };
  const intros = id => (events.get(id) || []).filter(e => e.kind === "intro").length;
  const res = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

  window.MOCK_FETCH = async (path, init) => {
    await new Promise(r => setTimeout(r, 40 + Math.random() * 80));
    const method = init.method || "GET", body = init.body ? JSON.parse(init.body) : {}, me = pick();
    let mm;
    if (path === "/api/people" && method === "GET") {
      const ppl = [...people.values()].map(p => ({ ...p, intro_count: intros(p.id) })).sort((a, b) => b.last_contact.localeCompare(a.last_contact));
      return res(200, { people: ppl, intros: ppl.reduce((a, p) => a + p.intro_count, 0) / 2 | 0, owner, today, served_by: me });
    }
    if ((mm = path.match(/^\/api\/people\/([a-f0-9]{12})$/)) && method === "GET") {
      const p = people.get(mm[1]); if (!p) return res(404, { error: "not found", served_by: me });
      return res(200, { person: p, timeline: [...events.get(p.id)].sort((a, b) => b.at.localeCompare(a.at)), owner, served_by: me });
    }
    if (path === "/api/fleet") {
      const now = Date.now();
      return res(200, { tasks: fleet.map(t => ({ ...t, age: 1 })).sort((a, b) => (a.state !== "alive") - (b.state !== "alive") || a.az.localeCompare(b.az)), served_by: me, owner, at: iso(now) });
    }
    if (path === "/api/unlock") { if (body.key !== "preview") return res(403, { error: "wrong key — in this preview it's: preview", served_by: me }); owner = true; return res(200, { ok: true, served_by: me }); }
    if (path === "/api/lock") { owner = false; return res(200, { ok: true, served_by: me }); }
    if (!owner) return res(401, { error: "locked: unlock with the owner key first", served_by: me });
    if (path === "/api/chaos") {
      const t = fleet.find(x => x.task === me.task); t.state = "killed";
      setTimeout(() => { const i = fleet.indexOf(t); if (i >= 0) fleet.splice(i, 1); }, 30000);
      setTimeout(() => { const n = task(t.az); n.started = iso(Date.now()); n.served = 0; fleet.push(n); }, 9000);
      return res(200, { killed: me });
    }
    if (path === "/api/people" && method === "POST") {
      if (!body.name || !body.name.trim()) return res(400, { error: "a name is required", served_by: me });
      const id = hex(12);
      people.set(id, { id, name: body.name.trim(), title: body.title || "", company: body.company || "", city: body.city || "", tags: [...new Set(body.tags.map(t => t.toLowerCase()))].sort(),
                       cadence: body.cadence || 30, notes: body.notes || "", last_contact: today, added: today });
      events.set(id, [{ kind: "met", text: body.met || "Added to the Rolodex", at: iso(Date.now()) }]);
      return res(201, { id, served_by: me });
    }
    if ((mm = path.match(/^\/api\/people\/([a-f0-9]{12})\/log$/))) {
      const p = people.get(mm[1]); if (body.kind !== "note") p.last_contact = today;
      events.get(p.id).push({ kind: body.kind, text: { text: "Texted", email: "Emailed", logged: "Checked in" }[body.kind] || "Checked in", at: iso(Date.now()) });
      return res(200, { ok: true, served_by: me });
    }
    if (path === "/api/intro") {
      if (body.a === body.b) return res(400, { error: "pick two different people", served_by: me });
      const A = people.get(body.a), B = people.get(body.b), why = body.why ? ` · ${body.why}` : "", at = iso(Date.now());
      events.get(A.id).push({ kind: "intro", text: `Introduced to ${B.name}${why}`, at, with: B.id });
      events.get(B.id).push({ kind: "intro", text: `Introduced to ${A.name}${why}`, at, with: A.id });
      A.last_contact = B.last_contact = today;
      return res(200, { ok: true, served_by: me });
    }
    if ((mm = path.match(/^\/api\/people\/([a-f0-9]{12})$/)) && method === "DELETE") { people.delete(mm[1]); return res(200, { ok: true, served_by: me }); }
    return res(404, { error: "not found", served_by: me });
  };
})();
