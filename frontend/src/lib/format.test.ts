import { describe, expect, it } from "vitest";

import { checkSchedule, timeAgo } from "./format";

describe("timeAgo", () => {
  const now = Date.parse("2026-11-20T12:00:00Z");
  const ago = (seconds: number) => new Date(now - seconds * 1000).toISOString();

  it.each([
    [0, "just now"],
    [59, "just now"],
    [60, "1 minute ago"],
    [5 * 60, "5 minutes ago"],
    [3 * 3600, "3 hours ago"],
    [24 * 3600, "yesterday"],
    [3 * 24 * 3600, "3 days ago"],
    [14 * 24 * 3600, "2 weeks ago"],
    [60 * 24 * 3600, "2 months ago"],
    [400 * 24 * 3600, "last year"],
  ])("%i seconds ago → %s", (seconds, text) => {
    expect(timeAgo(ago(seconds), now)).toBe(text);
  });

  it("treats a timestamp slightly in the future (clock skew) as just now", () => {
    expect(timeAgo(ago(-30), now)).toBe("just now");
  });
});

describe("checkSchedule", () => {
  it.each([
    [24, "Checked daily"],
    [1, "Checked hourly"],
    [6, "Checked every 6 hours"],
  ])("%i hours → %s", (hours, text) => {
    expect(checkSchedule(hours)).toBe(text);
  });
});
