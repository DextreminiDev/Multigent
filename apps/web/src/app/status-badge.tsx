"use client";

import { useEffect, useState } from "react";
import { fetchReadiness, type Readiness } from "@/lib/api/health";

type State = { kind: "loading" } | { kind: "done"; value: Readiness | null };

export function StatusBadge() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let alive = true;
    fetchReadiness().then((value) => alive && setState({ kind: "done", value }));
    return () => {
      alive = false;
    };
  }, []);

  if (state.kind === "loading") {
    return <span className="text-muted">Checking backend…</span>;
  }
  const ok = state.value?.status === "ok";
  return (
    <span role="status" className={ok ? "text-ok" : "text-bad"}>
      {ok ? "Backend ready" : "Backend unavailable"}
    </span>
  );
}
