// Shows a timestamp in the viewer's own locale and time zone (the API sends UTC). Callers only
// render dates after the client-side fetch, so server and client output can't mismatch.
export function formatDate(iso: string | null, empty = "Never"): string {
  return iso === null ? empty : new Date(iso).toLocaleString();
}

// "every hour", "every 24 hours".
export function formatInterval(hours: number): string {
  return hours === 1 ? "every hour" : `every ${hours} hours`;
}
