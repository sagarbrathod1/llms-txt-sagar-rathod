// The site page's data: load the site, poll its active job, start a new run, apply a monitoring
// change. The components only render what this returns.
import { useCallback, useEffect, useState } from "react";

import {
  createSite,
  errorText,
  getJob,
  getSite,
  httpStatus,
  isActive,
  POLL_INTERVAL_MS,
  type Site,
} from "@/lib/api";

import { isPermanentPollError } from "./site-state";

// A bad id in the URL comes back as 422 (not a UUID) or 404 (no such site); both mean the same
// thing to the user.
function siteErrorText(err: unknown): string {
  const status = httpStatus(err);
  return status === 404 || status === 422 ? "Site not found." : errorText(err);
}

export function useSite(siteId: string) {
  // The single source of truth: the job shown is always site.latest_job, which polling updates
  // in place.
  const [site, setSite] = useState<Site | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pollProblem, setPollProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const showSite = useCallback((next: Site) => {
    setSite(next);
    setLoadError(null);
    setPollProblem(null);
  }, []);

  // latest_job tells us the state on first load and after a reload, so the URL needs no job id.
  // The ignore flag drops a response that arrives after unmount or a siteId change.
  useEffect(() => {
    let ignore = false;
    getSite(siteId).then(
      (next) => {
        if (!ignore) showSite(next);
      },
      (err) => {
        if (!ignore) setLoadError(siteErrorText(err));
      },
    );
    return () => {
      ignore = true;
    };
  }, [siteId, showSite]);

  // Poll the active job. The effect depends on the job id only, so progress updates don't restart
  // it; it ends when the job finishes (the reloaded site then has a finished latest_job), on a
  // permanent error, or on unmount.
  const activeJobId = site && isActive(site.latest_job) ? site.latest_job.id : null;
  useEffect(() => {
    if (activeJobId === null) return;
    let stopped = false;

    async function poll(jobId: string) {
      while (!stopped) {
        // Wait before each request rather than using setInterval, so a slow response can never
        // overlap with the next request.
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        if (stopped) return;
        try {
          const next = await getJob(jobId);
          if (stopped) return;
          setPollProblem(null);
          if (isActive(next)) {
            setSite((current) => current && { ...current, latest_job: next });
            continue;
          }
          // Finished: reload the site for the new version, check times and final job. This is
          // inside the try so a failed reload is retried on the next tick rather than leaving the
          // progress panel stuck.
          const fresh = await getSite(siteId);
          if (stopped) return;
          showSite(fresh);
          // Defensive: if the site still reports this job as active (e.g. a read that raced the
          // job's final write), keep polling it. A different active job changes activeJobId,
          // which restarts this effect, so returning is right in that case.
          if (isActive(fresh.latest_job) && fresh.latest_job.id === jobId) continue;
          return;
        } catch (err) {
          if (stopped) return;
          if (isPermanentPollError(httpStatus(err))) {
            setPollProblem(`Stopped checking progress (${errorText(err)}). Reload to try again.`);
            return;
          }
          setPollProblem("Lost contact with the server, retrying…");
        }
      }
    }

    void poll(activeJobId);
    return () => {
      stopped = true;
    };
  }, [activeJobId, siteId, showSite]);

  // Used by both "Try again" and "Re-generate now". POST /sites returns the site's active job if
  // one exists, so a double click can't start two crawls.
  async function regenerate() {
    if (site === null) return;
    setStarting(true);
    setStartError(null);
    try {
      await createSite(site.url);
    } catch (err) {
      setStartError(errorText(err));
      setStarting(false);
      return;
    }
    // The run has started; reloading the site shows it (and starts polling). A failed reload
    // is a refresh problem, not a failed start.
    try {
      showSite(await getSite(siteId));
    } catch (err) {
      setLoadError(siteErrorText(err));
    } finally {
      setStarting(false);
    }
  }

  // Merge only the fields PATCH owns. Replacing the whole site would let a slow PATCH response
  // overwrite a newer latest_job (from a re-generate or poll) and stop polling.
  const applyMonitoring = useCallback((next: Site) => {
    setSite(
      (current) =>
        current && {
          ...current,
          monitor_enabled: next.monitor_enabled,
          next_check_at: next.next_check_at,
          consecutive_failures: next.consecutive_failures,
        },
    );
  }, []);

  return { site, loadError, pollProblem, starting, startError, regenerate, applyMonitoring };
}
