"use client";

import { useState } from "react";

import { errorText, setMonitoring, type Site } from "@/lib/api";
import { formatDate, formatInterval } from "@/lib/format";

import { failedChecksNote } from "./site-state";

type Props = {
  site: Site;
  onChange: (site: Site) => void;
  // Set when the latest scheduled re-check failed; the shown file is then the last good one.
  recheckError: string | null;
};

export function Monitoring({ site, onChange, recheckError }: Props) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle(enabled: boolean) {
    setSaving(true);
    setError(null);
    try {
      // PATCH returns the updated site, so the check times refresh without another request.
      onChange(await setMonitoring(site.id, enabled));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  }

  const failureNote = failedChecksNote(site.consecutive_failures, site.monitor_enabled);

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold">Monitoring</h2>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {/* A native checkbox is a keyboard- and screen-reader-accessible toggle with no extra ARIA. */}
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={site.monitor_enabled}
            disabled={saving}
            onChange={(event) => toggle(event.target.checked)}
            className="size-4 accent-accent"
          />
          <span>Re-check this site {formatInterval(site.check_interval_hours)}</span>
        </label>
        <span role="status" className="text-sm text-muted">
          {saving ? "Saving…" : ""}
        </span>
      </div>
      {error && (
        <p role="alert" className="text-danger">
          Could not update monitoring: {error}
        </p>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted">Last checked</dt>
        <dd>{formatDate(site.last_checked_at)}</dd>
        <dt className="text-muted">Next check</dt>
        <dd>
          {site.monitor_enabled ? formatDate(site.next_check_at, "Not scheduled") : "Monitoring is off"}
        </dd>
      </dl>
      {failureNote && <p className="text-sm text-muted">{failureNote}</p>}
      {recheckError !== null && (
        <p className="text-sm text-danger">
          The latest scheduled check failed: {recheckError}
          <br />
          The file above is from the last successful run.
        </p>
      )}
    </section>
  );
}
