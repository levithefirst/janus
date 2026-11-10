// Run from landing/ with Playwright available (NODE_PATH): node tests/cta-and-signin.cjs. Auth is stubbed.
const http = require("http"), fs = require("fs"), { chromium } = require("playwright");
const html = fs.readFileSync(require("path").join(__dirname, "..", "public", "index.html"));
const srv = http.createServer((req, res) => {
  const signed = /signed=1/.test(req.headers.cookie || "");
  const u = new URL(req.url, "http://x");
  const json = o => { res.setHeader("content-type", "application/json"); res.end(JSON.stringify(o)); };
  if (u.pathname === "/") { res.setHeader("content-type", "text/html"); return res.end(html); }
  if (u.pathname === "/app") return res.end("APP PAGE");
  if (u.pathname === "/api/auth/session") return json(signed ? { user: { name: "Test User", email: "t@example.test" } } : {});
  if (u.pathname === "/api/auth/providers") return json({ github: {}, google: {} });
  if (u.pathname === "/api/auth/csrf") return json({ csrfToken: "tok" });
  if (req.method === "POST" && u.pathname.startsWith("/api/auth/signin/")) {   // stands in for the OAuth round trip
    let b = ""; req.on("data", d => b += d); req.on("end", () => {
      const cb = new URLSearchParams(b).get("callbackUrl");
      res.writeHead(303, { "set-cookie": "signed=1; Path=/", location: cb }); res.end(); });
    return;
  }
  res.statusCode = 404; res.end();
});
srv.listen(0, async () => {
  const base = "http://127.0.0.1:" + srv.address().port; const r = [];
  const ok = (n, c) => { r.push((c ? "PASS " : "FAIL ") + n); if (!c) process.exitCode = 1; };
  const br = await chromium.launch();
  // signed out
  let ctx = await br.newContext(), p = await ctx.newPage();
  await p.goto(base + "/"); await p.waitForTimeout(300);
  const links = await p.$$eval("a.btn-solid", a => a.map(x => [x.textContent.trim(), x.getAttribute("href")]));
  ok("two Get started links to /app: " + JSON.stringify(links), links.filter(l => l[0] === "Get started" && l[1] === "/app").length === 2);
  ok("no button still says Get started", (await p.$$eval("button", b => b.filter(x => /Get started/.test(x.textContent)).length)) === 0);
  await p.click("a.btn-solid:has-text('Get started') >> nth=0"); await p.waitForURL("**/app");
  ok("signed out: hero Get started goes to /app, no modal", (await p.textContent("body")).includes("APP PAGE"));
  await p.goto(base + "/"); await p.click("a.btn-solid:has-text('Get started') >> nth=1"); await p.waitForURL("**/app");
  ok("signed out: bottom CTA goes to /app", true);
  await p.goto(base + "/"); await p.click("#headerSignIn"); 
  ok("signed out: header Sign in opens the sign-in modal", await p.isVisible("#authModal .modal") && await p.isVisible("#authSignedOut"));
  await p.click("#btnGithub"); await p.waitForURL("**/app");
  ok("signed out: signing in lands on /app, not the landing page", new URL(p.url()).pathname === "/app");
  // signed in (the cookie from the sign-in above is still set)
  await p.goto(base + "/"); await p.waitForSelector("#sessionChip.on");
  await p.click("a.btn-solid:has-text('Get started') >> nth=0"); await p.waitForURL("**/app");
  ok("signed in: hero Get started goes to /app, no 'You're signed in' modal", new URL(p.url()).pathname === "/app");
  await p.goto(base + "/"); await p.waitForSelector("#sessionChip.on");
  await p.click("a.btn-solid:has-text('Get started') >> nth=1"); await p.waitForURL("**/app");
  ok("signed in: bottom CTA goes to /app", true);
  await p.goto(base + "/"); await p.waitForSelector("#sessionChip.on");
  ok("signed in: modal closed on load", !(await p.isVisible("#authModal .modal")));
  await p.click("#sessionOpen");
  ok("signed in: avatar/name opens the account modal", (await p.textContent("#authTitle")).includes("signed in") && await p.isVisible("#modalSignOut"));
  await br.close(); srv.close(); console.log(r.join("\n"));
});
