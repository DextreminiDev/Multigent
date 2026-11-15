import { API_URL } from "@/lib/config";

export type Readiness = { status: "ok" | "unavailable"; postgres: boolean; redis: boolean };

/** Fetch backend readiness. Never throws: network failure maps to `null`. */
export async function fetchReadiness(
  fetchImpl: typeof fetch = fetch,
  baseUrl: string = API_URL,
): Promise<Readiness | null> {
  try {
    const res = await fetchImpl(`${baseUrl}/readyz`, { cache: "no-store" });
    return (await res.json()) as Readiness;
  } catch {
    return null;
  }
}
