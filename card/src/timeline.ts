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
 * drawn from `plan.today.cycles` (base durations) unless `runs.current` is a
 * run of the same kind on the same irrigation day, in which case that run's
 * zones' planned windows (quoted durations) are drawn instead.
 *
 * Instants are the wire's UTC ISO-8601 strings, parsed with `Date.parse`.
 * Nothing here formats anything for a human — the element does that with
 * the user's locale.
 */

import type { CycleKind, RunView, ScheduleView, StateView } from "./types";

export type TimelineSource = "plan" | "run";

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
}

export interface TimelineRow {
  kind: CycleKind;
  /** The axis start: the cycle's start, UTC ISO-8601. */
  start: string;
  /** The axis end: the cycle's end, UTC ISO-8601. */
  end: string;
  source: TimelineSource;
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
  }));
  return {
    kind: cycle.kind,
    start: cycle.start,
    end: cycle.end,
    source: "plan",
    segments: segmentsOf(windows, cycle.start, cycle.end),
  };
}

function rowFromRun(run: RunView, names: Map<string, string>): TimelineRow {
  const windows = run.zones.map((zone) => ({
    zoneId: zone.zone_id,
    name: zone.name || (names.get(zone.zone_id) ?? zone.zone_id),
    start: zone.planned_start,
    end: zone.planned_end,
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
    segments: segmentsOf(windows, start, end),
  };
}

/**
 * Build the timeline rows of `view`: today's cycles in plan order, each from
 * the plan or — the authority rule — from `runs.current` when that run is
 * this cycle on this irrigation day. `timeZone` is the controller's IANA
 * zone (`hass.config.time_zone`), used only for the same-day test.
 *
 * A current run whose kind has no planned cycle today (the engine allows
 * `run_now(morning)` while the morning cycle is disabled) gets a row of its
 * own, unless the zone says it belongs to another day; rows are then kept
 * in start order.
 */
export function buildTimeline(view: StateView, timeZone?: string): TimelineModel {
  const names = new Map(view.plan.zones.map((zone) => [zone.zone_id, zone.name]));
  const irrigationDay = view.plan.today.irrigation_day;
  const current = view.runs.current;
  const cycles = view.plan.today.cycles;
  const rows = cycles.map((cycle) =>
    current !== null && isTodaysRun(current, cycle, irrigationDay, timeZone)
      ? rowFromRun(current, names)
      : rowFromPlan(cycle, names),
  );
  if (
    current !== null &&
    !cycles.some((cycle) => cycle.kind === current.kind) &&
    (onIrrigationDay(current, irrigationDay, timeZone) ?? true)
  ) {
    rows.push(rowFromRun(current, names));
    rows.sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
  }
  return { irrigationDay, rows };
}
