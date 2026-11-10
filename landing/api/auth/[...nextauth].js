import { Auth } from "@auth/core";
import GitHub from "@auth/core/providers/github";
import Google from "@auth/core/providers/google";

export const config = { runtime: "edge" };

function providers() {
  const list = [];
  if (process.env.AUTH_GITHUB_ID && process.env.AUTH_GITHUB_SECRET) {
    list.push(
      GitHub({
        clientId: process.env.AUTH_GITHUB_ID,
        clientSecret: process.env.AUTH_GITHUB_SECRET,
      })
    );
  }
  if (process.env.AUTH_GOOGLE_ID && process.env.AUTH_GOOGLE_SECRET) {
    list.push(
      Google({
        clientId: process.env.AUTH_GOOGLE_ID,
        clientSecret: process.env.AUTH_GOOGLE_SECRET,
      })
    );
  }
  return list;
}

export default async function handler(req) {
  return Auth(req, {
    providers: providers(),
    secret: process.env.AUTH_SECRET,
    trustHost: true,
    basePath: "/api/auth",
    session: { strategy: "jwt" },
    pages: {
      signIn: "/",
      error: "/",
    },
  });
}
