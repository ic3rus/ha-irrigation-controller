import { describe, expect, it } from "vitest";

import translations from "../../custom_components/ha_irrigation_controller/translations/en.json";
import { anomaly, stateView } from "./fixtures.test-helpers";
import {
  ALERT_ICON,
  ANOMALY_VISUALS,
  buildHealth,
  chipLabel,
  healthSummary,
  itemLabel,
  NOMINAL,
  UNKNOWN_ANOMALY,
} from "./health";
import type { AnomalyKind, HealthItem } from "./health";
import type { StateView } from "./types";

/** `engine/ports.py` `AnomalyKind`, in declaration order. */
const KINDS: AnomalyKind[] = [
  "pump_on_unconfirmed",
  "pump_off_unconfirmed",
  "valve_open_unconfirmed",
  "valve_close_unconfirmed",
  "journal_save_failed",
  "configured_entity_missing",
  "cycle_interrupted",
  "cycle_recovered",
  "missed_cycle",
  "manual_valve_timeout",
];

function withOpen(open: unknown[]): StateView {
  return stateView({ health: { open: open as Array<Record<string, unknown>>, last: null } });
}

/** The Repairs issues the integration declares: one per anomaly kind. */
const ISSUES = translations.issues as Record<string, { title: string }>;

describe("ANOMALY_VISUALS", () => {
  it("maps each of the ten engine kinds to its own glyph and sentence", () => {
    const icons = KINDS.map((kind) => ANOMALY_VISUALS[kind].icon);
    expect(new Set(icons).size).toBe(KINDS.length);
    expect(icons.every((icon) => icon.startsWith("mdi:"))).toBe(true);
    const labels = KINDS.map((kind) => ANOMALY_VISUALS[kind].label);
    expect(new Set(labels).size).toBe(KINDS.length);
  });

  it("covers exactly the kinds en.json declares a Repairs issue for, and reuses each title verbatim", () => {
    // Read from the integration's own translations, so a title edit or a new
    // kind on the backend fails here instead of drifting silently.
    expect(Object.keys(ANOMALY_VISUALS).sort()).toEqual(Object.keys(ISSUES).sort());
    expect(Object.keys(ISSUES).sort()).toEqual([...KINDS].sort());
    for (const [kind, issue] of Object.entries(ISSUES)) {
      expect(ANOMALY_VISUALS[kind as AnomalyKind].label).toBe(issue.title);
    }
  });

  it("pins the glyphs", () => {
    expect(KINDS.map((kind) => ANOMALY_VISUALS[kind].icon)).toEqual([
      "mdi:water-pump-off",
      "mdi:water-pump",
      "mdi:valve-closed",
      "mdi:valve-open",
      "mdi:content-save-alert",
      "mdi:link-off",
      "mdi:power-plug-off",
      "mdi:backup-restore",
      "mdi:alert-circle",
      "mdi:timer-off",
    ]);
  });
});

describe("UNKNOWN_ANOMALY", () => {
  it("is a question mark with its own sentence, distinct from every known glyph", () => {
    expect(UNKNOWN_ANOMALY).toEqual({ icon: "mdi:help-circle", label: "Unknown anomaly" });
    expect(KINDS.map((kind) => ANOMALY_VISUALS[kind].icon)).not.toContain(UNKNOWN_ANOMALY.icon);
  });
});

describe("NOMINAL", () => {
  it("is a hollow check reading All is well", () => {
    expect(NOMINAL).toEqual({ icon: "mdi:check-circle-outline", label: "All is well" });
    expect(KINDS.map((kind) => ANOMALY_VISUALS[kind].icon)).not.toContain(NOMINAL.icon);
  });
});

describe("ALERT_ICON", () => {
  it("is an mdi glyph distinct from the nominal one", () => {
    expect(ALERT_ICON).toMatch(/^mdi:/);
    expect(ALERT_ICON).not.toBe(NOMINAL.icon);
  });
});

describe("buildHealth", () => {
  it("is nominal with nothing open: no items", () => {
    expect(buildHealth(stateView())).toEqual({ state: "nominal", items: [] });
    expect(buildHealth(stateView()).state).toBe("nominal");
  });

  it("resolves a zone anomaly's subject to the zone's name from plan.zones (golden example)", () => {
    const view = withOpen([anomaly("valve_open_unconfirmed", { zone_id: "z1" })]);
    expect(buildHealth(view)).toEqual({
      state: "anomaly",
      items: [{ kind: "valve_open_unconfirmed", visual: ANOMALY_VISUALS.valve_open_unconfirmed, subject: "Lawn" }],
    });
  });

  it("lists a controller-level item without a subject and an entity item with the raw entity_id, in document order", () => {
    const view = withOpen([anomaly("missed_cycle"), anomaly("pump_on_unconfirmed", { entity_id: "switch.pump" })]);
    const model = buildHealth(view);
    expect(model.state).toBe("anomaly");
    expect(model.items.map((item) => [item.kind, item.subject])).toEqual([
      ["missed_cycle", null],
      ["pump_on_unconfirmed", "switch.pump"],
    ]);
    expect(model.items[0]?.visual).toBe(ANOMALY_VISUALS.missed_cycle);
    expect(model.items[1]?.visual).toBe(ANOMALY_VISUALS.pump_on_unconfirmed);
  });

  it("shows the raw zone_id when plan.zones does not know the zone", () => {
    const model = buildHealth(withOpen([anomaly("valve_close_unconfirmed", { zone_id: "zone-9" })]));
    expect(model.items[0]?.subject).toBe("zone-9");
  });

  it("prefers the zone over entity_id, entity_id over role, and shows role raw (synthetic items: the precedence rule is under test, not a backend shape)", () => {
    const model = buildHealth(
      withOpen([
        anomaly("configured_entity_missing", { zone_id: "z2", entity_id: "switch.beds", role: "valve_switch" }),
        anomaly("configured_entity_missing", { entity_id: "switch.pump", role: "pump_switch" }),
        anomaly("configured_entity_missing", { role: "rain_sensor" }),
      ]),
    );
    expect(model.items.map((item) => item.subject)).toEqual(["Beds", "switch.pump", "rain_sensor"]);
  });

  it("counts a kind this bundle does not know as an unknown anomaly carrying the raw kind", () => {
    const model = buildHealth(withOpen([anomaly("solar_flare")]));
    expect(model.state).toBe("anomaly");
    expect(model.items).toEqual([{ kind: "solar_flare", visual: UNKNOWN_ANOMALY, subject: null }]);
    // A kind that names an Object.prototype member is unknown too, not a function.
    const proto = buildHealth(withOpen([anomaly("constructor"), anomaly("toString")]));
    expect(proto.items.map((item) => item.visual)).toEqual([UNKNOWN_ANOMALY, UNKNOWN_ANOMALY]);
  });

  it("drops a malformed item — no string anomaly — and is nominal when nothing remains, never throwing", () => {
    expect(buildHealth(withOpen([{ zone_id: "z1" }]))).toEqual({ state: "nominal", items: [] });
    expect(buildHealth(withOpen([{ anomaly: 42 }, { anomaly: "" }, null, "missed_cycle", 7]))).toEqual({
      state: "nominal",
      items: [],
    });
    const mixed = buildHealth(withOpen([{ zone_id: "z1" }, anomaly("missed_cycle")]));
    expect(mixed.state).toBe("anomaly");
    expect(mixed.items.map((item) => item.kind)).toEqual(["missed_cycle"]);
  });

  it("renders each open item exactly once, duplicates of a kind included", () => {
    const model = buildHealth(
      withOpen([
        anomaly("valve_open_unconfirmed", { zone_id: "z1" }),
        anomaly("valve_open_unconfirmed", { zone_id: "z2" }),
      ]),
    );
    expect(model.items.map((item) => item.subject)).toEqual(["Lawn", "Beds"]);
  });

  it("never reads health.last: a cleared anomaly kept in last is nominal", () => {
    const view = stateView({ health: { open: [], last: { anomaly: "missed_cycle", cycle_id: "x" } } });
    expect(buildHealth(view)).toEqual({ state: "nominal", items: [] });
  });

  it("tolerates a document without a usable health section", () => {
    const noOpen = { ...stateView(), health: { open: "nope", last: null } } as unknown as StateView;
    expect(buildHealth(noOpen)).toEqual({ state: "nominal", items: [] });
    const noHealth = { ...stateView(), health: undefined } as unknown as StateView;
    expect(buildHealth(noHealth)).toEqual({ state: "nominal", items: [] });
  });
});

describe("healthSummary", () => {
  it("says All is well for none, and counts the issues otherwise (golden example)", () => {
    expect(healthSummary(0)).toBe("All is well");
    expect(healthSummary(1)).toBe("1 issue needs attention");
    expect(healthSummary(2)).toBe("2 issues need attention");
    expect(healthSummary(7)).toBe("7 issues need attention");
  });
});

describe("chipLabel", () => {
  it("is the short count for the header chip", () => {
    expect(chipLabel(0)).toBe("All is well");
    expect(chipLabel(1)).toBe("1 issue");
    expect(chipLabel(2)).toBe("2 issues");
  });
});

describe("itemLabel", () => {
  it("is the kind's sentence, with the raw kind appended only for an unknown kind", () => {
    const known: HealthItem = { kind: "missed_cycle", visual: ANOMALY_VISUALS.missed_cycle, subject: null };
    expect(itemLabel(known)).toBe("A scheduled cycle did not run");
    const unknown: HealthItem = { kind: "solar_flare", visual: UNKNOWN_ANOMALY, subject: null };
    expect(itemLabel(unknown)).toBe("Unknown anomaly (solar_flare)");
  });
});
