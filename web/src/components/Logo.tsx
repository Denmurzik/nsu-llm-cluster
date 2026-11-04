/** Flat mark: a 2x2 grid of machines, one of them lit. */
export function Mark({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true">
      <rect x="1" y="1" width="6" height="6" rx="1.5" fill="var(--accent)" />
      <rect x="9" y="1" width="6" height="6" rx="1.5" fill="var(--text-3)" opacity="0.6" />
      <rect x="1" y="9" width="6" height="6" rx="1.5" fill="var(--text-3)" opacity="0.6" />
      <rect x="9" y="9" width="6" height="6" rx="1.5" fill="var(--text-3)" opacity="0.6" />
    </svg>
  );
}

export function Logo() {
  return (
    <>
      <Mark />
      <span>NSU LLM</span>
    </>
  );
}
