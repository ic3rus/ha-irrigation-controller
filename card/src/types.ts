/**
 * The TypeScript mirror of the engine state view (`engine/view.py`).
 *
 * Mirrored key for key and adding nothing: the schema is defined
 * backend-side (Story 4.1, AD-14) and this file only names it for the
 * compiler. Wire conventions hold verbatim — snake_case keys, every instant
 * a UTC ISO-8601 string, every duration in seconds, `null` for absent,
 * enums as their string values, the zone subentry id as the zone key.
 */

/** `engine/plan.py` `CycleKind`. */
export type CycleKind = "morning" | "evening";

/** `engine/runs.py` `CycleStatus`. */
export type CycleStatus =
  | "pending"
  | "running"
  | "completed"
  | "cancelled"
  | "waived"
  | "interrupted"
  | "missed";

/** `engine/runs.py` `ZoneRunStatus`. */
export type ZoneRunStatus = "pending" | "running" | "completed" | "failed" | "skipped";

/** `engine/view.py` `Outcome` — the one outcome a history row carries. */
export type Outcome = "ran" | "reduced" | "waived" | "recovered" | "missed" | "cancelled";

/** The `state_subscribe` result, and the same two keys atop every document. */
export interface Handshake {
  schema_version: number;
  version: string;
}

export interface ControllerView {
  entry_id: string;
  season_enabled: boolean;
  reconciled: boolean;
  manual_override: boolean;
  config_change_pending: boolean;
  deferred: string[];
  next_wakeup: string | null;
}

export interface PlanZoneView {
  zone_id: string;
  name: string;
  valve_entity_id: string;
  morning_duration_s: number;
  evening_duration_s: number;
  rain_exposed: boolean;
  rain_factor: number;
}

export interface ZoneWindowView {
  zone_id: string;
  start: string;
  end: string;
}

export interface ScheduleView {
  kind: CycleKind;
  start: string;
  end: string;
  zones: ZoneWindowView[];
}

export interface TodayView {
  irrigation_day: string;
  cycles: ScheduleView[];
}

export interface PlanView {
  pump_entity_id: string;
  morning_enabled: boolean;
  morning_start: string;
  evening_start: string;
  manual_timeout_s: number;
  zones: PlanZoneView[];
  today: TodayView;
}

export interface ZoneRunView {
  zone_id: string;
  name: string;
  valve_entity_id: string;
  status: ZoneRunStatus;
  planned_start: string;
  planned_end: string;
  duration_s: number;
  base_s: number;
  carried_s: number;
  rain_credit_s: number;
  actual_start: string | null;
  actual_end: string | null;
  effective_s: number;
}

export interface RunView {
  cycle_id: string;
  kind: CycleKind;
  status: CycleStatus;
  manual: boolean;
  late_rerun: boolean;
  recovery: string | null;
  configured_start: string;
  scheduled_start: string;
  pump_entity_id: string;
  rain_total_mm: number | null;
  live_zone_id: string | null;
  zones: ZoneRunView[];
}

export interface RunsView {
  current: RunView | null;
  last: RunView | null;
}

export interface LedgerZoneView {
  deficit_s: number;
  rain_baseline_mm: number | null;
}

export interface LedgerView {
  settled_cycle_id: string | null;
  day_credit: string | null;
  rain_source: string | null;
  zones: Record<string, LedgerZoneView>;
}

export interface HistoryRow {
  irrigation_day: string;
  kind: CycleKind;
  outcome: Outcome;
  cycle_id: string;
  status: CycleStatus;
  manual: boolean;
  waived_by: string | null;
  recovery: string | null;
  late_rerun: boolean;
  runs: number;
  ended_at: string | null;
  rain_total_mm: number | null;
  planned_s: number;
  carried_s: number;
  effective_s: number;
  rain_credit_s: number;
}

export interface HealthView {
  open: Array<Record<string, unknown>>;
  last: Record<string, unknown> | null;
}

/** The whole document: handshake, then the six sections. */
export interface StateView {
  schema_version: number;
  version: string;
  generated_at: string;
  controller: ControllerView;
  plan: PlanView;
  runs: RunsView;
  ledger: LedgerView;
  history: HistoryRow[];
  health: HealthView;
}
