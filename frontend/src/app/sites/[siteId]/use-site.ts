// The site page's data: load the site, poll its active job, start a new run, resume monitoring.
// The components only render what this returns.
import { useCallback, useEffect, useState } from "react";

import {
  createSite,
  errorText,
  getJob,
  getSite,
  httpStatus,
  isActive,
  POLL_INTERVAL_MS,
  setMonitoring,
  type Site,
} from "@/lib/api";

import { isPermanentPollError, type WatchedRun } from "./site-state";

export function useSite(siteId: string) {
  // The single source of truth: the job shown is always site.latest_job, which polling updates
  // in place.
  const [site, setSite] = useState<Site | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pollProblem, setPollProblem] = useState<string | null>(null);
  const [watched, setWatched] = useState<WatchedRun | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);

  const showSite = useCallback((next: Site) => {
    setSite(next);
    setLoadError(null);
    setPollProblem(null);
  }, []);

  // A bad id in the URL comes back as 422 (not a UUID) or 404 (no such site); both mean the same
  // thing to the user.
  const showLoadError = useCallback((err: unknown) => {
    const status = httpStatus(err);
    if (status === 404 || status === 422) setNotFound(true);
    else setLoadError(errorText(err));
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
        if (!ignore) showLoadError(err);
      },
    );
    return () => {
      ignore = true;
    };
  }, [siteId, showSite, showLoadError]);

  // A tab left open would otherwise show "Last checked yesterday" forever, ageing as if live.
  // Re-read the site whenever the tab becomes visible again, unless a job is being polled (that
  // already keeps the page current). A failed refresh is ignored: the data on screen is still
  // valid, and the next visit tries again.
  const jobIsActive = site !== null && isActive(site.latest_job);
  useEffect(() => {
    if (jobIsActive) return;
    let ignore = false;
    function refresh() {
      if (document.visibilityState !== "visible") return;
      getSite(siteId).then(
        (next) => {
          if (!ignore) showSite(next);
        },
        () => {},
      );
    }
    document.addEventListener("visibilitychange", refresh);
    return () => {
      ignore = true;
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [jobIsActive, siteId, showSite]);

  // Poll the active job. The effect depends on the job id only, so progress updates don't restart
  // it; it ends when the job finishes (the reloaded site then has a finished latest_job), on a
  // permanent error, or on unmount. The version a run started from changes only when the run
  // finishes, at which point the job id changes too, so it doesn't restart the effect mid-run.
  const activeJobId = site && isActive(site.latest_job) ? site.latest_job.id : null;
  const startVersionId = site?.latest_version?.id ?? null;
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
          // progress view stuck.
          const fresh = await getSite(siteId);
          if (stopped) return;
          // Remembered so the status line can say whether this run changed the file.
          setWatched({ jobId, previousVersionId: startVersionId });
          showSite(fresh);
          // Defensive: if the site still reports this job as active (e.g. a read that raced the
          // job's final write), keep polling it. A different active job changes activeJobId,
          // which restarts this effect, so returning is right in that case.
          if (isActive(fresh.latest_job) && fresh.latest_job.id === jobId) continue;
          return;
        } catch (err) {
          if (stopped) return;
          if (isPermanentPollError(httpStatus(err))) {
            setPollProblem("Lost track of progress. Reload the page.");
            return;
          }
          setPollProblem("Reconnecting…");
        }
      }
    }

    void poll(activeJobId);
    return () => {
      stopped = true;
    };
  }, [activeJobId, startVersionId, siteId, showSite]);

  // "Check now" and "Try again". POST /sites returns the site's active job if one exists, so a
  // double click can't start two crawls.
  async function checkNow() {
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
      showLoadError(err);
    } finally {
      setStarting(false);
    }
  }

  // Turns monitoring back on. Only ever on: see SiteStatus.canResume.
  async function resumeMonitoring() {
    if (site === null) return;
    setResuming(true);
    setResumeError(null);
    try {
      const next = await setMonitoring(site.id, true);
      // Merge only the fields PATCH owns. Replacing the whole site would let a slow response
      // overwrite a newer latest_job (from Check now or polling) and stop polling.
      setSite(
        (current) =>
          current && {
            ...current,
            monitor_enabled: next.monitor_enabled,
            next_check_at: next.next_check_at,
            consecutive_failures: next.consecutive_failures,
          },
      );
    } catch (err) {
      // 409: the monitored-sites cap is full. The API's own message is written for the old
      // toggle, so this one points at what still works here.
      setResumeError(
        httpStatus(err) === 409
          ? "Daily checks are full right now. Use Check now to refresh it."
          : errorText(err),
      );
    } finally {
      setResuming(false);
    }
  }

  return {
    site,
    notFound,
    loadError,
    pollProblem,
    watched,
    starting,
    startError,
    checkNow,
    resuming,
    resumeError,
    resumeMonitoring,
  };
}
