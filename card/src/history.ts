/**
 * The 7-day history strip's logic — pure, DOM-free, hass-free (Story 4.4).
 *
 * `buildHistory` turns one state view document into the grid the element
 * draws: seven day columns anchored on `plan.today.irrigation_day` (the six
 * days before today, then today, oldest left), one row per cycle kind, and
 * one `HistoryRow` per (day, kind) that has one. The day is the ENGINE's
 * irrigation day rather than the browser's date on purpose: the engine
 * prunes history on read to today + six and stamps each row with the
 * HA-local date of its configured start, so anchoring on its stamp keeps
 * the columns identical to the rows that will fill them, with no client
 * time-zone computation to shift a 23:45 cycle into the wrong column.
 *
 * Each row's `outcome` is used verbatim: the six `Outcome` literals map
 * one-to-one to a glyph, a label and an anomaly flag in `OUTCOME_VISUALS`,
 * and nothing here re-derives an outcome from `status` or the markers —
 * the engine stamped it once, with its precedence rule (Story 4.1).
 *
 * The labels (`dayLabel`, `cellTitle`) are formatted from the user's
 * language at render time; the model itself holds no formatted text, so it
 * can be memoised on the document reference alone.
 */

import type { CycleKind, HistoryRow, Outcome, StateView } from "./types";

/** The window the strip shows: today and the six days before it. */
export const HISTORY_DAYS = 7;

/** The wording of a cycle kind, shared by today's rows and the strip. */
export const KIND_LABELS: Record<CycleKind, string> = {
  morning: "Morning",
  evening: "Evening",
};

export interface OutcomeVisual {
  /** An `mdi:` icon name for `ha-icon`. */
  icon: string;
  /** The outcome's wording, for the tooltip and the accessible label. */
  label: string;
  /** Does this outcome stand out from the nominal ones (FR27)? */
  anomaly: boolean;
}

/**
 * One glyph and one label per engine outcome. `missed` and `recovered` are
 * the anomalies — the week must read "all is well" or "look here" at a
 * glance; `cancelled` is a deliberate act the operator took (spec 4.1
 * Decision 1) and keeps its own glyph in a neutral colour.
 */
export const OUTCOME_VISUALS: Record<Outcome, OutcomeVisual> = {
  ran: { icon: "mdi:check-circle", label: "Ran", anomaly: false },
  reduced: { icon: "mdi:weather-rainy", label: "Reduced by rain", anomaly: false },
  waived: { icon: "mdi:hand-back-right", label: "Waived by run-now", anomaly: false },
  recovered: { icon: "mdi:backup-restore", label: "Recovered", anomaly: true },
  missed: { icon: "mdi:alert-circle", label: "Missed", anomaly: true },
  cancelled: { icon: "mdi:cancel", label: "Cancelled", anomaly: false },
};

/** The hollow cell: a (day, kind) without a row, including a cycle not run yet today. */
export const NO_RECORD: OutcomeVisual = { icon: "mdi:circle-outline", label: "No record", anomaly: false };

/**
 * A row whose `outcome` this bundle does not know (a stale bundle against a
 * newer backend): a record exists, so the cell is not hollow, but it cannot
 * be read — never an anomaly the card invented.
 */
export const UNKNOWN_OUTCOME: OutcomeVisual = { icon: "mdi:help-circle", label: "Unknown outcome", anomaly: false };

/** The strip's model: the day columns, the kind rows and the rows that fill them. */
export interface HistoryModel {
  /** `YYYY-MM-DD`, oldest first, ending on today's irrigation day; empty when that day is unusable. */
  days: string[];
  /** The rows shown, in plan order (morning first when present). */
  kinds: CycleKind[];
  /** The in-window history rows, keyed by `cellKey(day, kind)`. */
  cells: Map<string, HistoryRow>;
}

const DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * `YYYY-MM-DD` → epoch ms at UTC midnight, or `undefined` when the string
 * is not a real calendar date (`Date.UTC` would roll `2026-13-45` over
 * into a valid instant; the round trip catches that).
 */
function dayToMs(day: string): number | undefined {
  if (typeof day !== "string" || !DAY_PATTERN.test(day)) {
    return undefined;
  }
  const [year, month, dayOfMonth] = day.split("-").map(Number) as [number, number, number];
  const ms = Date.UTC(year, month - 1, dayOfMonth);
  return msToDay(ms) === day ? ms : undefined;
}

function msToDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/** The key of one cell in `HistoryModel.cells`. */
export function cellKey(day: string, kind: CycleKind): string {
  return `${day}|${kind}`;
}

/**
 * The seven irrigation days ending on `today`, oldest first — pure calendar
 * arithmetic on `YYYY-MM-DD` strings via `Date.UTC`, so no time zone or DST
 * rule can add or drop a day. Empty when `today` is not a calendar date.
 */
export function weekDays(today: string): string[] {
  const end = dayToMs(today);
  if (end === undefined) {
    return [];
  }
  const days: string[] = [];
  for (let offset = HISTORY_DAYS - 1; offset >= 0; offset -= 1) {
    days.push(msToDay(end - offset * DAY_MS));
  }
  return days;
}

/**
 * Build the strip's model from `view`: the week anchored on
 * `plan.today.irrigation_day`, the evening row always and the morning row
 * when the morning cycle is enabled or any in-window row is a morning one
 * (a morning row recorded before the cycle was disabled still happened),
 * and every `history` row whose `irrigation_day` falls in the window. A row
 * whose day is malformed or out of the window is dropped, never thrown on.
 */
export function buildHistory(view: StateView): HistoryModel {
  const days = weekDays(view.plan.today.irrigation_day);
  const inWindow = new Set(days);
  const cells = new Map<string, HistoryRow>();
  let morning = view.plan.morning_enabled;
  const history = Array.isArray(view.history) ? view.history : [];
  for (const row of history) {
    if (!inWindow.has(row.irrigation_day)) {
      continue;
    }
    cells.set(cellKey(row.irrigation_day, row.kind), row);
    if (row.kind === "morning") {
      morning = true;
    }
  }
  const kinds: CycleKind[] = morning ? ["morning", "evening"] : ["evening"];
  return { days, kinds, cells };
}

/**
 * A formatter for `language` (`hass.locale.language`), or for the browser's
 * default locale when `language` is absent or not a locale tag `Intl` knows.
 */
function dateFormat(language: string | undefined, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  try {
    return new Intl.DateTimeFormat(language, options);
  } catch {
    return new Intl.DateTimeFormat(undefined, options);
  }
}

function numberFormat(language: string | undefined): Intl.NumberFormat {
  const options: Intl.NumberFormatOptions = { maximumFractionDigits: 1 };
  try {
    return new Intl.NumberFormat(language, options);
  } catch {
    return new Intl.NumberFormat(undefined, options);
  }
}

/** Format `day` (`YYYY-MM-DD`) at UTC midnight, or return it as is when it is not a date. */
function formatDay(day: string, language: string | undefined, options: Intl.DateTimeFormatOptions): string {
  const ms = dayToMs(day);
  if (ms === undefined) {
    return day;
  }
  return dateFormat(language, { ...options, timeZone: "UTC" }).format(new Date(ms));
}

/**
 * The column header of `day`: its short weekday name ("Mon") in `language`.
 * Formatted at UTC on a UTC-midnight instant, so the browser's zone cannot
 * move the label to the day before.
 */
export function dayLabel(day: string, language?: string): string {
  return formatDay(day, language, { weekday: "short" });
}

/**
 * The tooltip (and accessible label) of one cell: the long date, the cycle
 * kind, the outcome's wording, then — from the row, when it has them — the
 * minutes actually watered and the rain total, joined by " · ". Without a
 * row the cell says "No record".
 */
export function cellTitle(
  day: string,
  kind: CycleKind,
  row: HistoryRow | undefined,
  language?: string,
): string {
  const parts = [
    formatDay(day, language, { weekday: "short", month: "short", day: "numeric" }),
    KIND_LABELS[kind],
    (row === undefined ? NO_RECORD : (OUTCOME_VISUALS[row.outcome] ?? UNKNOWN_OUTCOME)).label,
  ];
  if (row !== undefined) {
    if (row.effective_s > 0) {
      parts.push(`${Math.max(1, Math.round(row.effective_s / 60))} min watered`);
    }
    if (row.rain_total_mm !== null && Number.isFinite(row.rain_total_mm)) {
      parts.push(`${numberFormat(language).format(row.rain_total_mm)} mm rain`);
    }
  }
  return parts.join(" · ");
}
