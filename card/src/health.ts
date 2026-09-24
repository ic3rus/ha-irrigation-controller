/**
 * The health indicator's logic — pure, DOM-free, hass-free (Story 4.5).
 *
 * `buildHealth` turns one state view document into what the card shows in
 * its header (a chip) and, when anything is open, in its body (a banner):
 * one item per entry of `health.open`, in document order, each carrying
 * the kind's sentence and its subject. `health.open` is the mirror of the
 * open Repairs issues (Story 3.1): the card renders it and mutates
 * nothing — no acknowledgement, no service, no re-derivation. `health.last`
 * is never read: a nominal card must not raise a ghost.
 *
 * The sentences are the Repairs issue titles from `translations/en.json`
 * verbatim, so an admin who reads one in Repairs and on the card
 * recognises them as one issue; the card has no i18n of its own.
 */

import type { StateView } from "./types";

/** `engine/ports.py` `AnomalyKind` — the ten kinds the backend can report. */
export type AnomalyKind =
  | "pump_on_unconfirmed"
  | "pump_off_unconfirmed"
  | "valve_open_unconfirmed"
  | "valve_close_unconfirmed"
  | "journal_save_failed"
  | "configured_entity_missing"
  | "cycle_interrupted"
  | "cycle_recovered"
  | "missed_cycle"
  | "manual_valve_timeout";

export interface AnomalyVisual {
  /** An `mdi:` icon name for `ha-icon`. */
  icon: string;
  /** The kind's sentence: the Repairs issue title, verbatim. */
  label: string;
}

/** One glyph and one sentence per anomaly kind (`issues.<kind>.title` in `en.json`). */
export const ANOMALY_VISUALS: Record<AnomalyKind, AnomalyVisual> = {
  pump_on_unconfirmed: { icon: "mdi:water-pump-off", label: "Pump did not confirm turning on" },
  pump_off_unconfirmed: { icon: "mdi:water-pump", label: "Pump did not confirm turning off" },
  valve_open_unconfirmed: { icon: "mdi:valve-closed", label: "Valve did not confirm opening" },
  valve_close_unconfirmed: { icon: "mdi:valve-open", label: "Valve did not confirm closing" },
  journal_save_failed: { icon: "mdi:content-save-alert", label: "Cycle journal could not be saved" },
  configured_entity_missing: { icon: "mdi:link-off", label: "Configured entity is missing" },
  cycle_interrupted: { icon: "mdi:power-plug-off", label: "A cycle was interrupted" },
  cycle_recovered: { icon: "mdi:backup-restore", label: "A cycle was recovered at startup" },
  missed_cycle: { icon: "mdi:alert-circle", label: "A scheduled cycle did not run" },
  manual_valve_timeout: { icon: "mdi:timer-off", label: "A switch left open by hand was closed" },
};

/**
 * An open item whose kind this bundle does not know (a stale bundle against
 * a newer backend): still counted and listed — the backend says something
 * is wrong — but with a question mark and the raw kind beside it.
 */
export const UNKNOWN_ANOMALY: AnomalyVisual = { icon: "mdi:help-circle", label: "Unknown anomaly" };

/** The quiet state: one check glyph and one calm line in the header. */
export const NOMINAL: AnomalyVisual = { icon: "mdi:check-circle-outline", label: "All is well" };

/** The glyph of the anomaly chip and of the banner's header line. */
export const ALERT_ICON = "mdi:alert";

export interface HealthItem {
  /** The item's `anomaly` verbatim, known kind or not. */
  kind: string;
  visual: AnomalyVisual;
  /** The zone's name (raw `zone_id` when unknown), else `entity_id`, else `role`, else nothing. */
  subject: string | null;
}

export type HealthState = "nominal" | "anomaly";

export interface HealthModel {
  state: HealthState;
  /** Every well-formed open item, in document order. */
  items: HealthItem[];
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined;
}

/**
 * The subject of one open item: the zone's `name` from `plan.zones` for a
 * `zone_id` (the raw id when the plan does not know it — a zone deleted
 * since), else the raw `entity_id`, else the raw `role`, else `null`
 * (a controller-level anomaly such as a missed cycle).
 */
function subjectOf(item: Record<string, unknown>, view: StateView): string | null {
  const zoneId = asString(item["zone_id"]);
  if (zoneId !== undefined) {
    const zones = Array.isArray(view.plan?.zones) ? view.plan.zones : [];
    return zones.find((zone) => zone.zone_id === zoneId)?.name ?? zoneId;
  }
  return asString(item["entity_id"]) ?? asString(item["role"]) ?? null;
}

/**
 * Build the indicator's model from `view`. An entry of `health.open` that
 * is not an object with a string `anomaly` is dropped, never thrown on; a
 * kind this bundle does not know reads as `UNKNOWN_ANOMALY`. The state is
 * `anomaly` when at least one item remains.
 */
export function buildHealth(view: StateView): HealthModel {
  const open = Array.isArray(view.health?.open) ? view.health.open : [];
  const items: HealthItem[] = [];
  for (const entry of open) {
    if (entry === null || typeof entry !== "object") {
      continue;
    }
    const item = entry as Record<string, unknown>;
    const kind = asString(item["anomaly"]);
    if (kind === undefined) {
      continue;
    }
    items.push({
      kind,
      // `Object.hasOwn`, not `in` or a bare index: "constructor" or
      // "toString" would otherwise read a prototype function as a visual.
      visual: Object.hasOwn(ANOMALY_VISUALS, kind) ? ANOMALY_VISUALS[kind as AnomalyKind] : UNKNOWN_ANOMALY,
      subject: subjectOf(item, view),
    });
  }
  return { state: items.length === 0 ? "nominal" : "anomaly", items };
}

/** The banner's title (and the chip's accessible label): "All is well", "1 issue needs attention", "N issues need attention". */
export function healthSummary(count: number): string {
  if (count <= 0) {
    return NOMINAL.label;
  }
  return count === 1 ? "1 issue needs attention" : `${count} issues need attention`;
}

/** The chip's text: "All is well", "1 issue", "N issues". */
export function chipLabel(count: number): string {
  if (count <= 0) {
    return NOMINAL.label;
  }
  return count === 1 ? "1 issue" : `${count} issues`;
}

/**
 * The sentence of one item: its kind's label, with the raw kind appended
 * for a kind this bundle does not know ("Unknown anomaly (solar_flare)"),
 * so the stale bundle still tells the operator what the backend said.
 */
export function itemLabel(item: HealthItem): string {
  return item.visual === UNKNOWN_ANOMALY ? `${UNKNOWN_ANOMALY.label} (${item.kind})` : item.visual.label;
}
