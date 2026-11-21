// Pure decisions behind the site page, kept out of the components so they can be unit tested.
import { isActive, type ActiveJob, type Site, type Version } from "@/lib/api";
import { checkSchedule } from "@/lib/format";

const UNKNOWN_FAILURE = "Something went wrong while generating.";

// What the page is about. Once a site has any llms.txt, that file is always the page: later runs
// (a resubmit, Check now, the daily check) only change the status line, never hide the file.
export type SiteView =
  | { kind: "generating"; job: ActiveJob }
  | { kind: "failed"; error: string }
  | { kind: "ready"; version: Version };

export function siteView(site: Site): SiteView {
  if (site.latest_version) return { kind: "ready", version: site.latest_version };
  const job = site.latest_job;
  if (isActive(job)) return { kind: "generating", job };
  // No file and nothing running means the first run failed. (A site is created together with its
  // first job, so "no job at all" shouldn't happen; offering to try again is right for it too.)
  return { kind: "failed", error: job?.error ?? UNKNOWN_FAILURE };
}

// The line under the progress bar, from the job's real progress.
export function progressLine(job: ActiveJob): string {
  if (job.status === "queued") return "Getting started…";
  if (job.pages_discovered === 0) return "Finding pages…";
  // Every planned page is read, but the job is still running: it's ranking and writing the file.
  if (job.pages_crawled >= job.pages_discovered) return "Writing llms.txt…";
  // No "of N": while following links, N is the crawl's page budget, not the site's size.
  return `${job.pages_crawled} pages read`;
}

// 0 to 1. The engine guarantees crawled/discovered never goes backwards, so this can feed a bar
// directly.
export function progressFraction(job: ActiveJob): number {
  if (job.status === "queued" || job.pages_discovered === 0) return 0;
  return Math.min(1, job.pages_crawled / job.pages_discovered);
}

// A run this page saw while it was active (from its start, or from a load mid-run) and then saw
// finish, so its outcome can be reported ("Updated", "No changes"). previousVersionId is the
// latest version while the run was active: versions only change when a run finishes.
export type WatchedRun = { jobId: string; previousVersionId: string | null };

// One segment of the status line. `at` is rendered as a relative time after `text`.
export type StatusPart = { text: string; at?: string };

export type SiteStatus = {
  parts: StatusPart[];
  checking: boolean;
  // Why the latest run failed; the file shown is then the last one that worked.
  failure: string | null;
  // Monitoring is off: offer to turn it back on. Never offered the other way round, because with
  // no accounts, "off" would let anyone stop checks for everyone.
  canResume: boolean;
};

// The status line for a site that has a file: how it's monitored, and what the latest run did.
export function siteStatus(site: Site, watched: WatchedRun | null): SiteStatus {
  const job = site.latest_job;
  const version = site.latest_version;
  const schedule = site.monitor_enabled
    ? checkSchedule(site.check_interval_hours)
    : "Not checked automatically";
  const parts: StatusPart[] = [{ text: schedule }];
  // With one version the file has never changed, so "Generated" is the honest label.
  const changed: StatusPart | null = version
    ? { text: site.versions.length > 1 ? "Last changed" : "Generated", at: version.created_at }
    : null;
  // The run that saved the latest version also set last_checked_at, to the same timestamp (one
  // transaction). Then "Last checked" would repeat the time "Last changed"/"Generated" shows.
  const lastChecked = site.last_checked_at;
  const checkedSeparately = lastChecked !== null && lastChecked !== version?.created_at;
  let failure: string | null = null;

  // The outcome is known only for the latest job, and only if this page saw it active. A first
  // generation (no previous version) has no "updated" to report: the file just appears.
  const outcomeKnown =
    watched !== null && job?.id === watched.jobId && watched.previousVersionId !== null;

  if (isActive(job)) {
    parts.push({ text: "Checking for updates…" });
  } else if (job?.status === "failed") {
    failure = job.error ?? UNKNOWN_FAILURE;
    // The count also grows when a manual run fails, so this states the fact without claiming it
    // is why monitoring is off. A site refused by the monitoring cap has no failures, so it
    // reads differently: just "Not checked automatically".
    const failures = site.consecutive_failures;
    parts.push(
      failures > 1
        ? { text: `Last ${failures} checks failed` }
        : { text: "Last check failed", at: job.finished_at ?? undefined },
    );
    if (changed) parts.push(changed);
  } else if (outcomeKnown && version?.id !== watched.previousVersionId) {
    // "Updated" already says when it last changed.
    parts.push({ text: "Updated", at: version?.created_at });
  } else if (outcomeKnown) {
    parts.push({ text: "No changes · checked", at: site.last_checked_at ?? undefined });
    if (changed) parts.push(changed);
  } else {
    if (checkedSeparately) parts.push({ text: "Last checked", at: lastChecked });
    if (changed) parts.push(changed);
  }

  return { parts, checking: isActive(job), failure, canResume: !site.monitor_enabled };
}

// Whether a failed poll should stop polling. `status` is the HTTP status, or null when no
// response arrived. A 4xx won't fix itself by retrying (e.g. the job no longer exists), except
// 408 (timeout) and 429 (rate limited), where retrying later is the fix. Network errors and
// 5xx are usually transient.
export function isPermanentPollError(status: number | null): boolean {
  return status !== null && status < 500 && status !== 408 && status !== 429;
}
