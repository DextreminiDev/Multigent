import createClient from "openapi-fetch";
import { API_URL } from "@/lib/config";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Chat = Schemas["ChatView"];
export type Me = Schemas["MeView"];
export type User = Schemas["UserView"];
export type KeyView = Schemas["KeyView"];
export type Provider = Schemas["ProviderView"];
export type ModelOption = Schemas["ModelOption"];
export type ChatMessage = Schemas["MessageView"];
export type Role = Chat["role"];

/** CSRF token from GET /me (double-submit). Held in memory only. */
let csrfToken: string | null = null;
export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export const api = createClient<paths>({
  baseUrl: API_URL,
  credentials: "include",
  // Resolve fetch at call time (lets tests and polyfills swap it).
  fetch: (req) => globalThis.fetch(req),
});

api.use({
  onRequest({ request }) {
    if (request.method !== "GET" && csrfToken) request.headers.set("X-CSRF-Token", csrfToken);
    return request;
  },
});

type ErrorBody = { error?: { code?: string; message?: string } };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Turn an openapi-fetch result into data-or-throw with a typed ApiError. */
export async function unwrap<T>(
  p: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let res: { data?: T; error?: unknown; response: Response };
  try {
    res = await p;
  } catch {
    throw new ApiError(0, "network", "Can't reach the server. Check your connection.");
  }
  if (res.response.ok) return res.data as T;
  const body = (res.error ?? {}) as ErrorBody;
  throw new ApiError(
    res.response.status,
    body.error?.code ?? "http_error",
    body.error?.message ?? `Request failed (${res.response.status})`,
  );
}
