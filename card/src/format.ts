/**
 * Pure formatting helpers for the timeline card.
 * Seed for the vitest harness — the real timeline logic arrives in Epic 4.
 */

/** Format a duration in seconds as "H:MM:SS" (or "M:SS" under an hour). */
export function formatDuration(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const seconds = safe % 60;
  const ss = String(seconds).padStart(2, "0");
  if (hours === 0) {
    return `${minutes}:${ss}`;
  }
  return `${hours}:${String(minutes).padStart(2, "0")}:${ss}`;
}
