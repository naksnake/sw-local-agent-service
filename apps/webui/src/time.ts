// Human time (CLAUDE.md §9): "3 minutes ago", "yesterday", "Never" — never a raw timestamp.

export function humanTime(iso: string | null, now: Date = new Date()): string {
  if (iso === null) {
    return "Never";
  }
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) {
    return "Never";
  }
  const seconds = Math.max(0, Math.round((now.getTime() - then.getTime()) / 1000));
  if (seconds < 60) {
    return "just now";
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return minutes === 1 ? "1 minute ago" : `${minutes} minutes ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
  }
  const days = Math.round(hours / 24);
  if (days === 1) {
    return "yesterday";
  }
  if (days < 7) {
    return `${days} days ago`;
  }
  return then.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** "14:02" — the wall clock as the Settings page says it. */
export function clockShort(now: Date = new Date()): string {
  return now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
}
