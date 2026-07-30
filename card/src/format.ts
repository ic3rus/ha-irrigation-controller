/**
 * Pure formatting helpers for the timeline card.
 * Seed for the vitest harness — the real timeline logic arrives in Epic 4.
 */

/**
 * Format a duration in seconds as "H:MM:SS" (or "M:SS" under an hour).
 *
 * Non-finite input formats as "0:00" rather than "NaN:NaN": callers read HA state
 * attributes, where `unknown`/`unavailable` coerce to NaN, and a timeline must
 * never render NaN to the user.
 */
export function formatDuration(totalSeconds: number): string {
  const safe = Number.isFinite(totalSeconds)
    ? Math.max(0, Math.floor(totalSeconds))
    : 0;
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const seconds = safe % 60;
  const ss = String(seconds).padStart(2, "0");
  if (hours === 0) {
    return `${minutes}:${ss}`;
  }
  return `${hours}:${String(minutes).padStart(2, "0")}:${ss}`;
}
