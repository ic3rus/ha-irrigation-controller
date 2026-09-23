/**
 * Wall-clock math for the live cursor — pure, DOM-free, hass-free.
 *
 * The card never accumulates ticks: progress is always `serverNow − backend
 * timestamps`, where `serverNow = Date.now() + offset` and `offset` is the
 * difference between the pushed document's `generated_at` and the moment
 * it arrived (server minus client, biased by one network latency —
 * milliseconds on a LAN). A tablet whose clock is minutes off would
 * otherwise draw a zone as finished while the valve is still open. Every
 * push refreshes the estimate; the newest wins. An unparsable
 * `generated_at` leaves the previous estimate alone.
 */

import type { StateView } from "./types";

/** Tick period while `runs.current` is running. */
export const FAST_TICK_MS = 1000;
/** Tick period otherwise: the cursor still crosses a plan row, slowly. */
export const SLOW_TICK_MS = 60000;
/** The fill's CSS transition: slightly longer than the fast tick, so it never stalls. */
export const TRANSITION_MS = 1100;

/**
 * The server-minus-client clock offset a pushed document yields:
 * `Date.parse(generatedAt) − receivedAtMs`, or `undefined` when
 * `generatedAt` does not parse (the caller keeps its previous estimate).
 */
export function estimateOffset(generatedAt: string, receivedAtMs: number): number | undefined {
  const generatedMs = Date.parse(generatedAt);
  if (Number.isNaN(generatedMs)) {
    return undefined;
  }
  return generatedMs - receivedAtMs;
}

/** The server-aligned "now": the client clock plus the offset (0 when unknown). */
export function serverNow(offsetMs: number | undefined, clientNowMs: number = Date.now()): number {
  return clientNowMs + (offsetMs ?? 0);
}

/** 1 s while the current run is running, 60 s otherwise (no document included). */
export function tickPeriodFor(view: StateView | undefined): number {
  return view?.runs.current?.status === "running" ? FAST_TICK_MS : SLOW_TICK_MS;
}
