/** Wordmark: three overlapping dots (people) around one ring (the model). */
export function Logo({ className }: { className?: string }) {
  return (
    <span className={className}>
      <svg viewBox="0 0 24 24" className="inline size-5 align-[-4px]" aria-hidden>
        <circle cx="12" cy="12" r="5" fill="none" stroke="var(--primary)" strokeWidth="2" />
        <circle cx="5" cy="7" r="2.2" fill="currentColor" opacity=".55" />
        <circle cx="19" cy="7" r="2.2" fill="currentColor" opacity=".55" />
        <circle cx="12" cy="20.5" r="2.2" fill="currentColor" opacity=".55" />
      </svg>
      <span className="ml-1.5 font-semibold tracking-tight">Multigent</span>
    </span>
  );
}
