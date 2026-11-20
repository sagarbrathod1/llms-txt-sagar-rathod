// Thin typed wrapper over fetch for the backend API. The types come from api-types.ts, which is
// generated from the API's OpenAPI schema (npm run gen:api), so a contract change shows up here
// as a type error.
import type { components } from "./api-types";

type Schemas = components["schemas"];
export type Site = Schemas["SiteResponse"];
export type Job = Schemas["JobResponse"];
export type ActiveJob = Job & { status: "queued" | "running" };
export type Version = Schemas["VersionResponse"];

export const POLL_INTERVAL_MS = 1500;

// Without a limit, a request that never answers (a stalled proxy) would leave a button spinning
// or silently end polling, which waits on each request. Generous, because the first request after
// a quiet period also waits for the API machine to start and the database to resume.
const REQUEST_TIMEOUT_MS = 30_000;

// Inlined at build time. Trailing slashes are stripped so "https://api.example.com/" still
// joins cleanly with "/sites".
const CONFIGURED_API_URL = process.env.NEXT_PUBLIC_API_URL?.replace(/\/+$/, "");

// Read at request time rather than module load, so `npm run build` (e.g. in CI) succeeds without
// the variable, while a deployed build that forgot it fails loudly on its first request instead
// of silently calling localhost.
function apiUrl(): string {
  if (CONFIGURED_API_URL) return CONFIGURED_API_URL;
  if (process.env.NODE_ENV === "development") return "http://localhost:8000";
  throw new Error("NEXT_PUBLIC_API_URL was not set when this app was built.");
}

// An error from the API. status is the HTTP status, or null when the request never got a
// response (network failure, CORS), so callers can tell permanent errors from transient ones.
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
  }
}

export function httpStatus(err: unknown): number | null {
  return err instanceof ApiError ? err.status : null;
}

// Turns any thrown value into text that is safe to show in the UI.
export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

// Queued and running jobs can still change, so the UI keeps polling them.
export function isActive(job: Job | null): job is ActiveJob {
  return job !== null && (job.status === "queued" || job.status === "running");
}

// The public URL that always serves a site's latest llms.txt.
export function hostedUrl(siteId: string): string {
  return `${apiUrl()}/sites/${siteId}/llms.txt`;
}

export function createSite(url: string): Promise<Schemas["CreateSiteResponse"]> {
  const body: Schemas["CreateSiteRequest"] = { url };
  return request("/sites", { method: "POST", body: JSON.stringify(body) });
}

export function getSite(siteId: string): Promise<Site> {
  return request(`/sites/${encodeURIComponent(siteId)}`);
}

export function getJob(jobId: string): Promise<Job> {
  return request(`/jobs/${encodeURIComponent(jobId)}`);
}

export function getVersion(siteId: string, versionId: string): Promise<Version> {
  return request(
    `/sites/${encodeURIComponent(siteId)}/versions/${encodeURIComponent(versionId)}`,
  );
}

export function setMonitoring(siteId: string, enabled: boolean): Promise<Site> {
  const body: Schemas["UpdateSiteRequest"] = { monitor_enabled: enabled };
  return request(`/sites/${encodeURIComponent(siteId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // Built outside the try below, so a missing NEXT_PUBLIC_API_URL isn't reported as a network error.
  const url = `${apiUrl()}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      // Only send Content-Type with a body: on a GET it would force a needless CORS preflight.
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (err) {
    // Both cases have no HTTP status, so callers treat them as transient.
    if (err instanceof DOMException && err.name === "TimeoutError") {
      throw new ApiError("The API took too long to answer. Try again shortly.", null);
    }
    // Otherwise fetch rejects only when no response arrives: offline, server down, or a CORS
    // rejection (the browser hides which, so the message names both).
    throw new ApiError("Could not reach the API (network or CORS problem). Try again shortly.", null);
  }
  if (!res.ok) {
    throw new ApiError(await errorMessage(res), res.status);
  }
  return (await res.json()) as T;
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json();
    // FastAPI sends {detail: "..."} for HTTPException and {detail: [{msg}, ...]} for validation errors.
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail) && typeof body.detail[0]?.msg === "string") {
      return body.detail[0].msg;
    }
  } catch {
    // Not JSON (e.g. a proxy error page); fall through to the generic message.
  }
  return `Request failed (HTTP ${res.status}).`;
}
