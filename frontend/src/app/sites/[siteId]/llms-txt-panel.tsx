"use client";

import { Fragment, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { errorText, getVersion, hostedUrl, type Site, type Version } from "@/lib/api";
import { formatDate, formatDay } from "@/lib/format";

type Props = { site: Site; latest: Version };

// The llms.txt itself (copy, download), its Live URL, the last run's notes, and the history that
// switches which version is shown. The parent renders it with key={latest.id}, so a new latest
// version resets it to showing the latest.
export function LlmsTxtPanel({ site, latest }: Props) {
  // The version on screen; starts as the latest, which the site response already includes.
  const [shown, setShown] = useState<Version>(latest);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function showVersion(versionId: string) {
    setError(null);
    if (versionId === latest.id) {
      setShown(latest);
      return;
    }
    setLoadingId(versionId);
    try {
      setShown(await getVersion(site.id, versionId));
    } catch (err) {
      setError(`Couldn't load that version. ${errorText(err)}`);
    } finally {
      setLoadingId(null);
    }
  }

  const isLatest = shown.id === latest.id;
  // Warnings belong to the run that made the shown file, so they're shown only for the latest
  // file, and only once that run has finished (a running check has none yet).
  const job = site.latest_job;
  const warnings = isLatest && job?.status === "succeeded" ? job.warnings : [];

  return (
    <div className="space-y-10">
      <section className="space-y-3">
        <div className="overflow-hidden rounded-lg border border-border">
          <div className="flex items-center justify-between gap-3 border-b border-border bg-surface px-4 py-2">
            <p className="truncate text-sm text-muted">
              {isLatest ? (
                <span className="font-mono">llms.txt</span>
              ) : (
                <>
                  Version from {formatDate(shown.created_at)} ·{" "}
                  <button
                    type="button"
                    className="btn-text text-foreground underline"
                    onClick={() => showVersion(latest.id)}
                  >
                    Back to current
                  </button>
                </>
              )}
            </p>
            <div className="flex shrink-0 gap-4">
              <CopyButton text={shown.content} label="Copy" />
              <button type="button" className="btn-text" onClick={() => download(shown.content)}>
                Download
              </button>
            </div>
          </div>
          {/* Raw text, as the file is served: what people copy is exactly what they see.
              Focusable so keyboard users can scroll it; a named region so focus is announced. */}
          <pre
            tabIndex={0}
            role="region"
            aria-label="llms.txt content"
            className="max-h-[36rem] overflow-auto p-4 font-mono text-[13px] leading-relaxed break-words whitespace-pre-wrap"
          >
            {shown.content}
          </pre>
        </div>

        <LiveUrl siteId={site.id} />
        {warnings.length > 0 && <Notes warnings={warnings} />}
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-medium">History</h2>
        {site.versions.length <= 1 ? (
          // A version is saved only when the file changes, so one version means no changes yet.
          <p className="text-sm text-muted">No changes since {formatDay(latest.created_at)}.</p>
        ) : (
          <ul className="text-sm">
            {site.versions.map((version) => (
              <li key={version.id}>
                {/* Disabled while one loads, so a slow response can't overwrite a later click. */}
                <button
                  type="button"
                  onClick={() => showVersion(version.id)}
                  disabled={loadingId !== null}
                  aria-pressed={version.id === shown.id}
                  className="-mx-2 flex w-[calc(100%+1rem)] justify-between gap-2 rounded-md px-2 py-1.5 text-left text-muted enabled:hover:bg-surface disabled:cursor-wait aria-pressed:text-foreground"
                >
                  <span>{formatDate(version.created_at)}</span>
                  <span>
                    {loadingId === version.id
                      ? "Loading…"
                      : version.id === latest.id
                        ? "Current"
                        : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {error && (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        )}
      </section>
    </div>
  );
}

// The stable URL that always serves the latest version: what a site owner points /llms.txt at.
function LiveUrl({ siteId }: { siteId: string }) {
  const url = hostedUrl(siteId);
  return (
    <div className="space-y-1 text-sm">
      <div className="flex items-baseline justify-between gap-4">
        <p className="text-muted">Live URL · always serves the latest version</p>
        <CopyButton text={url} label="Copy link" />
      </div>
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="block font-mono text-[13px] underline-offset-4 hover:underline"
      >
        {/* The URL is longer than the column; a break opportunity after each slash wraps it
            between path segments instead of mid-word. */}
        {url.split("/").map((segment, i) => (
          <Fragment key={i}>
            {i > 0 && "/"}
            {i > 0 && <wbr />}
            {segment}
          </Fragment>
        ))}
      </a>
    </div>
  );
}

// The engine's warnings are useful context, not errors: collapsed, grey, no icons.
function Notes({ warnings }: { warnings: string[] }) {
  return (
    <details className="text-sm text-muted">
      <summary className="cursor-pointer select-none hover:text-foreground">
        {warnings.length} {warnings.length === 1 ? "note" : "notes"} from the last run
      </summary>
      <ul className="mt-2 list-disc space-y-1 pl-5">
        {warnings.map((warning, i) => (
          <li key={i}>{warning}</li>
        ))}
      </ul>
    </details>
  );
}

// Builds the file in the browser, so downloading needs no extra request.
function download(content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: "text/plain" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "llms.txt";
  link.click();
  // Revoke later, not immediately: Safari can start the download after click() returns.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
