import { API_URL } from "@/lib/config";

/** Full-page navigation to the API, which redirects to the provider (and back). */
export function oauthUrl(provider: "google" | "github", next: string): string {
  return `${API_URL}/auth/${provider}/login?next=${encodeURIComponent(next)}`;
}
