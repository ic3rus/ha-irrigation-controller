import { describe, expect, it } from "vitest";

import { EVENING, eveningRun, MORNING, stateView, TIME_ZONE, zoneRun } from "./fixtures.test-helpers";
import { buildTimeline, isTodaysRun, localDate } from "./timeline";

describe("buildTimeline", () => {
  it("draws both cycles from the plan, in plan order, when no run exists", () => {
    const { rows, irrigationDay } = buildTimeline(stateView(), TIME_ZONE);

    expect(irrigationDay).toBe("2026-09-23");
    expect(rows.map((row) => row.kind)).toEqual(["morning", "evening"]);
    expect(rows.every((row) => row.source === "plan")).toBe(true);
  });

  it("matches the spec's golden row: proportional fractions on the cycle's own axis", () => {
    const [, evening] = buildTimeline(stateView(), TIME_ZONE).rows;

    expect(evening).toMatchObject({
      kind: "evening",
      start: "2026-09-23T18:00:00+00:00",
      end: "2026-09-23T18:35:00+00:00",
      source: "plan",
    });
    expect(evening?.segments).toHaveLength(2);
    const [lawn, beds] = evening?.segments ?? [];
    expect(lawn).toMatchObject({
      zoneId: "z1",
      name: "Lawn",
      start: "2026-09-23T18:00:00+00:00",
      end: "2026-09-23T18:20:00+00:00",
      x0: 0,
    });
    expect(lawn?.x1).toBeCloseTo(0.571, 3);
    expect(beds).toMatchObject({ zoneId: "z2", name: "Beds", x1: 1 });
    expect(beds?.x0).toBeCloseTo(0.571, 3);
  });

  it("gives each row its OWN axis: the morning row spans 0..1 too", () => {
    const [morning] = buildTimeline(stateView(), TIME_ZONE).rows;

    expect(morning?.segments[0]?.x0).toBe(0);
    expect(morning?.segments[0]?.x1).toBeCloseTo(0.4, 6);
    expect(morning?.segments[1]?.x1).toBe(1);
  });

  it("shows the evening cycle alone when the morning cycle is disabled", () => {
    const { rows } = buildTimeline(stateView({ morningEnabled: false }), TIME_ZONE);

    expect(rows.map((row) => row.kind)).toEqual(["evening"]);
  });

  it("draws a cycle from runs.current when that run is today's cycle (quoted durations)", () => {
    const view = stateView({ current: eveningRun() });
    const [morning, evening] = buildTimeline(view, TIME_ZONE).rows;

    expect(morning?.source).toBe("plan");
    expect(evening).toMatchObject({
      kind: "evening",
      source: "run",
      start: "2026-09-23T18:00:00+00:00",
      // The quoted plan ends 5 minutes early: Beds was credited rain.
      end: "2026-09-23T18:30:00+00:00",
    });
    const [lawn, beds] = evening?.segments ?? [];
    expect(lawn).toMatchObject({ name: "Lawn", x0: 0 });
    expect(lawn?.x1).toBeCloseTo(2 / 3, 6);
    expect(beds).toMatchObject({
      name: "Beds",
      start: "2026-09-23T18:20:00+00:00",
      end: "2026-09-23T18:30:00+00:00",
      x1: 1,
    });
  });

  it("ignores a run of another irrigation day (yesterday's run lingering)", () => {
    const yesterday = eveningRun({
      cycle_id: "2026-09-22-evening",
      configured_start: "2026-09-22T18:00:00+00:00",
      scheduled_start: "2026-09-22T18:00:00+00:00",
    });
    const [, evening] = buildTimeline(stateView({ current: yesterday }), TIME_ZONE).rows;

    expect(evening?.source).toBe("plan");
    expect(evening?.end).toBe("2026-09-23T18:35:00+00:00");
  });

  it("ignores a run of the other kind", () => {
    const morningRun = eveningRun({ kind: "morning", configured_start: "2026-09-23T05:00:00+00:00" });
    const [morning, evening] = buildTimeline(stateView({ current: morningRun }), TIME_ZONE).rows;

    expect(morning?.source).toBe("run");
    expect(evening?.source).toBe("plan");
  });

  it("still recognises today's run after the operator moved the start time (same local day)", () => {
    // The plan now says 20:30 local, the running cycle was configured at 20:00.
    const moved = { ...EVENING, start: "2026-09-23T18:30:00+00:00", end: "2026-09-23T19:05:00+00:00" };
    const view = stateView({ cycles: [MORNING, moved], current: eveningRun() });

    expect(buildTimeline(view, TIME_ZONE).rows[1]?.source).toBe("run");
    // Without a zone to attribute days with, only the exact instant matches.
    expect(buildTimeline(view).rows[1]?.source).toBe("plan");
  });

  it("attributes a late-evening cycle to its LOCAL day, like the engine does", () => {
    // 23:30 Paris on the 23rd is 21:30Z on the 23rd — but 23:30 in a zone
    // far west would be the 24th in UTC. Use a zone where UTC disagrees.
    const view = stateView({
      cycles: [{ ...EVENING, start: "2026-09-24T03:30:00+00:00", end: "2026-09-24T04:00:00+00:00" }],
      current: eveningRun({
        configured_start: "2026-09-24T03:30:00+00:00",
        scheduled_start: "2026-09-24T03:30:00+00:00",
      }),
    });
    // America/Los_Angeles (UTC-7 in September): 03:30Z on the 24th is 20:30 on the 23rd.
    expect(buildTimeline(view, "America/Los_Angeles").rows[0]?.source).toBe("run");
    // In UTC that instant is the 24th, not today's irrigation day.
    expect(buildTimeline(view, "UTC").rows[0]?.source).toBe("plan");
  });

  it("gives a current run whose kind is not planned today a row of its own, in start order", () => {
    // `run_now(morning)` is allowed while the morning cycle is disabled.
    const morningRun = eveningRun({
      cycle_id: "2026-09-23-morning",
      kind: "morning",
      manual: true,
      configured_start: "2026-09-23T05:00:00+00:00",
      scheduled_start: "2026-09-23T09:00:00+00:00",
      zones: [
        zoneRun({
          zone_id: "z1",
          name: "Lawn",
          planned_start: "2026-09-23T09:00:00+00:00",
          planned_end: "2026-09-23T09:10:00+00:00",
        }),
      ],
    });
    const { rows } = buildTimeline(stateView({ morningEnabled: false, current: morningRun }), TIME_ZONE);

    expect(rows.map((row) => [row.kind, row.source])).toEqual([
      ["morning", "run"],
      ["evening", "plan"],
    ]);
    expect(rows[0]).toMatchObject({ start: "2026-09-23T09:00:00+00:00", end: "2026-09-23T09:10:00+00:00" });
    expect(rows[0]?.segments[0]).toMatchObject({ name: "Lawn", x0: 0, x1: 1 });
  });

  it("does not resurrect an unplanned kind's run from another irrigation day", () => {
    const stale = eveningRun({
      kind: "morning",
      configured_start: "2026-09-22T05:00:00+00:00",
      scheduled_start: "2026-09-22T05:00:00+00:00",
    });
    const { rows } = buildTimeline(stateView({ morningEnabled: false, current: stale }), TIME_ZONE);

    expect(rows.map((row) => row.kind)).toEqual(["evening"]);
  });

  it("yields no segments for a cycle without zones", () => {
    const empty = { ...EVENING, end: EVENING.start, zones: [] };
    const [evening] = buildTimeline(stateView({ cycles: [empty] }), TIME_ZONE).rows;

    expect(evening?.segments).toEqual([]);
    expect(evening?.source).toBe("plan");
  });

  it("collapses a zero-length window to x0 === x1 instead of dividing by zero", () => {
    const degenerate = {
      ...EVENING,
      end: EVENING.start,
      zones: [{ zone_id: "z1", start: EVENING.start, end: EVENING.start }],
    };
    const [evening] = buildTimeline(stateView({ cycles: [degenerate] }), TIME_ZONE).rows;

    expect(evening?.segments[0]?.x0).toBe(evening?.segments[0]?.x1);
    expect(Number.isFinite(evening?.segments[0]?.x0)).toBe(true);
  });

  it("clamps a window that leaks outside its cycle into [0, 1]", () => {
    const leaking = {
      ...EVENING,
      zones: [{ zone_id: "z1", start: "2026-09-23T17:50:00+00:00", end: "2026-09-23T19:00:00+00:00" }],
    };
    const [evening] = buildTimeline(stateView({ cycles: [leaking] }), TIME_ZONE).rows;

    expect(evening?.segments[0]).toMatchObject({ x0: 0, x1: 1 });
  });

  it("falls back to the zone id when the plan does not name a zone", () => {
    const unknown = { ...EVENING, zones: [{ zone_id: "ghost", start: EVENING.start, end: EVENING.end }] };
    const [evening] = buildTimeline(stateView({ cycles: [unknown] }), TIME_ZONE).rows;

    expect(evening?.segments[0]?.name).toBe("ghost");
  });

  it("degenerates a run without zones to a point at its scheduled start", () => {
    const bare = eveningRun({ zones: [] });
    const [, evening] = buildTimeline(stateView({ current: bare }), TIME_ZONE).rows;

    expect(evening).toMatchObject({
      source: "run",
      start: bare.scheduled_start,
      end: bare.scheduled_start,
      segments: [],
    });
  });
});

describe("localDate", () => {
  it("returns the calendar date of an instant in a zone", () => {
    expect(localDate("2026-09-23T22:30:00+00:00", "Europe/Paris")).toBe("2026-09-24");
    expect(localDate("2026-09-23T22:30:00+00:00", "UTC")).toBe("2026-09-23");
  });

  it("returns undefined for an unparsable instant or an unknown zone", () => {
    expect(localDate("not a date", "UTC")).toBeUndefined();
    expect(localDate("2026-09-23T22:30:00+00:00", "Mars/Olympus_Mons")).toBeUndefined();
  });
});

describe("isTodaysRun", () => {
  it("requires the same kind", () => {
    expect(isTodaysRun(eveningRun({ kind: "morning" }), EVENING, "2026-09-23", TIME_ZONE)).toBe(false);
  });

  it("falls back to an exact configured-start match when the zone is unusable", () => {
    expect(isTodaysRun(eveningRun(), EVENING, "2026-09-23", "Mars/Olympus_Mons")).toBe(true);
    expect(isTodaysRun(eveningRun({ configured_start: "2026-09-23T18:01:00+00:00" }), EVENING, "2026-09-23")).toBe(false);
  });
});
