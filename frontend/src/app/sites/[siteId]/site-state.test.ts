import { describe, expect, it } from "vitest";

import type { ActiveJob, Job, Site, Version } from "@/lib/api";

import {
  isPermanentPollError,
  progressFraction,
  progressLine,
  siteStatus,
  siteView,
} from "./site-state";

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

function site(
  latestJob: Job | null,
  latestVersion: Version | null,
  overrides: Partial<Site> = {},
): Site {
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
    ...overrides,
  };
}

const V2: Version = { ...VERSION, id: "v2", created_at: "2026-11-21T00:00:00Z" };
const CHECKED = "2026-11-21T00:00:00Z";

describe("siteView", () => {
  it("shows the file once there is one, even while a new run is going", () => {
    expect(siteView(site(job({ status: "running" }), VERSION))).toEqual({
      kind: "ready",
      version: VERSION,
    });
  });

  it("shows the first generation while it runs", () => {
    const running = job({ status: "running" });
    expect(siteView(site(running, null))).toEqual({ kind: "generating", job: running });
  });

  it("shows the failure when the first run failed", () => {
    expect(siteView(site(job({ status: "failed", error: "Blocked." }), null))).toEqual({
      kind: "failed",
      error: "Blocked.",
    });
  });

  it("falls back to a generic message when the failure has none", () => {
    expect(siteView(site(job({ status: "failed" }), null))).toEqual({
      kind: "failed",
      error: "Something went wrong while generating.",
    });
  });
});

describe("progress", () => {
  const active = (overrides: Partial<Job>) => job({ status: "running", ...overrides }) as ActiveJob;

  it.each([
    ["queued", active({ status: "queued" }), "Getting started…", 0],
    ["nothing found yet", active({}), "Finding pages…", 0],
    ["crawling", active({ pages_discovered: 100, pages_crawled: 42 }), "42 of 100 pages read", 0.42],
    ["writing", active({ pages_discovered: 30, pages_crawled: 30 }), "Writing llms.txt…", 1],
  ])("%s", (_, j, line, fraction) => {
    expect(progressLine(j)).toBe(line);
    expect(progressFraction(j)).toBe(fraction);
  });
});

describe("siteStatus", () => {
  const texts = (s: ReturnType<typeof siteStatus>) =>
    s.parts.map((p) => (p.at ? `${p.text} @${p.at}` : p.text));

  it("shows the schedule, last check and last change at rest", () => {
    const status = siteStatus(site(job({}), VERSION, { last_checked_at: CHECKED }), null);
    expect(texts(status)).toEqual([
      "Checked daily",
      `Last checked @${CHECKED}`,
      `Last changed @${VERSION.created_at}`,
    ]);
    expect(status).toMatchObject({ checking: false, failure: null, canResume: false });
  });

  it("shows a running check without hiding anything else", () => {
    const status = siteStatus(site(job({ status: "running" }), VERSION), null);
    expect(texts(status)).toEqual(["Checked daily", "Checking for updates…"]);
    expect(status.checking).toBe(true);
  });

  it("says Updated when a watched run produced a new version", () => {
    const status = siteStatus(site(job({}), V2), { jobId: "j1", previousVersionId: "v1" });
    expect(texts(status)).toEqual(["Checked daily", `Updated @${V2.created_at}`]);
  });

  it("says No changes when a watched run kept the same version", () => {
    const s = site(job({}), VERSION, { last_checked_at: CHECKED });
    expect(texts(siteStatus(s, { jobId: "j1", previousVersionId: "v1" }))).toEqual([
      "Checked daily",
      `No changes · checked @${CHECKED}`,
      `Last changed @${VERSION.created_at}`,
    ]);
  });

  it("reports no outcome for a first generation or a run this page didn't watch", () => {
    const s = site(job({}), VERSION, { last_checked_at: CHECKED });
    const atRest = texts(siteStatus(s, null));
    expect(texts(siteStatus(s, { jobId: "j1", previousVersionId: null }))).toEqual(atRest);
    expect(texts(siteStatus(s, { jobId: "older-job", previousVersionId: "v1" }))).toEqual(atRest);
  });

  it("keeps the last file on a failed check and says why", () => {
    const failed = job({ status: "failed", error: "Timed out.", finished_at: CHECKED });
    const status = siteStatus(site(failed, VERSION), null);
    expect(texts(status)).toEqual([
      "Checked daily",
      `Last check failed @${CHECKED}`,
      `Last changed @${VERSION.created_at}`,
    ]);
    expect(status.failure).toBe("Timed out.");
  });

  it("offers to resume only when monitoring is off", () => {
    const status = siteStatus(site(job({}), VERSION, { monitor_enabled: false }), null);
    expect(status.parts[0].text).toBe("Not checked automatically");
    expect(status.canResume).toBe(true);
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
