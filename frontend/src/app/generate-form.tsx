"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { createSite, errorText } from "@/lib/api";

export function GenerateForm() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // `replace` is for the /?url= path below: there, router.replace("/") may still be pending when
  // this resolves, and a push would pre-empt it and leave /?url=... in history, so Back would
  // submit again. Replacing that entry with the site page makes Back skip it entirely.
  async function start(siteUrl: string, replace = false) {
    setSubmitting(true);
    setError(null);
    try {
      const { site_id } = await createSite(siteUrl.trim());
      // The site page finds the new job through the site's latest_job, so the URL needs no job id.
      const sitePage = `/sites/${site_id}`;
      if (replace) router.replace(sitePage);
      else router.push(sitePage);
      // submitting stays true on success: the button stays disabled until the next page replaces
      // this one, so a second click can't send another request.
    } catch (err) {
      setError(errorText(err));
      setSubmitting(false);
    }
  }

  // A submit before the page's JavaScript has loaded (slow network, cold start) is a plain GET
  // to /?url=..., because the input has a name. Pick it up here and carry on, so the click isn't
  // lost. The parameter is removed first, so a reload doesn't submit again.
  useEffect(() => {
    const pending = new URLSearchParams(window.location.search).get("url");
    if (!pending) return;
    // Through the router, not history.replaceState: a raw replace on mount can run before Next
    // has taken over history, and leaves an entry Next's Back handling ignores.
    router.replace(window.location.pathname);
    // Syncing from an external system (the address bar), once, on a rare path: the extra render
    // this costs is the point, so the box shows what is being generated.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setUrl(pending);
    void start(pending, true);
    // Runs once, on mount: start reads nothing that changes later.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="mx-auto max-w-lg text-left">
      <form
        action="/"
        onSubmit={(event) => {
          event.preventDefault();
          void start(url);
        }}
        className="flex flex-col gap-2 sm:flex-row"
      >
        <label htmlFor="url" className="sr-only">
          Website URL
        </label>
        {/* type="text" rather than "url": the browser's URL check rejects "example.com",
            which the API accepts and normalizes. */}
        <input
          id="url"
          name="url"
          type="text"
          inputMode="url"
          autoComplete="url"
          autoFocus
          required
          placeholder="example.com"
          value={url}
          readOnly={submitting}
          onChange={(event) => setUrl(event.target.value)}
          aria-invalid={error !== null}
          aria-describedby={error ? "url-error" : undefined}
          className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 placeholder:text-muted/60 read-only:text-muted"
        />
        {/* The first request after a quiet period can take a few seconds; the button saying
            "Generating…" covers that wait without explaining why. */}
        <button type="submit" disabled={submitting} className="btn btn-primary px-4 py-2">
          {submitting ? "Generating…" : "Generate"}
        </button>
      </form>

      {error && (
        <p id="url-error" role="alert" className="mt-2 text-sm text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
