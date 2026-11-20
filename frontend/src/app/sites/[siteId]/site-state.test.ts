import { describe, expect, it } from "vitest";

import type { Job, Site, Version } from "@/lib/api";

import { failedChecksNote, isPermanentPollError, siteViewState } from "./site-state";

const VERSION: Version = {
  id: "v1",
  site_id: "s1",
  created_at: "2026-11-20T00:00:00Z",
  content: "# Acme\n",
};

function job(overrides: Partial<Job>): Job {
  return {
    id: "j1",
    site_id: "s1",
    kind: "generate",
    status: "succeeded",
    pages_discovered: 0,
    pages_crawled: 0,
    error: null,
    warnings: [],
    created_at: "2026-11-20T00:00:00Z",
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function site(latestJob: Job | null, latestVersion: Version | null): Site {
  return {
    id: "s1",
    url: "https://acme.com",
    monitor_enabled: true,
    check_interval_hours: 24,
    last_checked_at: null,
    next_check_at: null,
    consecutive_failures: 0,
    created_at: "2026-11-20T00:00:00Z",
    latest_job: latestJob,
    latest_version: latestVersion,
    versions: latestVersion ? [{ id: latestVersion.id, created_at: latestVersion.created_at }] : [],
  };
}

describe("siteViewState", () => {
  it.each([
    {
      case: "a finished run offers Re-generate",
      site: site(job({}), VERSION),
      expected: { recheckError: null, failure: null, canRegenerate: true },
    },
    {
      case: "a running job offers nothing",
      site: site(job({ status: "running" }), VERSION),
      expected: { recheckError: null, failure: null, canRegenerate: false },
    },
    {
      case: "a failed generation shows the error panel instead of Re-generate",
      site: site(job({ status: "failed", error: "Blocked." }), null),
      expected: { recheckError: null, failure: "Blocked.", canRegenerate: false },
    },
    {
      case: "a failed re-check keeps the last good file and is only a note",
      site: site(job({ kind: "recheck", status: "failed", error: "Timed out." }), VERSION),
      expected: { recheckError: "Timed out.", failure: null, canRegenerate: true },
    },
    {
      case: "a failed re-check with no earlier file is a real failure",
      site: site(job({ kind: "recheck", status: "failed", error: null }), null),
      expected: {
        recheckError: null,
        failure: "The run failed for an unknown reason.",
        canRegenerate: false,
      },
    },
  ])("$case", ({ site, expected }) => {
    expect(siteViewState(site)).toEqual(expected);
  });
});

describe("isPermanentPollError", () => {
  it.each([
    [404, true], // the job is gone: retrying won't help
    [422, true],
    [408, false], // timeouts and rate limits clear up
    [429, false],
    [500, false], // server errors are usually transient
    [503, false],
    [null, false], // no response at all: network blip
  ])("status %s → %s", (status, permanent) => {
    expect(isPermanentPollError(status)).toBe(permanent);
  });
});

describe("failedChecksNote", () => {
  it.each([
    [0, true, null],
    [1, true, "The last check failed."],
    [3, true, "The last 3 checks failed in a row."],
    [
      5,
      false,
      "Monitoring is off and the last 5 checks failed in a row. Turn it back on to resume scheduled checks.",
    ],
  ])("%i failures, monitoring %s", (failures, enabled, note) => {
    expect(failedChecksNote(failures, enabled)).toBe(note);
  });
});
