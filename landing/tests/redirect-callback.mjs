// Run from landing/ after `npm ci`: node tests/redirect-callback.mjs  (checks where sign-in sends the user)
process.env.AUTH_GITHUB_ID="dummy"; process.env.AUTH_GITHUB_SECRET="dummy"; process.env.AUTH_SECRET="x".repeat(40);
const { default: handler } = await import("../api/auth/[...nextauth].js");
const base = "https://site.test";
async function signin(cb) {
  const csrf = await handler(new Request(base + "/api/auth/csrf"));
  const { csrfToken } = await csrf.json();
  const cookie = csrf.headers.getSetCookie().map(c => c.split(";")[0]).join("; ");
  const body = new URLSearchParams({ csrfToken, callbackUrl: cb });
  const r = await handler(new Request(base + "/api/auth/signin/github", { method: "POST", body, headers: { cookie, "content-type": "application/x-www-form-urlencoded" } }));
  const c = r.headers.getSetCookie().find(c => c.includes("callback-url"));
  return decodeURIComponent((c || "").split(";")[0].split("=").slice(1).join("="));
}
for (const cb of [base + "/app", "/app", "https://evil.test/x", base + "/"]) console.log(JSON.stringify(cb), "->", await signin(cb));
