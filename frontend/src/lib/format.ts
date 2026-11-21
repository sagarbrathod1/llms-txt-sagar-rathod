// Timestamps come from the API in UTC. Callers only render them after the client-side fetch, so
// server and client output can't mismatch.

// The exact time in the viewer's own locale and time zone: history rows and hover titles.
export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// English to match the rest of the copy; numeric: "auto" gives "yesterday" rather than "1 day ago".
const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

// "just now", "5 minutes ago", "yesterday", "3 weeks ago". Status lines read better relative;
// the exact time goes in a hover title.
export function timeAgo(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  for (const [unit, size] of UNITS) {
    if (seconds >= size) return relative.format(-Math.floor(seconds / size), unit);
  }
  return "just now";
}

// "Checked daily", "Checked hourly", "Checked every 6 hours".
export function checkSchedule(hours: number): string {
  if (hours === 24) return "Checked daily";
  if (hours === 1) return "Checked hourly";
  return `Checked every ${hours} hours`;
}
