import type { HomeAssistant } from "custom-card-helpers";
import { formatTime } from "custom-card-helpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EVENING, eveningRun, MORNING, stateView, TIME_ZONE } from "./fixtures.test-helpers";
import {
  HaIrrigationTimelineCard,
  type IrrigationTimelineCardConfig,
  LANE_HEIGHT,
  RETRY_DELAY_MS,
} from "./ha-irrigation-timeline-card";
import type { StateView } from "./types";
import { WS_TYPE_STATE_SUBSCRIBE } from "./ws";

const CARD_TYPE = "ha-irrigation-timeline-card";

const LOCALE = { language: "en", number_format: "language", time_format: "24" };

type Listener = () => void;

interface FakeHass {
  hass: HomeAssistant;
  subscribeMessage: ReturnType<typeof vi.fn>;
  unsubscribe: ReturnType<typeof vi.fn>;
  addEventListener: ReturnType<typeof vi.fn>;
  removeEventListener: ReturnType<typeof vi.fn>;
  /** The connection listeners currently registered, by event type. */
  listeners: Map<string, Set<Listener>>;
  /** Deliver one document on the open subscription. */
  push: (view: StateView) => void;
  /** A fresh `hass` object sharing the same connection, as Lovelace hands out. */
  replace: () => HomeAssistant;
  /** `deferred` mode: settle the latest pending `subscribeMessage` by hand. */
  resolveSubscribe: () => void;
  rejectSubscribe: (err: unknown) => void;
  /** Fire a connection lifecycle event (`disconnected`, `ready`). */
  fire: (type: string) => void;
}

interface FakeOptions {
  reject?: { code: string; message: string };
  /** Hold every `subscribeMessage` promise until the test settles it. */
  deferred?: boolean;
  /** `null` leaves `hass.config.time_zone` undefined. */
  timeZone?: string | null;
}

function createHass(options: FakeOptions = {}): FakeHass {
  let callback: ((view: StateView) => void) | undefined;
  let pending: { resolve: () => void; reject: (err: unknown) => void } | undefined;
  const listeners = new Map<string, Set<Listener>>();
  const unsubscribe = vi.fn(async () => undefined);
  const subscribeMessage = vi.fn((cb: (view: StateView) => void) => {
    if (options.deferred) {
      return new Promise((resolve, reject) => {
        pending = {
          resolve: () => {
            callback = cb;
            resolve(unsubscribe);
          },
          reject,
        };
      });
    }
    if (options.reject) {
      return Promise.reject(options.reject);
    }
    callback = cb;
    return Promise.resolve(unsubscribe);
  });
  const addEventListener = vi.fn((type: string, listener: Listener) => {
    const set = listeners.get(type) ?? new Set<Listener>();
    set.add(listener);
    listeners.set(type, set);
  });
  const removeEventListener = vi.fn((type: string, listener: Listener) => {
    listeners.get(type)?.delete(listener);
  });
  const connection = { subscribeMessage, addEventListener, removeEventListener };
  const make = (): HomeAssistant =>
    ({
      connection,
      callWS: vi.fn(),
      locale: LOCALE,
      config: options.timeZone === null ? {} : { time_zone: options.timeZone ?? TIME_ZONE },
      states: {},
    }) as unknown as HomeAssistant;
  return {
    hass: make(),
    subscribeMessage,
    unsubscribe,
    addEventListener,
    removeEventListener,
    listeners,
    push: (view) => {
      if (!callback) {
        throw new Error("no subscription open");
      }
      callback(view);
    },
    replace: make,
    resolveSubscribe: () => pending?.resolve(),
    rejectSubscribe: (err) => pending?.reject(err),
    fire: (type) => {
      for (const listener of [...(listeners.get(type) ?? [])]) {
        listener();
      }
    },
  };
}

function createCard(): HaIrrigationTimelineCard {
  const card = document.createElement(CARD_TYPE);
  document.body.append(card);
  return card;
}

/** Let the pending `subscribeMessage` promise settle and Lit render. */
async function settle(card: HaIrrigationTimelineCard): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await card.updateComplete;
}

async function connectedCard(fake: FakeHass, config: Partial<IrrigationTimelineCardConfig> = {}) {
  const card = createCard();
  card.setConfig({ type: CARD_TYPE, ...config });
  card.hass = fake.hass;
  await settle(card);
  return card;
}

function time(iso: string): string {
  return formatTime(new Date(iso), LOCALE as HomeAssistant["locale"]);
}

function sources(card: HaIrrigationTimelineCard): Array<string | null> {
  return [...(card.shadowRoot?.querySelectorAll("section.cycle") ?? [])].map((row) =>
    row.getAttribute("data-source"),
  );
}

describe("ha-irrigation-timeline-card", () => {
  beforeEach(() => {
    document.body.replaceChildren();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("registers the custom element", () => {
    expect(customElements.get(CARD_TYPE)).toBe(HaIrrigationTimelineCard);
  });

  it("does NOT declare hass as a reactive property (the re-render gate is structural)", () => {
    expect(HaIrrigationTimelineCard.elementProperties.has("hass")).toBe(false);
    const card = createCard();
    const fake = createHass();
    card.hass = fake.hass;
    expect(card.hass).toBe(fake.hass);
  });

  it("advertises itself exactly once in window.customCards", () => {
    const entries = (window.customCards ?? []).filter((card) => card["type"] === CARD_TYPE);
    expect(entries).toHaveLength(1);
  });

  it("renders nothing until it is configured", async () => {
    const card = createCard();
    await card.updateComplete;
    expect(card.shadowRoot?.querySelector("ha-card")).toBeNull();
  });

  it("renders inside ha-card once configured and says it is connecting", async () => {
    const card = createCard();
    card.setConfig({ type: CARD_TYPE, title: "Garden" });
    await card.updateComplete;

    const haCard = card.shadowRoot?.querySelector("ha-card");
    expect(haCard).not.toBeNull();
    expect((haCard as (HTMLElement & { header?: string }) | null)?.header).toBe("Garden");
    expect(card.shadowRoot?.textContent).toContain("Connecting");
  });

  it("falls back to a default header", async () => {
    const card = createCard();
    card.setConfig({ type: CARD_TYPE });
    await card.updateComplete;

    const haCard = card.shadowRoot?.querySelector("ha-card");
    expect((haCard as (HTMLElement & { header?: string }) | null)?.header).toBe("Irrigation");
  });

  it("throws on an invalid configuration instead of rendering blank", () => {
    const card = createCard();
    const invalid = [null, undefined, "nope", 42];

    for (const value of invalid) {
      expect(() => card.setConfig(value as unknown as IrrigationTimelineCardConfig)).toThrow(
        /invalid configuration/,
      );
    }
    expect(() =>
      card.setConfig({ type: CARD_TYPE, entry_id: 7 } as unknown as IrrigationTimelineCardConfig),
    ).toThrow(/entry_id/);
  });

  it("reports a card size and grid options before any document", () => {
    const card = createCard();
    expect(card.getCardSize()).toBe(2);
    expect(card.getGridOptions()).toMatchObject({ columns: 12, min_columns: 6, min_rows: 2 });
    expect(card.getGridOptions().rows).toBeGreaterThanOrEqual(2);
  });

  // ---------------------------------------------------------------- wiring

  it("subscribes over state_subscribe once connected with config and hass, omitting entry_id", async () => {
    const fake = createHass();
    await connectedCard(fake);

    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);
    const message = fake.subscribeMessage.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(message).toEqual({ type: WS_TYPE_STATE_SUBSCRIBE });
    expect("entry_id" in message).toBe(false);
    // Bound to this socket on purpose: the card re-subscribes on `ready` itself.
    expect(fake.subscribeMessage.mock.calls[0]?.[2]).toEqual({ resubscribe: false });
  });

  it("passes the configured entry_id to the subscription", async () => {
    const fake = createHass();
    await connectedCard(fake, { entry_id: "entry-9" });

    expect(fake.subscribeMessage.mock.calls[0]?.[1]).toEqual({
      type: WS_TYPE_STATE_SUBSCRIBE,
      entry_id: "entry-9",
    });
  });

  it("does not subscribe before it has a config, and subscribes only once", async () => {
    const fake = createHass();
    const card = createCard();
    card.hass = fake.hass;
    await settle(card);
    expect(fake.subscribeMessage).not.toHaveBeenCalled();

    card.setConfig({ type: CARD_TYPE });
    card.hass = fake.replace();
    card.hass = fake.replace();
    await settle(card);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);
  });

  it("closes the subscription when removed from the DOM and re-subscribes when re-attached", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    expect(fake.unsubscribe).not.toHaveBeenCalled();

    card.remove();
    expect(fake.unsubscribe).toHaveBeenCalledTimes(1);

    document.body.append(card);
    await settle(card);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(2);
  });

  it("re-subscribes for another controller when entry_id changes", async () => {
    const fake = createHass();
    const card = await connectedCard(fake, { entry_id: "a" });
    fake.push(stateView());
    await card.updateComplete;

    card.setConfig({ type: CARD_TYPE, entry_id: "b" });
    await settle(card);

    expect(fake.unsubscribe).toHaveBeenCalledTimes(1);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(2);
    expect(fake.subscribeMessage.mock.calls[1]?.[1]).toEqual({
      type: WS_TYPE_STATE_SUBSCRIBE,
      entry_id: "b",
    });
    expect(card.shadowRoot?.textContent).toContain("Connecting");
  });

  // -------------------------------------------------------- in-flight races

  it("releases a subscription that resolves after the card was removed, and arms no retry", async () => {
    vi.useFakeTimers();
    const fake = createHass({ deferred: true });
    const card = await connectedCard(fake);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);

    card.remove();
    fake.resolveSubscribe();
    await settle(card);

    expect(fake.unsubscribe).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(RETRY_DELAY_MS * 2);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);
  });

  it("ignores a refusal that lands after the card was removed: no error, no retry", async () => {
    vi.useFakeTimers();
    const fake = createHass({ deferred: true });
    const card = await connectedCard(fake);

    card.remove();
    fake.rejectSubscribe({ code: "not_found", message: "" });
    await settle(card);
    await vi.advanceTimersByTimeAsync(RETRY_DELAY_MS * 2);

    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);
    // Re-attached, it simply subscribes afresh — nothing was remembered.
    document.body.append(card);
    await settle(card);
    expect(card.shadowRoot?.querySelector(".message.error")).toBeNull();
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(2);
  });

  it("re-pointed from the default config to entry_id b while pending: releases the old handle, subscribes for b", async () => {
    const fake = createHass({ deferred: true });
    const card = await connectedCard(fake);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);

    card.setConfig({ type: CARD_TYPE, entry_id: "b" });
    await settle(card);
    // Still one in flight: the second attempt waits for the first to settle.
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);

    fake.resolveSubscribe();
    await settle(card);

    expect(fake.unsubscribe).toHaveBeenCalledTimes(1);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(2);
    expect(fake.subscribeMessage.mock.calls[1]?.[1]).toEqual({
      type: WS_TYPE_STATE_SUBSCRIBE,
      entry_id: "b",
    });
  });

  // -------------------------------------------------------------- reconnect

  it("follows the connection: drops the handle on disconnected and subscribes again on ready", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;
    expect([...fake.listeners.keys()].sort()).toEqual(["disconnected", "ready"]);

    fake.fire("disconnected");
    fake.fire("ready");
    await settle(card);

    // The server side went with the socket: nothing is sent to close it.
    expect(fake.unsubscribe).not.toHaveBeenCalled();
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(2);
    // The stale document stays on screen until the new subscription delivers.
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(2);
  });

  it("shows the refusal of a re-subscribe after reconnect and retries", async () => {
    vi.useFakeTimers();
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;

    fake.subscribeMessage.mockRejectedValueOnce({ code: "not_loaded", message: "" });
    fake.fire("disconnected");
    fake.fire("ready");
    await settle(card);

    expect(card.shadowRoot?.querySelector(".message.error")?.textContent).toContain("loading");
    await vi.advanceTimersByTimeAsync(RETRY_DELAY_MS);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(3);
  });

  it("registers the connection listeners once and removes them when disconnected from the DOM", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    card.hass = fake.replace();
    expect(fake.addEventListener).toHaveBeenCalledTimes(2);

    card.remove();

    expect(fake.removeEventListener).toHaveBeenCalledTimes(2);
    expect([...fake.listeners.values()].every((set) => set.size === 0)).toBe(true);
  });

  // ------------------------------------------------------------- rendering

  it("renders one row per cycle with one rect per zone, proportional, from the first push", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;

    const rows = card.shadowRoot?.querySelectorAll("section.cycle") ?? [];
    expect(rows).toHaveLength(2);
    expect([...rows].map((row) => row.getAttribute("data-kind"))).toEqual(["morning", "evening"]);
    expect([...rows].every((row) => row.getAttribute("data-source") === "plan")).toBe(true);

    const evening = rows[1] as HTMLElement;
    const rects = evening.querySelectorAll("svg rect");
    expect(rects).toHaveLength(2);
    expect(Number(rects[0]?.getAttribute("x"))).toBe(0);
    expect(Number(rects[0]?.getAttribute("width"))).toBeCloseTo(571.43, 1);
    expect(Number(rects[1]?.getAttribute("x"))).toBeCloseTo(571.43, 1);
    expect(Number(rects[1]?.getAttribute("y"))).toBeGreaterThanOrEqual(LANE_HEIGHT);
    const svgEl = evening.querySelector("svg");
    expect(svgEl?.getAttribute("viewBox")).toBe(`0 0 1000 ${2 * LANE_HEIGHT}`);
    expect(svgEl?.getAttribute("preserveAspectRatio")).toBe("none");
  });

  it("labels every segment in plain text with its zone name and planned start–end", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;

    const evening = card.shadowRoot?.querySelectorAll("section.cycle")[1] as HTMLElement;
    const labels = [...evening.querySelectorAll("li.label")].map((li) =>
      li.textContent?.replace(/\s+/g, " ").trim(),
    );
    expect(labels).toHaveLength(2);
    expect(labels[0]).toContain("Lawn");
    expect(labels[0]).toContain(`${time(EVENING.zones[0]!.start)} – ${time(EVENING.zones[0]!.end)}`);
    expect(labels[1]).toContain("Beds");
    expect(labels[1]).toContain(`${time(EVENING.zones[1]!.start)} – ${time(EVENING.zones[1]!.end)}`);

    const header = evening.querySelector(".cycle-header")?.textContent?.replace(/\s+/g, " ");
    expect(header).toContain("Evening");
    expect(header).toContain(`${time(EVENING.start)} – ${time(EVENING.end)}`);
  });

  it("shows the evening row alone when the morning cycle is disabled", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ morningEnabled: false }));
    await card.updateComplete;

    const rows = card.shadowRoot?.querySelectorAll("section.cycle") ?? [];
    expect([...rows].map((row) => row.getAttribute("data-kind"))).toEqual(["evening"]);
  });

  it("draws a cycle from the running cycle's quoted windows once a run exists", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ current: eveningRun() }));
    await card.updateComplete;

    expect(sources(card)).toEqual(["plan", "run"]);
    const rows = card.shadowRoot?.querySelectorAll("section.cycle") ?? [];
    const header = rows[1]?.querySelector(".cycle-header")?.textContent ?? "";
    expect(header).toContain(time("2026-09-23T18:30:00+00:00"));
  });

  it("forwards hass.config.time_zone to the same-day test (a run whose configured start moved)", async () => {
    // Plan says 20:30 local now; the running cycle was configured at 20:00 — same local day.
    const moved = { ...EVENING, start: "2026-09-23T18:30:00+00:00", end: "2026-09-23T19:05:00+00:00" };
    const view = stateView({ cycles: [MORNING, moved], current: eveningRun() });

    const withZone = createHass();
    const card = await connectedCard(withZone);
    withZone.push(view);
    await card.updateComplete;
    expect(sources(card)).toEqual(["plan", "run"]);

    const noZone = createHass({ timeZone: null });
    const bare = await connectedCard(noZone);
    noZone.push(view);
    await bare.updateComplete;
    expect(sources(bare)).toEqual(["plan", "plan"]);
  });

  it("shows an empty-plan hint and no SVG for a cycle without zones", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ cycles: [{ ...EVENING, end: EVENING.start, zones: [] }] }));
    await card.updateComplete;

    const row = card.shadowRoot?.querySelector("section.cycle");
    expect(row?.querySelector("svg")).toBeNull();
    expect(row?.textContent).toContain("No zones");
  });

  it("sizes itself from the rows once a document is in", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    const before = card.getGridOptions().rows;
    fake.push(stateView());
    await card.updateComplete;

    expect(card.getCardSize()).toBeGreaterThan(2);
    // Two cycles of two lanes each: header + 2 × (40 + 2 × LANE_HEIGHT) px = 248 px → 4 rows.
    expect(card.getGridOptions().rows).toBe(Math.ceil((56 + 2 * (40 + 2 * LANE_HEIGHT)) / 64));
    expect(card.getGridOptions().rows).toBeGreaterThan(before);
  });

  // ------------------------------------------------------------------ gate

  it("does not re-render when Lovelace replaces hass with the same document in place", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;

    const render = vi.spyOn(card as unknown as { render: () => unknown }, "render");
    card.hass = fake.replace();
    card.hass = fake.replace();
    await card.updateComplete;

    expect(render).not.toHaveBeenCalled();
    expect(card.hass).not.toBe(fake.hass);
  });

  it("renders exactly once per pushed document", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;

    const render = vi.spyOn(card as unknown as { render: () => unknown }, "render");
    fake.push(stateView({ morningEnabled: false }));
    await card.updateComplete;
    expect(render).toHaveBeenCalledTimes(1);

    // A structurally identical document is still a new reference: one render, no more.
    fake.push(stateView({ morningEnabled: false }));
    await card.updateComplete;
    expect(render).toHaveBeenCalledTimes(2);
  });

  // ---------------------------------------------------------------- errors

  it("says no controller was found on not_found and retries later", async () => {
    vi.useFakeTimers();
    const fake = createHass({ reject: { code: "not_found", message: "No irrigation controller entry to read" } });
    const card = await connectedCard(fake);

    const message = card.shadowRoot?.querySelector(".message.error");
    expect(message?.textContent).toContain("No irrigation controller was found");
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(RETRY_DELAY_MS);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(2);
  });

  it("says the controller is loading on not_loaded", async () => {
    const fake = createHass({ reject: { code: "not_loaded", message: "entry is not loaded" } });
    const card = await connectedCard(fake);

    expect(card.shadowRoot?.querySelector(".message.error")?.textContent).toContain("loading");
  });

  it("names any other refusal and keeps the card readable", async () => {
    const fake = createHass({ reject: { code: "unknown_command", message: "nope" } });
    const card = await connectedCard(fake);

    expect(card.shadowRoot?.querySelector(".message.error")?.textContent).toContain("unknown_command");
  });

  it("clears the error once a document arrives after a retry", async () => {
    vi.useFakeTimers();
    const fake = createHass();
    fake.subscribeMessage.mockRejectedValueOnce({ code: "not_loaded", message: "" });
    const card = await connectedCard(fake);
    expect(card.shadowRoot?.querySelector(".message.error")).not.toBeNull();

    await vi.advanceTimersByTimeAsync(RETRY_DELAY_MS);
    fake.push(stateView());
    await card.updateComplete;

    expect(card.shadowRoot?.querySelector(".message.error")).toBeNull();
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(2);
  });

  it("does not schedule a retry after being removed from the DOM", async () => {
    vi.useFakeTimers();
    const fake = createHass({ reject: { code: "not_found", message: "" } });
    const card = await connectedCard(fake);
    card.remove();

    await vi.advanceTimersByTimeAsync(RETRY_DELAY_MS * 2);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);
  });

  it("stores the handshake from the pushed document", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    expect(card.handshake).toBeUndefined();
    fake.push(stateView());

    expect(card.handshake).toEqual({ schema_version: 1, version: "0.1.0" });
  });

  // ------------------------------------------------------------ evaluation

  it("survives being evaluated twice (cache-busted ?v= reload)", async () => {
    // The documented dev-loop workaround for card caching is a ?v= query string,
    // which makes the browser evaluate this module a second time. An unguarded
    // customElements.define would throw NotSupportedError here.
    // Held in a variable so tsc does not try to resolve the ?v= specifier.
    const cacheBusted = "./ha-irrigation-timeline-card?v=2";
    await expect(import(/* @vite-ignore */ cacheBusted)).resolves.toBeDefined();

    const entries = (window.customCards ?? []).filter((card) => card["type"] === CARD_TYPE);
    expect(entries).toHaveLength(1);
  });

  it("would collide without the registration guard", () => {
    // Proves the hazard the guard above exists for is real, not theoretical.
    expect(() => customElements.define(CARD_TYPE, class extends HTMLElement {})).toThrow();
  });
});
