"use client";

// Shown instead of a blank page when rendering throws (e.g. a production build made without
// NEXT_PUBLIC_API_URL). API errors don't land here: the pages catch those and show them inline.
export default function Error({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div role="alert" className="space-y-3">
      <h1 className="text-2xl font-semibold">Something went wrong</h1>
      <p className="text-muted">{error.message || "The page failed to load."}</p>
      <button type="button" className="btn" onClick={reset}>
        Try again
      </button>
    </div>
  );
}
