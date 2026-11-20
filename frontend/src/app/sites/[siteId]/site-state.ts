// Pure decisions behind the site page, kept out of the components so they can be unit tested.
import { isActive, type Site } from "@/lib/api";

export type SiteViewState = {
  // The latest scheduled re-check failed, but an older file is still there to show.
  recheckError: string | null;
  // The latest run failed with nothing new to show: the error panel with "Try again".
  failure: string | null;
  // "Re-generate now" is offered: no job is running and no "Try again" is on screen.
  canRegenerate: boolean;
};

export function siteViewState(site: Site): SiteViewState {
  const job = site.latest_job;
  const failed = job?.status === "failed";
  // A failed scheduled re-check leaves the last good file in place, so it is a note under
  // monitoring; any other failure means there is nothing new to show.
  const recheckFailed = failed && job.kind === "recheck" && site.latest_version !== null;
  const error = job?.error ?? "The run failed for an unknown reason.";
  return {
    recheckError: recheckFailed ? error : null,
    failure: failed && !recheckFailed ? error : null,
    canRegenerate: !isActive(job) && !(failed && !recheckFailed),
  };
}

// Whether a failed poll should stop polling. `status` is the HTTP status, or null when no
// response arrived. A 4xx won't fix itself by retrying (e.g. the job no longer exists), except
// 408 (timeout) and 429 (rate limited), where retrying later is the fix. Network errors and
// 5xx are usually transient.
export function isPermanentPollError(status: number | null): boolean {
  return status !== null && status < 500 && status !== 408 && status !== 429;
}

// The API turns monitoring off after a fixed number of failures in a row, but the count also
// grows when a manual run fails after the user turned monitoring off. So the "off" wording states
// both facts without claiming which one caused the other, and needs no knowledge of the limit.
export function failedChecksNote(failures: number, monitorEnabled: boolean): string | null {
  if (failures === 0) return null;
  const failed = failures === 1 ? "check failed" : `${failures} checks failed in a row`;
  return monitorEnabled
    ? `The last ${failed}.`
    : `Monitoring is off and the last ${failed}. Turn it back on to resume scheduled checks.`;
}
