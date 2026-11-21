"use client";

import { Fragment, useEffect, useState } from "react";

import type { Site } from "@/lib/api";
import { formatDate, timeAgo } from "@/lib/format";

import { siteStatus, type WatchedRun } from "./site-state";

type Props = {
  site: Site;
  watched: WatchedRun | null;
  pollProblem: string | null;
  starting: boolean;
  startError: string | null;
  checkNow: () => void;
  resuming: boolean;
  resumeError: string | null;
  resumeMonitoring: () => void;
};

// Monitoring as status rather than a control: "Checked daily · Last checked 3 hours ago · Last
// changed 2 days ago", with Check now on the side.
export function SiteStatusLine(props: Props) {
  const { site, watched, pollProblem, starting, startError, checkNow } = props;
  const { resuming, resumeError, resumeMonitoring } = props;
  const status = siteStatus(site, watched);
  const now = useNow();

  return (
    <div className="space-y-1 text-sm text-muted">
      <div className="flex items-baseline justify-between gap-4">
        {/* A live region so the switch to "Updated" / "No changes" is announced. */}
        <p role="status" className="flex min-w-0 flex-wrap items-baseline gap-x-1.5">
          {status.parts.map((part, i) => (
            <Fragment key={part.text}>
              {i > 0 && <span aria-hidden>·</span>}
              <span>
                {part.text}
                {part.at && (
                  <>
                    {" "}
                    <time dateTime={part.at} title={formatDate(part.at)}>
                      {timeAgo(part.at, now)}
                    </time>
                  </>
                )}
              </span>
              {/* Right after "Not checked automatically", the part it changes. Not "Check daily":
                  next to "Checked daily" and "Check now", that read as a status. */}
              {i === 0 && status.canResume && (
                <>
                  <span aria-hidden>·</span>
                  <button
                    type="button"
                    className="btn-text text-foreground underline"
                    disabled={resuming}
                    onClick={resumeMonitoring}
                  >
                    Turn on daily checks
                  </button>
                </>
              )}
            </Fragment>
          ))}
        </p>
        {!status.checking && (
          <button
            type="button"
            className="btn-text shrink-0"
            disabled={starting}
            onClick={checkNow}
          >
            {starting ? "Starting…" : "Check now"}
          </button>
        )}
      </div>

      {status.failure && <p>{status.failure} Showing the last version that worked.</p>}
      {status.checking && pollProblem && <p>{pollProblem}</p>}
      {startError && (
        <p role="alert" className="text-danger">
          {startError}
        </p>
      )}
      {resumeError && <p role="alert">{resumeError}</p>}
    </div>
  );
}

// The current time, refreshed every minute, so "just now" doesn't stay on screen for an hour.
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}
