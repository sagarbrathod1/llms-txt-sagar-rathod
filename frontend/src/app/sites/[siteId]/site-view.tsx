"use client";

import { CopyButton } from "@/components/copy-button";
import { hostedUrl, isActive, type ActiveJob } from "@/lib/api";
import { formatDate } from "@/lib/format";

import { LlmsTxtPanel } from "./llms-txt-panel";
import { Monitoring } from "./monitoring";
import { siteViewState } from "./site-state";
import { useSite } from "./use-site";

export function SiteView({ siteId }: { siteId: string }) {
  const { site, loadError, pollProblem, starting, startError, regenerate, applyMonitoring } =
    useSite(siteId);

  if (site === null) {
    return loadError ? (
      <p role="alert" className="text-danger">
        Could not load this site: {loadError}
      </p>
    ) : (
      <p role="status" className="text-muted">
        Loading…
      </p>
    );
  }

  const job = site.latest_job;
  const { recheckError, failure, canRegenerate } = siteViewState(site);

  return (
    <div className="space-y-8">
      {/* React 19 hoists this into <head>, so each site's tab and history entry are named. */}
      <title>{`${site.url} · llms.txt generator`}</title>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h1 className="text-2xl font-semibold break-all">{site.url}</h1>
          <p className="text-sm text-muted">Added {formatDate(site.created_at)}</p>
        </div>
        {canRegenerate && (
          <button type="button" className="btn" disabled={starting} onClick={regenerate}>
            {starting ? "Starting…" : "Re-generate now"}
          </button>
        )}
      </div>

      {loadError && (
        <p role="alert" className="text-danger">
          Could not refresh this site: {loadError}
        </p>
      )}
      {startError && (
        <p role="alert" className="text-danger">
          Could not start a new run: {startError}
        </p>
      )}

      {isActive(job) && <JobProgress job={job} problem={pollProblem} />}

      {failure !== null && (
        <section className="space-y-3 rounded-md border border-danger p-4">
          <h2 className="font-semibold text-danger">Generation failed</h2>
          <p>{failure}</p>
          <button
            type="button"
            className="btn btn-primary"
            disabled={starting}
            onClick={regenerate}
          >
            {starting ? "Starting…" : "Try again"}
          </button>
        </section>
      )}

      {site.latest_version ? (
        <>
          {/* key: a new latest version resets the panel back to showing the latest. */}
          <LlmsTxtPanel key={site.latest_version.id} site={site} latest={site.latest_version} />
          {job?.status === "succeeded" && job.warnings.length > 0 && (
            <Warnings warnings={job.warnings} />
          )}
          <HostedUrl siteId={site.id} />
          <Monitoring site={site} onChange={applyMonitoring} recheckError={recheckError} />
        </>
      ) : (
        !isActive(job) &&
        failure === null && <p className="text-muted">No llms.txt has been generated yet.</p>
      )}
    </div>
  );
}

function JobProgress({ job, problem }: { job: ActiveJob; problem: string | null }) {
  return (
    <section className="space-y-1 rounded-md border border-border bg-surface p-4">
      <h2 className="font-semibold">
        {job.kind === "recheck" ? "Checking the site for changes…" : "Generating llms.txt…"}
      </h2>
      {/* Only the phase line is a live region: announcing the counter every 1.5s would be noise. */}
      <p role="status">
        {job.status === "queued"
          ? "Queued, waiting for a worker to pick it up."
          : "Running: finding and fetching pages."}
      </p>
      {job.status === "running" && (
        <p className="text-sm text-muted">
          {job.pages_discovered === 0
            ? "Discovering pages…"
            : `Crawled ${job.pages_crawled} of ${job.pages_discovered} pages discovered.`}
        </p>
      )}
      {problem && (
        <p role="alert" className="text-sm text-danger">
          {problem}
        </p>
      )}
    </section>
  );
}

function Warnings({ warnings }: { warnings: string[] }) {
  return (
    <details className="text-sm text-muted">
      <summary className="cursor-pointer">
        {warnings.length} {warnings.length === 1 ? "warning" : "warnings"} from the last run
      </summary>
      <ul className="mt-2 list-disc space-y-1 pl-5">
        {warnings.map((warning, i) => (
          <li key={i}>{warning}</li>
        ))}
      </ul>
    </details>
  );
}

function HostedUrl({ siteId }: { siteId: string }) {
  const url = hostedUrl(siteId);
  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold">Hosted URL</h2>
      <p className="text-sm text-muted">
        This URL always serves the latest version. Point or redirect your site&apos;s /llms.txt
        here to keep it up to date.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 rounded-md border border-border bg-surface px-3 py-1.5 font-mono text-sm break-all">
          {url}
        </code>
        <CopyButton text={url} label="Copy URL" />
      </div>
    </section>
  );
}
