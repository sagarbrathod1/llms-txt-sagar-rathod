"use client";

import Link from "next/link";

import { type ActiveJob } from "@/lib/api";

import { LlmsTxtPanel } from "./llms-txt-panel";
import { SiteStatusLine } from "./site-status";
import { progressFraction, progressLine, siteView } from "./site-state";
import { useSite } from "./use-site";

export function SiteView({ siteId }: { siteId: string }) {
  const {
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
  } = useSite(siteId);

  if (notFound) {
    return (
      <div className="space-y-3">
        <h1 className="text-xl font-medium">We couldn&apos;t find that site.</h1>
        <Link href="/" className="link-muted">
          Generate a new llms.txt →
        </Link>
      </div>
    );
  }

  if (site === null) {
    return loadError ? (
      <p role="alert" className="text-sm text-danger">
        {loadError}
      </p>
    ) : (
      <p role="status" className="text-sm text-muted">
        Loading…
      </p>
    );
  }

  const view = siteView(site);
  // The site's address without the scheme: shorter, and how people say it.
  const host = site.url.replace(/^https?:\/\//, "");

  return (
    <div className="space-y-8">
      {/* React 19 hoists this into <head>, so each site's tab and history entry are named. */}
      <title>{`${host} · llms.txt`}</title>

      <div className="space-y-1.5">
        <h1 className="text-2xl font-medium tracking-tight break-all">
          <a
            href={site.url}
            target="_blank"
            rel="noreferrer"
            className="underline-offset-4 hover:underline"
          >
            {host}
          </a>
        </h1>
        {view.kind === "ready" && (
          <SiteStatusLine
            site={site}
            watched={watched}
            pollProblem={pollProblem}
            starting={starting}
            startError={startError}
            checkNow={checkNow}
            resuming={resuming}
            resumeError={resumeError}
            resumeMonitoring={resumeMonitoring}
          />
        )}
      </div>

      {/* A refresh that failed after the page loaded; the data on screen is still valid. */}
      {loadError && <p className="text-sm text-muted">{loadError}</p>}

      {view.kind === "generating" && <Generating job={view.job} problem={pollProblem} />}

      {view.kind === "failed" && (
        <section className="space-y-4">
          <div className="space-y-1">
            <h2 className="font-medium">Couldn&apos;t generate an llms.txt</h2>
            <p className="text-sm text-muted">{view.error}</p>
          </div>
          <div className="flex items-center gap-4">
            <button
              type="button"
              className="btn btn-primary"
              disabled={starting}
              onClick={checkNow}
            >
              {starting ? "Starting…" : "Try again"}
            </button>
            <Link href="/" className="link-muted">
              Try another site
            </Link>
          </div>
          {startError && (
            <p role="alert" className="text-sm text-danger">
              {startError}
            </p>
          )}
        </section>
      )}

      {view.kind === "ready" && (
        // key: a new latest version resets the panel back to showing the latest.
        <LlmsTxtPanel key={view.version.id} site={site} latest={view.version} />
      )}
    </div>
  );
}

function Generating({ job, problem }: { job: ActiveJob; problem: string | null }) {
  const fraction = progressFraction(job);
  return (
    <section className="space-y-3">
      <h2 className="font-medium">Generating llms.txt</h2>
      <div
        role="progressbar"
        aria-label="Progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(fraction * 100)}
        // Pulses until the first page count arrives, so a bar at 0 doesn't look stuck.
        className={`h-1 overflow-hidden rounded-full bg-border ${fraction === 0 ? "animate-pulse" : ""}`}
      >
        <div
          className="h-full rounded-full bg-foreground transition-[width] duration-500 ease-out"
          style={{ width: `${fraction * 100}%` }}
        />
      </div>
      {/* Not a live region: announcing the count every 1.5 s would be noise. The bar's value
          carries progress for assistive tech. */}
      <p className="text-sm text-muted tabular-nums">{problem ?? progressLine(job)}</p>
    </section>
  );
}
