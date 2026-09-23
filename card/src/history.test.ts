import { describe, expect, it } from "vitest";

import { fullWeek, historyRow, IRRIGATION_DAY, stateView, WEEK } from "./fixtures.test-helpers";
import {
  buildHistory,
  cellKey,
  cellTitle,
  dayLabel,
  HISTORY_DAYS,
  KIND_LABELS,
  NO_RECORD,
  OUTCOME_VISUALS,
  UNKNOWN_OUTCOME,
  weekDays,
} from "./history";
import type { CycleKind, HistoryRow, Outcome } from "./types";

const OUTCOMES: Outcome[] = ["ran", "reduced", "waived", "recovered", "missed", "cancelled"];

describe("HISTORY_DAYS", () => {
  it("is a week", () => {
    expect(HISTORY_DAYS).toBe(7);
  });
});

describe("OUTCOME_VISUALS", () => {
  it("maps each of the six engine outcomes to its own glyph and label", () => {
    expect(Object.keys(OUTCOME_VISUALS).sort()).toEqual([...OUTCOMES].sort());
    const icons = OUTCOMES.map((outcome) => OUTCOME_VISUALS[outcome].icon);
    expect(new Set(icons).size).toBe(OUTCOMES.length);
    expect(icons.every((icon) => icon.startsWith("mdi:"))).toBe(true);
    const labels = OUTCOMES.map((outcome) => OUTCOME_VISUALS[outcome].label);
    expect(new Set(labels).size).toBe(OUTCOMES.length);
  });

  it("flags missed and recovered as the anomalies, nothing else", () => {
    expect(OUTCOME_VISUALS.recovered).toEqual({ icon: "mdi:backup-restore", label: "Recovered", anomaly: true });
    expect(OUTCOME_VISUALS.missed).toEqual({ icon: "mdi:alert-circle", label: "Missed", anomaly: true });
    expect(OUTCOME_VISUALS.ran).toEqual({ icon: "mdi:check-circle", label: "Ran", anomaly: false });
    expect(OUTCOME_VISUALS.reduced).toEqual({ icon: "mdi:weather-rainy", label: "Reduced by rain", anomaly: false });
    expect(OUTCOME_VISUALS.waived).toEqual({ icon: "mdi:hand-back-right", label: "Waived by run-now", anomaly: false });
    // Cancelled is a deliberate act (spec 4.1 Decision 1): its own glyph, not an anomaly.
    expect(OUTCOME_VISUALS.cancelled).toEqual({ icon: "mdi:cancel", label: "Cancelled", anomaly: false });
  });
});

describe("NO_RECORD", () => {
  it("is a hollow glyph, distinct from every outcome's", () => {
    expect(NO_RECORD).toEqual({ icon: "mdi:circle-outline", label: "No record", anomaly: false });
    expect(OUTCOMES.map((outcome) => OUTCOME_VISUALS[outcome].icon)).not.toContain(NO_RECORD.icon);
  });
});

describe("UNKNOWN_OUTCOME", () => {
  it("is a filled question mark, distinct from every known glyph and from the hollow cell", () => {
    expect(UNKNOWN_OUTCOME).toEqual({ icon: "mdi:help-circle", label: "Unknown outcome", anomaly: false });
    expect(OUTCOMES.map((outcome) => OUTCOME_VISUALS[outcome].icon)).not.toContain(UNKNOWN_OUTCOME.icon);
    expect(UNKNOWN_OUTCOME.icon).not.toBe(NO_RECORD.icon);
  });
});

describe("KIND_LABELS", () => {
  it("names both cycle kinds", () => {
    expect(KIND_LABELS).toEqual({ morning: "Morning", evening: "Evening" });
  });
});

describe("cellKey", () => {
  it("joins day and kind", () => {
    expect(cellKey("2026-09-20", "evening")).toBe("2026-09-20|evening");
  });
});

describe("weekDays", () => {
  it("returns the golden week: six days before today, then today, oldest first", () => {
    expect(weekDays("2026-09-23")).toEqual(WEEK);
  });

  it("crosses a month boundary and a leap day by calendar arithmetic", () => {
    expect(weekDays("2026-10-02")).toEqual([
      "2026-09-26",
      "2026-09-27",
      "2026-09-28",
      "2026-09-29",
      "2026-09-30",
      "2026-10-01",
      "2026-10-02",
    ]);
    expect(weekDays("2028-03-02")).toEqual([
      "2028-02-25",
      "2028-02-26",
      "2028-02-27",
      "2028-02-28",
      "2028-02-29",
      "2028-03-01",
      "2028-03-02",
    ]);
  });

  it("is empty — never throws — for a day that is not a calendar date", () => {
    expect(weekDays("garbage")).toEqual([]);
    expect(weekDays("2026-9-23")).toEqual([]);
    expect(weekDays("2026-13-45")).toEqual([]);
    expect(weekDays("2026-02-30")).toEqual([]);
    expect(weekDays("")).toEqual([]);
  });
});

describe("buildHistory", () => {
  it("full week: seven columns ending on today, both kinds, every cell the row's own outcome", () => {
    const model = buildHistory(stateView({ history: fullWeek() }));

    expect(model.days).toEqual(WEEK);
    expect(model.kinds).toEqual(["morning", "evening"]);
    expect(model.cells.size).toBe(14);
    for (const day of WEEK) {
      for (const kind of ["morning", "evening"] as CycleKind[]) {
        expect(model.cells.get(cellKey(day, kind))?.outcome).toBe("ran");
      }
    }
  });

  it("anchors the week on plan.today.irrigation_day, not on the browser's date", () => {
    const model = buildHistory(
      stateView({ irrigationDay: "2026-10-02", history: [historyRow("2026-09-26", "evening", "ran")] }),
    );

    expect(model.days[0]).toBe("2026-09-26");
    expect(model.days[6]).toBe("2026-10-02");
    expect(model.cells.get(cellKey("2026-09-26", "evening"))?.outcome).toBe("ran");
  });

  it("carries the engine's outcome verbatim — anomalies, cancelled and all", () => {
    const rows = [
      historyRow("2026-09-20", "evening", "missed", { status: "missed", runs: 0 }),
      historyRow("2026-09-21", "morning", "recovered", { recovery: "late_rerun", late_rerun: true }),
      historyRow("2026-09-22", "evening", "cancelled", { status: "cancelled" }),
      historyRow("2026-09-19", "evening", "waived", { status: "waived", waived_by: "run_now" }),
      historyRow("2026-09-18", "evening", "reduced", { rain_total_mm: 4.2 }),
    ];
    const model = buildHistory(stateView({ history: rows }));

    expect(model.cells.get("2026-09-20|evening")?.outcome).toBe("missed");
    expect(model.cells.get("2026-09-21|morning")?.outcome).toBe("recovered");
    expect(model.cells.get("2026-09-22|evening")?.outcome).toBe("cancelled");
    expect(model.cells.get("2026-09-19|evening")?.outcome).toBe("waived");
    expect(model.cells.get("2026-09-18|evening")?.outcome).toBe("reduced");
    // The row object itself is kept: the tooltip reads its totals later.
    expect(model.cells.get("2026-09-18|evening")).toBe(rows[4]);
  });

  it("shows only the evening row when the morning cycle is disabled and no morning row is in the window", () => {
    const model = buildHistory(
      stateView({ morningEnabled: false, history: [historyRow("2026-09-22", "evening", "ran")] }),
    );

    expect(model.kinds).toEqual(["evening"]);
  });

  it("shows the morning row when the morning cycle is disabled but an in-window morning row exists", () => {
    const model = buildHistory(
      stateView({ morningEnabled: false, history: [historyRow("2026-09-19", "morning", "ran")] }),
    );

    expect(model.kinds).toEqual(["morning", "evening"]);
    expect(model.cells.size).toBe(1);
    expect(model.cells.get("2026-09-19|morning")?.outcome).toBe("ran");
  });

  it("ignores rows outside the window: eight days ago and tomorrow", () => {
    const model = buildHistory(
      stateView({
        history: [
          historyRow("2026-09-16", "evening", "missed"),
          historyRow("2026-09-24", "evening", "ran"),
          historyRow("2026-09-17", "evening", "ran"),
        ],
      }),
    );

    expect(model.cells.size).toBe(1);
    expect(model.cells.has("2026-09-17|evening")).toBe(true);
  });

  it("does not bring the morning row back for an out-of-window morning row", () => {
    const model = buildHistory(
      stateView({ morningEnabled: false, history: [historyRow("2026-09-16", "morning", "ran")] }),
    );

    expect(model.kinds).toEqual(["evening"]);
  });

  it("leaves today's cycles that have not run yet without a cell", () => {
    const model = buildHistory(
      stateView({ history: [historyRow(IRRIGATION_DAY, "morning", "ran")] }),
    );

    expect(model.cells.has(cellKey(IRRIGATION_DAY, "morning"))).toBe(true);
    expect(model.cells.has(cellKey(IRRIGATION_DAY, "evening"))).toBe(false);
  });

  it("builds a week of hollow cells from an empty history", () => {
    const model = buildHistory(stateView({ history: [] }));

    expect(model.days).toEqual(WEEK);
    expect(model.kinds).toEqual(["morning", "evening"]);
    expect(model.cells.size).toBe(0);
  });

  it("drops a row whose irrigation_day is malformed and keeps the others — never throws", () => {
    const model = buildHistory(
      stateView({
        history: [
          historyRow("garbage", "evening", "ran"),
          historyRow(undefined as unknown as string, "evening", "ran"),
          historyRow("2026-09-22", "evening", "ran"),
        ],
      }),
    );

    expect(model.cells.size).toBe(1);
    expect(model.cells.has("2026-09-22|evening")).toBe(true);
  });

  it("treats a history that is not an array as empty, keeping the days and kinds", () => {
    const model = buildHistory(stateView({ history: null as unknown as HistoryRow[] }));

    expect(model.cells.size).toBe(0);
    expect(model.days).toEqual(WEEK);
    expect(model.kinds).toEqual(["morning", "evening"]);
  });

  it("yields no columns — never throws — when today's irrigation day is unusable", () => {
    const model = buildHistory(
      stateView({ irrigationDay: "garbage", history: [historyRow("2026-09-22", "evening", "ran")] }),
    );

    expect(model.days).toEqual([]);
    expect(model.cells.size).toBe(0);
    expect(model.kinds).toEqual(["morning", "evening"]);
  });
});

describe("dayLabel", () => {
  it("names the weekday, short, in the given language", () => {
    expect(dayLabel("2026-09-23", "en")).toBe("Wed");
    expect(dayLabel("2026-09-21", "en")).toBe("Mon");
    expect(dayLabel("2026-09-21", "fr")).toMatch(/^lun\.?$/);
  });

  it("falls back to the browser's locale without a language, and to that on an unknown tag", () => {
    const expected = new Intl.DateTimeFormat(undefined, { weekday: "short", timeZone: "UTC" }).format(
      new Date("2026-09-21T00:00:00Z"),
    );
    expect(dayLabel("2026-09-21")).toBe(expected);
    expect(dayLabel("2026-09-21", "not a locale !!")).toBe(expected);
  });

  it("returns a malformed day as is rather than throwing", () => {
    expect(dayLabel("garbage", "en")).toBe("garbage");
  });
});

describe("cellTitle", () => {
  it("reads date · kind · outcome · minutes watered · rain for a reduced row", () => {
    const row = historyRow("2026-09-21", "evening", "reduced", { effective_s: 720, rain_total_mm: 4.2 });

    expect(cellTitle("2026-09-21", "evening", row, "en")).toBe(
      "Mon, Sep 21 · Evening · Reduced by rain · 12 min watered · 4.2 mm rain",
    );
  });

  it("omits the values a row does not have", () => {
    const missed = historyRow("2026-09-20", "evening", "missed", { status: "missed", runs: 0 });
    expect(cellTitle("2026-09-20", "evening", missed, "en")).toBe("Sun, Sep 20 · Evening · Missed");

    const dry = historyRow("2026-09-22", "morning", "ran", { effective_s: 1500 });
    expect(cellTitle("2026-09-22", "morning", dry, "en")).toBe("Tue, Sep 22 · Morning · Ran · 25 min watered");

    const zeroRain = historyRow("2026-09-19", "evening", "ran", { effective_s: 1200, rain_total_mm: 0 });
    expect(cellTitle("2026-09-19", "evening", zeroRain, "en")).toBe(
      "Sat, Sep 19 · Evening · Ran · 20 min watered · 0 mm rain",
    );
  });

  it("says No record for a cell without a row", () => {
    expect(cellTitle("2026-09-23", "evening", undefined, "en")).toBe("Wed, Sep 23 · Evening · No record");
  });

  it("names an outcome this bundle does not know as unknown, not as no record", () => {
    const row = historyRow("2026-09-22", "evening", "drizzle" as Outcome);
    expect(cellTitle("2026-09-22", "evening", row, "en")).toBe("Tue, Sep 22 · Evening · Unknown outcome");
  });

  it("never shows 0 min for a watered cycle shorter than half a minute", () => {
    const brief = historyRow("2026-09-22", "evening", "ran", { effective_s: 10 });
    expect(cellTitle("2026-09-22", "evening", brief, "en")).toContain("1 min watered");
  });

  it("formats in the browser's locale without a language", () => {
    const row = historyRow("2026-09-21", "evening", "ran");
    const date = new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    }).format(new Date("2026-09-21T00:00:00Z"));
    expect(cellTitle("2026-09-21", "evening", row)).toBe(`${date} · Evening · Ran`);
  });
});
