"use client";

import { useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { errorText, getVersion, type Site, type Version } from "@/lib/api";
import { formatDate } from "@/lib/format";

type Props = { site: Site; latest: Version };

// The llms.txt viewer (copy, download) and the version history that switches what it shows.
// The parent renders it with key={latest.id}, so a new latest version resets it to "latest".
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
      setError(errorText(err));
    } finally {
      setLoadingId(null);
    }
  }

  const isLatest = shown.id === latest.id;

  return (
    <>
      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">
            llms.txt{" "}
            <span className="text-sm font-normal text-muted">
              {isLatest ? "latest" : "older version"}, {formatDate(shown.created_at)}
            </span>
          </h2>
          <div className="flex flex-wrap gap-2">
            <CopyButton text={shown.content} label="Copy" />
            <button type="button" className="btn" onClick={() => download(shown.content)}>
              Download
            </button>
          </div>
        </div>
        {/* Focusable so keyboard users can scroll it; a named region so that focus is announced. */}
        <pre
          tabIndex={0}
          role="region"
          aria-label="llms.txt content"
          className="max-h-[32rem] overflow-auto rounded-md border border-border bg-surface p-4 font-mono text-sm break-words whitespace-pre-wrap"
        >
          {shown.content}
        </pre>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Versions</h2>
        <p className="text-sm text-muted">
          A new version is saved only when the generated file changes.
        </p>
        {error && (
          <p role="alert" className="text-danger">
            Could not load that version: {error}
          </p>
        )}
        <ul className="divide-y divide-border rounded-md border border-border">
          {site.versions.map((version) => (
            <li key={version.id}>
              {/* Disabled while one loads, so a slow response can't overwrite a later click. */}
              <button
                type="button"
                onClick={() => showVersion(version.id)}
                disabled={loadingId !== null}
                aria-pressed={version.id === shown.id}
                className="flex w-full justify-between gap-2 px-3 py-2 text-left text-sm enabled:hover:bg-surface disabled:cursor-wait aria-pressed:font-semibold"
              >
                <span>{formatDate(version.created_at)}</span>
                <span className="text-muted">
                  {loadingId === version.id ? "Loading…" : version.id === latest.id ? "latest" : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </>
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
