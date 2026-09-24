/**
 * Representative state view documents for the card's test suites.
 *
 * Not a test file (no `.test.ts` suffix) so vitest does not collect it; kept
 * out of the bundle by never being imported from a shipped module. The
 * shapes follow `engine/view.py` and the wire conventions: UTC ISO-8601
 * instants, seconds, `null` for absent. The day is 2026-09-23 in
 * Europe/Paris (UTC+2): the morning cycle at 07:00 local is 05:00Z, the
 * evening cycle at 20:00 local is 18:00Z.
 */

import { CARD_VERSION } from "./editor";
import type {
  CycleKind,
  HealthView,
  HistoryRow,
  Outcome,
  RunView,
  ScheduleView,
  StateView,
  ZoneRunView,
} from "./types";

export const TIME_ZONE = "Europe/Paris";
export const IRRIGATION_DAY = "2026-09-23";

export const MORNING: ScheduleView = {
  kind: "morning",
  start: "2026-09-23T05:00:00+00:00",
  end: "2026-09-23T05:25:00+00:00",
  zones: [
    { zone_id: "z1", start: "2026-09-23T05:00:00+00:00", end: "2026-09-23T05:10:00+00:00" },
    { zone_id: "z2", start: "2026-09-23T05:10:00+00:00", end: "2026-09-23T05:25:00+00:00" },
  ],
};

/** The spec's golden row: Lawn 20 min then Beds 15 min → 0.571 split. */
export const EVENING: ScheduleView = {
  kind: "evening",
  start: "2026-09-23T18:00:00+00:00",
  end: "2026-09-23T18:35:00+00:00",
  zones: [
    { zone_id: "z1", start: "2026-09-23T18:00:00+00:00", end: "2026-09-23T18:20:00+00:00" },
    { zone_id: "z2", start: "2026-09-23T18:20:00+00:00", end: "2026-09-23T18:35:00+00:00" },
  ],
};

export function zoneRun(overrides: Partial<ZoneRunView> & { zone_id: string }): ZoneRunView {
  return {
    name: overrides.zone_id,
    valve_entity_id: `switch.${overrides.zone_id}`,
    status: "pending",
    planned_start: "2026-09-23T18:00:00+00:00",
    planned_end: "2026-09-23T18:00:00+00:00",
    duration_s: 0,
    base_s: 0,
    carried_s: 0,
    rain_credit_s: 0,
    actual_start: null,
    actual_end: null,
    effective_s: 0,
    ...overrides,
  };
}

/** An evening run quoted SHORTER than the plan (rain credit on Beds). */
export function eveningRun(overrides: Partial<RunView> = {}): RunView {
  return {
    cycle_id: "2026-09-23-evening",
    kind: "evening",
    status: "running",
    manual: false,
    late_rerun: false,
    recovery: null,
    configured_start: "2026-09-23T18:00:00+00:00",
    scheduled_start: "2026-09-23T18:00:00+00:00",
    pump_entity_id: "switch.pump",
    rain_total_mm: 2.5,
    live_zone_id: "z1",
    zones: [
      zoneRun({
        zone_id: "z1",
        name: "Lawn",
        status: "running",
        planned_start: "2026-09-23T18:00:00+00:00",
        planned_end: "2026-09-23T18:20:00+00:00",
        duration_s: 1200,
        base_s: 1200,
        actual_start: "2026-09-23T18:00:02+00:00",
      }),
      zoneRun({
        zone_id: "z2",
        name: "Beds",
        planned_start: "2026-09-23T18:20:00+00:00",
        planned_end: "2026-09-23T18:30:00+00:00",
        duration_s: 600,
        base_s: 900,
        rain_credit_s: 300,
      }),
    ],
    ...overrides,
  };
}

/** The evening run once every zone is done: `status` completed, actuals filled in. */
export function completedEveningRun(overrides: Partial<RunView> = {}): RunView {
  const run = eveningRun({ status: "completed", live_zone_id: null, ...overrides });
  return {
    ...run,
    zones: run.zones.map((zone) => ({
      ...zone,
      status: "completed",
      actual_start: zone.planned_start,
      actual_end: zone.planned_end,
      effective_s: zone.duration_s,
    })),
  };
}

/**
 * One engine-stamped history row for `day` and `kind` with `outcome`, the
 * rest at sane defaults: a completed, non-manual, single-run cycle with
 * zero totals and no rain reading. Override what a test cares about.
 */
export function historyRow(
  day: string,
  kind: CycleKind,
  outcome: Outcome,
  overrides: Partial<HistoryRow> = {},
): HistoryRow {
  return {
    irrigation_day: day,
    kind,
    outcome,
    cycle_id: `${day}-${kind}`,
    status: "completed",
    manual: false,
    waived_by: null,
    recovery: null,
    late_rerun: false,
    runs: 1,
    ended_at: null,
    rain_total_mm: null,
    planned_s: 0,
    carried_s: 0,
    effective_s: 0,
    rain_credit_s: 0,
    ...overrides,
  };
}

/** The spec's golden week: the six days before `IRRIGATION_DAY`, then it, oldest first. */
export const WEEK = [
  "2026-09-17",
  "2026-09-18",
  "2026-09-19",
  "2026-09-20",
  "2026-09-21",
  "2026-09-22",
  "2026-09-23",
];

/** Fourteen rows of `outcome`: every (day, kind) of the golden week. */
export function fullWeek(outcome: Outcome = "ran"): HistoryRow[] {
  return WEEK.flatMap((day) =>
    (["morning", "evening"] as CycleKind[]).map((kind) => historyRow(day, kind, outcome)),
  );
}

export interface ViewOptions {
  morningEnabled?: boolean;
  current?: RunView | null;
  last?: RunView | null;
  cycles?: ScheduleView[];
  /** `generated_at` on the wire; the card's clock offset is estimated from it. */
  generatedAt?: string;
  /** `history` on the wire: the engine's pruned, outcome-stamped rows. */
  history?: HistoryRow[];
  /** `plan.today.irrigation_day`; defaults to `IRRIGATION_DAY`. */
  irrigationDay?: string;
  /** `health` on the wire: the open Repairs-mirrored anomalies; defaults to none open. */
  health?: HealthView;
}

/**
 * One entry of `health.open` as `state_view.health_view` shapes it: the
 * kind plus the subject keys (`zone_id`, `entity_id` or `role`) and nothing
 * else. `kind` is untyped on purpose so a test can push a kind this bundle
 * does not know.
 */
export function anomaly(kind: string, subject: Record<string, unknown> = {}): Record<string, unknown> {
  return { anomaly: kind, ...subject };
}

export function stateView(options: ViewOptions = {}): StateView {
  const morningEnabled = options.morningEnabled ?? true;
  const cycles = options.cycles ?? (morningEnabled ? [MORNING, EVENING] : [EVENING]);
  return {
    schema_version: 1,
    version: CARD_VERSION,
    generated_at: options.generatedAt ?? "2026-09-23T04:00:00+00:00",
    controller: {
      entry_id: "entry-1",
      season_enabled: true,
      reconciled: true,
      manual_override: false,
      config_change_pending: false,
      deferred: [],
      next_wakeup: "2026-09-23T05:00:00+00:00",
    },
    plan: {
      pump_entity_id: "switch.pump",
      morning_enabled: morningEnabled,
      morning_start: "07:00:00",
      evening_start: "20:00:00",
      manual_timeout_s: 1800,
      zones: [
        {
          zone_id: "z1",
          name: "Lawn",
          valve_entity_id: "switch.lawn",
          morning_duration_s: 600,
          evening_duration_s: 1200,
          rain_exposed: true,
          rain_factor: 1,
        },
        {
          zone_id: "z2",
          name: "Beds",
          valve_entity_id: "switch.beds",
          morning_duration_s: 900,
          evening_duration_s: 900,
          rain_exposed: true,
          rain_factor: 0.5,
        },
      ],
      today: { irrigation_day: options.irrigationDay ?? IRRIGATION_DAY, cycles },
    },
    runs: { current: options.current ?? null, last: options.last ?? null },
    ledger: {
      settled_cycle_id: null,
      day_credit: null,
      rain_source: null,
      zones: { z1: { deficit_s: 0, rain_baseline_mm: null }, z2: { deficit_s: 0, rain_baseline_mm: null } },
    },
    history: options.history ?? [],
    health: options.health ?? { open: [], last: null },
  };
}
