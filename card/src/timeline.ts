/**
 * Schedule geometry for the timeline card — pure, DOM-free, hass-free.
 *
 * `buildTimeline` turns one state view document into rows the element can
 * draw without doing any date arithmetic of its own: one row per cycle in
 * `plan.today.cycles` order, each with its OWN axis running from the cycle's
 * start to its end, and one segment per zone with `x0`/`x1` as fractions of
 * that axis. Fractions rather than pixels on purpose: the SVG that draws
 * them uses `viewBox="0 0 1000 H"` with `preserveAspectRatio="none"`, so
 * `x = fraction * 1000` follows the card width with no resize observer.
 *
 * The authority rule from Story 4.1 lives here and nowhere else: a cycle is
 * drawn from `plan.today.cycles` (base durations) unless `runs.current` —
 * or, once the cycle is over, `runs.last` (Story 4.3) — is a run of the same
 * kind on the same irrigation day, in which case that run's zones' planned
 * windows (quoted durations) and engine statuses are drawn instead.
 * Statuses come from the engine verbatim; nothing here derives an outcome.
 *
 * The live half (`cursorAt`, `progressOf`) is wall-clock math against the
 * same rows: the element hands in the server-aligned "now" (`clock.ts`) and
 * gets back fractions of the row's axis, so a tick costs no date parsing of
 * its own beyond the row's two bounds.
 *
 * Instants are the wire's UTC ISO-8601 strings, parsed with `Date.parse`.
 * Nothing here formats anything for a human — the element does that with
 * the user's locale.
 */

import type { CycleKind, CycleStatus, RunView, ScheduleView, StateView, ZoneRunStatus } from "./types";

export type TimelineSource = "plan" | "run";

/** A segment's look: `planned` from the plan, the engine's status from a run. */
export type SegmentStatus = "planned" | ZoneRunStatus;

/** A row's look: `planned` from the plan, the engine's cycle status from a run. */
export type RowStatus = "planned" | CycleStatus;

export interface TimelineSegment {
  zoneId: string;
  name: string;
  /** Planned start, UTC ISO-8601 as on the wire. */
  start: string;
  /** Planned end, UTC ISO-8601 as on the wire. */
  end: string;
  /** Left edge as a fraction of the row's axis, in [0, 1]. */
  x0: number;
  /** Right edge as a fraction of the row's axis, in [0, 1]; `x0 <= x1`. */
  x1: number;
  status: SegmentStatus;
}

export interface TimelineRow {
  kind: CycleKind;
  /** The axis start: the cycle's start, UTC ISO-8601. */
  start: string;
  /** The axis end: the cycle's end, UTC ISO-8601. */
  end: string;
  source: TimelineSource;
  status: RowStatus;
  /** Empty when the cycle has no zones — the element shows a hint instead. */
  segments: TimelineSegment[];
}

export interface TimelineModel {
  irrigationDay: string;
  rows: TimelineRow[];
}

/**
 * Return the calendar date (`YYYY-MM-DD`) of `iso` in `timeZone`, mirroring
 * the engine's `irrigation_day` (the Home Assistant-local date of a cycle's
 * configured start). `undefined` when the instant or the zone is unusable —
 * the caller then falls back to an exact-instant comparison.
 */
export function localDate(iso: string, timeZone: string): string | undefined {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) {
    return undefined;
  }
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date(ms));
    const part = (type: string): string | undefined =>
      parts.find((candidate) => candidate.type === type)?.value;
    const year = part("year");
    const month = part("month");
    const day = part("day");
    if (year === undefined || month === undefined || day === undefined) {
      return undefined;
    }
    return `${year}-${month}-${day}`;
  } catch {
    // An invalid IANA zone name throws a RangeError.
    return undefined;
  }
}

/**
 * Is `run`'s configured start on `irrigationDay` in `timeZone`? `undefined`
 * when there is no usable zone to attribute days with.
 */
function onIrrigationDay(
  run: RunView,
  irrigationDay: string,
  timeZone?: string,
): boolean | undefined {
  const day =
    timeZone === undefined ? undefined : localDate(run.configured_start, timeZone);
  return day === undefined ? undefined : day === irrigationDay;
}

/**
 * The authority rule's test: is `run` THE run of `cycle` on today's
 * irrigation day? Same kind, and `configured_start` on `irrigationDay` in
 * the controller's time zone. Without a usable zone the two configured
 * starts are compared as instants instead — identical unless the operator
 * edited the start time while the cycle was running.
 */
export function isTodaysRun(
  run: RunView,
  cycle: ScheduleView,
  irrigationDay: string,
  timeZone?: string,
): boolean {
  if (run.kind !== cycle.kind) {
    return false;
  }
  return onIrrigationDay(run, irrigationDay, timeZone) ?? run.configured_start === cycle.start;
}

function clamp01(value: number): number {
  if (Number.isNaN(value)) {
    return 0;
  }
  return Math.min(1, Math.max(0, value));
}

interface ZoneWindow {
  zoneId: string;
  name: string;
  start: string;
  end: string;
  status: SegmentStatus;
}

/** Lay `windows` out along the axis `start`..`end` as fractions. */
function segmentsOf(windows: ZoneWindow[], start: string, end: string): TimelineSegment[] {
  const axisStart = Date.parse(start);
  const span = Date.parse(end) - axisStart;
  return windows.map((window) => {
    if (!(span > 0)) {
      // A zero-length (or unparsable) axis: every segment collapses to a
      // point rather than dividing by zero.
      return { ...window, x0: 0, x1: 0 };
    }
    const x0 = clamp01((Date.parse(window.start) - axisStart) / span);
    const x1 = clamp01((Date.parse(window.end) - axisStart) / span);
    return { ...window, x0, x1: Math.max(x0, x1) };
  });
}

function earliest(instants: string[]): string | undefined {
  let best: string | undefined;
  let bestMs = Number.POSITIVE_INFINITY;
  for (const instant of instants) {
    const ms = Date.parse(instant);
    if (ms < bestMs) {
      bestMs = ms;
      best = instant;
    }
  }
  return best;
}

function latest(instants: string[]): string | undefined {
  let best: string | undefined;
  let bestMs = Number.NEGATIVE_INFINITY;
  for (const instant of instants) {
    const ms = Date.parse(instant);
    if (ms > bestMs) {
      bestMs = ms;
      best = instant;
    }
  }
  return best;
}

function rowFromPlan(cycle: ScheduleView, names: Map<string, string>): TimelineRow {
  const windows = cycle.zones.map((zone) => ({
    zoneId: zone.zone_id,
    name: names.get(zone.zone_id) ?? zone.zone_id,
    start: zone.start,
    end: zone.end,
    status: "planned" as const,
  }));
  return {
    kind: cycle.kind,
    start: cycle.start,
    end: cycle.end,
    source: "plan",
    status: "planned",
    segments: segmentsOf(windows, cycle.start, cycle.end),
  };
}

function rowFromRun(run: RunView, names: Map<string, string>): TimelineRow {
  const windows = run.zones.map((zone) => ({
    zoneId: zone.zone_id,
    name: zone.name || (names.get(zone.zone_id) ?? zone.zone_id),
    start: zone.planned_start,
    end: zone.planned_end,
    status: zone.status,
  }));
  // The run's axis is its zones' planned envelope; a run without zones
  // degenerates to a point at its scheduled start.
  const start = earliest(windows.map((window) => window.start)) ?? run.scheduled_start;
  const end = latest(windows.map((window) => window.end)) ?? run.scheduled_start;
  return {
    kind: run.kind,
    start,
    end,
    source: "run",
    status: run.status,
    segments: segmentsOf(windows, start, end),
  };
}

/**
 * Build the timeline rows of `view`: today's cycles in plan order, each from
 * the plan or — the authority rule — from `runs.current`, then `runs.last`,
 * whichever is this cycle's run on this irrigation day. `timeZone` is the
 * controller's IANA zone (`hass.config.time_zone`), used only for the
 * same-day test.
 *
 * `last` joins the rule because the engine clears `current` in the same
 * step that finishes the run: the push showing the final zone `completed`
 * is the push where `current` becomes `null`, and without `last` the row
 * would snap back to the base-duration plan. Yesterday's `last` fails the
 * same-day test and the plan is drawn.
 *
 * A run whose kind has no planned cycle today (the engine allows
 * `run_now(morning)` while the morning cycle is disabled) gets a row of its
 * own — `current` first, then `last`, so the row survives the run's own
 * completion — unless the zone says it belongs to another day; rows are
 * then kept in start order.
 */
export function buildTimeline(view: StateView, timeZone?: string): TimelineModel {
  const names = new Map(view.plan.zones.map((zone) => [zone.zone_id, zone.name]));
  const irrigationDay = view.plan.today.irrigation_day;
  const { current, last } = view.runs;
  const cycles = view.plan.today.cycles;
  const pick = (run: RunView | null, cycle: ScheduleView): TimelineRow | undefined =>
    run !== null && isTodaysRun(run, cycle, irrigationDay, timeZone) ? rowFromRun(run, names) : undefined;
  const rows = cycles.map(
    (cycle) => pick(current, cycle) ?? pick(last, cycle) ?? rowFromPlan(cycle, names),
  );
  const extra = [current, last].find(
    (run): run is RunView =>
      run !== null &&
      !cycles.some((cycle) => cycle.kind === run.kind) &&
      (onIrrigationDay(run, irrigationDay, timeZone) ?? true),
  );
  if (extra !== undefined) {
    rows.push(rowFromRun(extra, names));
    rows.sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
  }
  return { irrigationDay, rows };
}

/**
 * Where "now" sits on `row`'s axis, as a fraction: defined only while
 * `start ≤ now ≤ end` on a span of positive length. Outside the row, on a
 * zero-length axis or with an unusable instant there is no cursor.
 */
export function cursorAt(row: TimelineRow, nowMs: number): number | undefined {
  const start = Date.parse(row.start);
  const span = Date.parse(row.end) - start;
  if (!(span > 0) || !(nowMs >= start) || !(nowMs <= start + span)) {
    return undefined;
  }
  return (nowMs - start) / span;
}

/**
 * How much of `segment` is filled, in [0, 1]: a running zone fills up to the
 * cursor (all the way once the cursor has left the row — the engine, not
 * the clock, decides when it is done); a completed or failed zone is full;
 * a planned, pending or skipped one is empty. A zero-width segment has
 * nothing to fill.
 */
export function progressOf(segment: TimelineSegment, cursor: number | undefined): number {
  switch (segment.status) {
    case "running": {
      const width = segment.x1 - segment.x0;
      if (!(width > 0)) {
        return 0;
      }
      const at = Math.min(segment.x1, Math.max(segment.x0, cursor ?? segment.x1));
      return (at - segment.x0) / width;
    }
    case "completed":
    case "failed":
      return 1;
    default:
      return 0;
  }
}
