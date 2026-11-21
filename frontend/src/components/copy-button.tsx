"use client";

import { useEffect, useRef, useState } from "react";

type Props = { text: string; label: string };

export function CopyButton({ text, label }: Props) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Don't leave the timer running after the button goes away (e.g. the version shown changes).
  useEffect(() => () => clearTimeout(resetTimer.current ?? undefined), []);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      // The Clipboard API is unavailable on non-HTTPS pages and can be denied by the user.
      setState("failed");
    }
    // A quick second click restarts the 2s window instead of being cut short by the first timer.
    if (resetTimer.current) clearTimeout(resetTimer.current);
    resetTimer.current = setTimeout(() => setState("idle"), 2000);
  }

  const feedback = state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "";
  return (
    <>
      <button type="button" onClick={copy} className="btn-text">
        {feedback || label}
      </button>
      {/* Screen readers announce the result from this live region; the button's own name
          changing is not reliably announced. */}
      <span role="status" className="sr-only">
        {feedback}
      </span>
    </>
  );
}
