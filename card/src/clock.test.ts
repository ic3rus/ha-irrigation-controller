import { describe, expect, it } from "vitest";

import {
  estimateOffset,
  FAST_TICK_MS,
  serverNow,
  SLOW_TICK_MS,
  tickPeriodFor,
  TRANSITION_MS,
} from "./clock";
import { eveningRun, stateView } from "./fixtures.test-helpers";

const RECEIVED_AT = Date.parse("2026-09-23T18:00:00+00:00");

describe("estimateOffset", () => {
  it("is generated_at minus the receipt instant: a server 10 minutes ahead gives +600 000", () => {
    expect(estimateOffset("2026-09-23T18:10:00+00:00", RECEIVED_AT)).toBe(600_000);
  });

  it("is negative when the client clock runs ahead of the server", () => {
    expect(estimateOffset("2026-09-23T17:59:30+00:00", RECEIVED_AT)).toBe(-30_000);
  });

  it("is ~0 on a LAN where the stamp and the receipt agree", () => {
    expect(estimateOffset("2026-09-23T18:00:00.040+00:00", RECEIVED_AT)).toBe(40);
  });

  it("returns undefined for an unparsable stamp so the caller keeps its previous estimate", () => {
    expect(estimateOffset("not an instant", RECEIVED_AT)).toBeUndefined();
    expect(estimateOffset("", RECEIVED_AT)).toBeUndefined();
  });
});

describe("serverNow", () => {
  it("adds the offset to the client clock", () => {
    expect(serverNow(600_000, RECEIVED_AT)).toBe(RECEIVED_AT + 600_000);
    expect(serverNow(-30_000, RECEIVED_AT)).toBe(RECEIVED_AT - 30_000);
  });

  it("trusts the client clock while no offset is known", () => {
    expect(serverNow(undefined, RECEIVED_AT)).toBe(RECEIVED_AT);
  });

  it("reads Date.now() when no client instant is given", () => {
    const before = Date.now();
    const now = serverNow(0);
    expect(now).toBeGreaterThanOrEqual(before);
    expect(now).toBeLessThanOrEqual(Date.now());
  });
});

describe("tickPeriodFor", () => {
  it("ticks every second while the current run is running", () => {
    expect(tickPeriodFor(stateView({ current: eveningRun() }))).toBe(FAST_TICK_MS);
    expect(FAST_TICK_MS).toBe(1000);
  });

  it("ticks every minute otherwise: no run, a pending run, a finished last run, no document", () => {
    expect(tickPeriodFor(stateView())).toBe(SLOW_TICK_MS);
    expect(tickPeriodFor(stateView({ current: eveningRun({ status: "pending" }) }))).toBe(SLOW_TICK_MS);
    expect(tickPeriodFor(stateView({ last: eveningRun({ status: "completed" }) }))).toBe(SLOW_TICK_MS);
    expect(tickPeriodFor(undefined)).toBe(SLOW_TICK_MS);
    expect(SLOW_TICK_MS).toBe(60_000);
  });

  it("keeps the fill transition slightly longer than the fast tick", () => {
    expect(TRANSITION_MS).toBeGreaterThan(FAST_TICK_MS);
    expect(TRANSITION_MS).toBe(1100);
  });
});
