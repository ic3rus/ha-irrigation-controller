import type { HomeAssistant } from "custom-card-helpers";
import { formatTime } from "custom-card-helpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FAST_TICK_MS, SLOW_TICK_MS, TRANSITION_MS } from "./clock";
import {
  anomaly,
  completedEveningRun,
  EVENING,
  eveningRun,
  fullWeek,
  historyRow,
  IRRIGATION_DAY,
  MORNING,
  stateView,
  TIME_ZONE,
  WEEK,
  zoneRun,
} from "./fixtures.test-helpers";
import {
  ANOMALY_HEADER_PX,
  ANOMALY_ROW_PX,
  HaIrrigationTimelineCard,
  HISTORY_HEADER_PX,
  HISTORY_ROW_PX,
  type IrrigationTimelineCardConfig,
  LANE_HEIGHT,
  RETRY_DELAY_MS,
} from "./ha-irrigation-timeline-card";
import * as health from "./health";
import * as history from "./history";
import * as timeline from "./timeline";
import type { CycleKind, HealthView, Outcome, StateView } from "./types";
import { WS_TYPE_STATE_GET, WS_TYPE_STATE_SUBSCRIBE } from "./ws";

// The real geometry, history and health logic, wrapped in spies: the memo
// tests below count the calls the card makes into them (ESM exports cannot
// be spied on after the fact).
vi.mock("./timeline", { spy: true });
vi.mock("./history", { spy: true });
vi.mock("./health", { spy: true });

const CARD_TYPE = "ha-irrigation-timeline-card";

const LOCALE = { language: "en", number_format: "language", time_format: "24" };

type Listener = () => void;

interface FakeHass {
  hass: HomeAssistant;
  subscribeMessage: ReturnType<typeof vi.fn>;
  /** `hass.callWS`, shared by every `hass` object the fake hands out. */
  callWS: ReturnType<typeof vi.fn>;
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
  const callWS = vi.fn();
  const make = (): HomeAssistant =>
    ({
      connection,
      callWS,
      locale: LOCALE,
      config: options.timeZone === null ? {} : { time_zone: options.timeZone ?? TIME_ZONE },
      states: {},
    }) as unknown as HomeAssistant;
  return {
    hass: make(),
    subscribeMessage,
    callWS,
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

function evening(card: HaIrrigationTimelineCard): HTMLElement {
  const row = card.shadowRoot?.querySelector('section.cycle[data-kind="evening"]');
  if (!(row instanceof HTMLElement)) {
    throw new Error("no evening row");
  }
  return row;
}

/** The `scaleX(...)` factor of a progress overlay's inline transform. */
function scaleOf(rect: Element | null | undefined): number {
  const match = /scaleX\(([^)]+)\)/.exec(rect?.getAttribute("style") ?? "");
  if (!match?.[1]) {
    throw new Error(`no scaleX in ${rect?.getAttribute("style") ?? "(no rect)"}`);
  }
  return Number(match[1]);
}

function cursorX(row: HTMLElement): number | undefined {
  const line = row.querySelector("line.cursor");
  return line === null ? undefined : Number(line.getAttribute("x1"));
}

function renderSpy(card: HaIrrigationTimelineCard): ReturnType<typeof vi.spyOn> {
  return vi.spyOn(card as unknown as { render: () => unknown }, "render");
}

function strip(card: HaIrrigationTimelineCard): HTMLElement {
  const section = card.shadowRoot?.querySelector("section.history");
  if (!(section instanceof HTMLElement)) {
    throw new Error("no history strip");
  }
  return section;
}

function cell(card: HaIrrigationTimelineCard, day: string, kind: CycleKind): HTMLElement {
  const found = strip(card).querySelector(`.outcome[data-day="${day}"][data-kind="${kind}"]`);
  if (!(found instanceof HTMLElement)) {
    throw new Error(`no cell for ${day} ${kind}`);
  }
  return found;
}

function outcomesOf(card: HaIrrigationTimelineCard, kind: CycleKind): Array<string | null> {
  return [...strip(card).querySelectorAll(`.outcome[data-kind="${kind}"]`)].map((el) =>
    el.getAttribute("data-outcome"),
  );
}

function iconOf(el: Element): string | null {
  return el.querySelector("ha-icon")?.getAttribute("icon") ?? null;
}

function text(el: Element | null | undefined): string {
  return el?.textContent?.replace(/\s+/g, " ").trim() ?? "";
}

function title(card: HaIrrigationTimelineCard): string {
  return text(card.shadowRoot?.querySelector(".card-header .title"));
}

function chip(card: HaIrrigationTimelineCard): HTMLElement | null {
  const found = card.shadowRoot?.querySelector(".card-header .health");
  return found instanceof HTMLElement ? found : null;
}

function banner(card: HaIrrigationTimelineCard): HTMLElement | null {
  const found = card.shadowRoot?.querySelector(".content .anomalies");
  return found instanceof HTMLElement ? found : null;
}

function open(...items: Array<Record<string, unknown>>): HealthView {
  return { open: items, last: null };
}

function labelOf(card: HaIrrigationTimelineCard, zoneId: string): HTMLElement {
  const index = [...evening(card).querySelectorAll("rect.segment")].findIndex(
    (rect) => rect.getAttribute("data-zone") === zoneId,
  );
  const li = evening(card).querySelectorAll("li.label")[index];
  if (!(li instanceof HTMLElement)) {
    throw new Error(`no label for ${zoneId}`);
  }
  return li;
}

/**
 * Fake timers with the browser clock parked at `iso` (UTC), so
 * `Date.now()` and every interval are under the test's control.
 */
function clockAt(iso: string): void {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(iso));
}

/** Push `view` stamped `generated_at` = now, so the estimated offset is 0. */
function pushNow(fake: FakeHass, options: Parameters<typeof stateView>[0] = {}): void {
  fake.push(stateView({ generatedAt: new Date(Date.now()).toISOString(), ...options }));
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

  it("renders inside ha-card once configured, with its own header, and says it is connecting", async () => {
    const card = createCard();
    card.setConfig({ type: CARD_TYPE, title: "Garden" });
    await card.updateComplete;

    const haCard = card.shadowRoot?.querySelector("ha-card");
    expect(haCard).not.toBeNull();
    // The title is a slotted .card-header (ha-card's own header typography),
    // not ha-card's `header` string, so the chip can sit beside it.
    expect((haCard as (HTMLElement & { header?: string }) | null)?.header).toBeUndefined();
    expect(haCard?.querySelector(":scope > .card-header")).not.toBeNull();
    expect(title(card)).toBe("Garden");
    expect(card.shadowRoot?.textContent).toContain("Connecting");
  });

  it("falls back to a default header", async () => {
    const card = createCard();
    card.setConfig({ type: CARD_TYPE });
    await card.updateComplete;

    expect(title(card)).toBe("Irrigation");
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
    ).toThrow(/invalid configuration \(entry_id must be a string\)/);
    expect(() =>
      card.setConfig({ type: CARD_TYPE, show_title: 1 } as unknown as IrrigationTimelineCardConfig),
    ).toThrow(/invalid configuration \(show_title must be a boolean\)/);
    expect(() =>
      card.setConfig({ type: CARD_TYPE, show_history: "yes" } as unknown as IrrigationTimelineCardConfig),
    ).toThrow(/invalid configuration \(show_history must be a boolean\)/);
    expect(() =>
      card.setConfig({ type: CARD_TYPE, show_health: "no" } as unknown as IrrigationTimelineCardConfig),
    ).toThrow(/invalid configuration \(show_health must be a boolean\)/);
    // A refused config leaves the card unconfigured: nothing rendered.
    expect(card.shadowRoot?.querySelector("ha-card")).toBeNull();
  });

  // ------------------------------------------------------- editor and picker

  it("exposes a schema-only visual editor: getConfigForm lists every option with a label and a helper", () => {
    const form = HaIrrigationTimelineCard.getConfigForm();
    expect(form.schema.map((item) => item.name)).toEqual([
      "title",
      "entry_id",
      "show_title",
      "show_history",
      "show_health",
    ]);
    expect(form.schema.map((item) => Object.keys(item.selector))).toEqual([
      ["text"],
      ["config_entry"],
      ["boolean"],
      ["boolean"],
      ["boolean"],
    ]);
    expect(form.schema[1]?.selector).toEqual({ config_entry: { integration: "ha_irrigation_controller" } });
    expect(form.schema.slice(2).map((item) => item.default)).toEqual([true, true, true]);
    expect(form.schema.every((item) => item.required === undefined)).toBe(true);
    for (const item of form.schema) {
      expect(form.computeLabel?.(item)).toBeTruthy();
      expect(form.computeHelper?.(item)).toBeTruthy();
    }
    expect(form.computeLabel?.({ name: "unknown", selector: {} })).toBeUndefined();
    // No editor element of its own: HA's hui-form-editor owns ha-form.
    expect("getConfigElement" in HaIrrigationTimelineCard).toBe(false);
  });

  it("validates in the editor with the very messages setConfig throws", () => {
    const card = createCard();
    const form = HaIrrigationTimelineCard.getConfigForm();
    const bad = [
      { type: CARD_TYPE, entry_id: 7 },
      { type: CARD_TYPE, show_title: 1 },
      { type: CARD_TYPE, show_history: 1 },
      { type: CARD_TYPE, show_health: "no" },
    ];
    for (const config of bad) {
      const messageOf = (run: () => void): string => {
        try {
          run();
        } catch (err) {
          return (err as Error).message;
        }
        throw new Error("did not throw");
      };
      const fromEditor = messageOf(() => form.assertConfig?.(config as unknown as IrrigationTimelineCardConfig));
      const fromCard = messageOf(() => card.setConfig(config as unknown as IrrigationTimelineCardConfig));
      expect(fromEditor).toMatch(/invalid configuration \(\w+ must be a (string|boolean)\)/);
      expect(fromCard).toBe(fromEditor);
    }
    expect(() => form.assertConfig?.({ type: CARD_TYPE })).not.toThrow();
  });

  it("getStubConfig is {}: spread over {type}, the card renders the default header, chip, rows and strip and subscribes without entry_id", async () => {
    expect(HaIrrigationTimelineCard.getStubConfig()).toEqual({});
    const fake = createHass();
    const card = createCard();
    card.setConfig({ type: `custom:${CARD_TYPE}`, ...HaIrrigationTimelineCard.getStubConfig() });
    card.hass = fake.hass;
    await settle(card);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);
    expect(fake.subscribeMessage.mock.calls[0]?.[1]).toEqual({ type: WS_TYPE_STATE_SUBSCRIBE });

    fake.push(stateView());
    await card.updateComplete;
    expect(title(card)).toBe("Irrigation");
    expect(chip(card)?.getAttribute("data-state")).toBe("nominal");
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(2);
    expect(strip(card).querySelectorAll(".outcome")).toHaveLength(14);
  });

  it("previews the stub without a controller as the existing no-controller sentence", async () => {
    vi.useFakeTimers();
    const fake = createHass({ reject: { code: "not_found", message: "" } });
    const card = createCard();
    card.setConfig({ type: `custom:${CARD_TYPE}`, ...HaIrrigationTimelineCard.getStubConfig() });
    card.hass = fake.hass;
    await settle(card);

    expect(card.shadowRoot?.querySelector(".message.error")?.textContent).toContain(
      "No irrigation controller was found",
    );
    expect(title(card)).toBe("Irrigation");
    await vi.advanceTimersByTimeAsync(RETRY_DELAY_MS);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(2);
  });

  it("advertises a live preview and its documentation in window.customCards", () => {
    const entry = (window.customCards ?? []).find((card) => card["type"] === CARD_TYPE);
    expect(entry?.["preview"]).toBe(true);
    expect(entry?.["documentationURL"]).toBe("https://github.com/ic3rus/ha-irrigation-controller#dashboard-card");
    expect(entry?.["name"]).toBe("HA Irrigation Timeline Card");
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

  it("treats a cleared Controller (entry_id: \"\") as no controller: nothing on the wire, and the same as an absent key", async () => {
    // The Controller picker writes "" when cleared. Sent as is, the backend
    // would read it as an explicit id and answer not_found forever.
    const fake = createHass();
    const card = await connectedCard(fake, { entry_id: "" });
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);
    expect(fake.subscribeMessage.mock.calls[0]?.[1]).toEqual({ type: WS_TYPE_STATE_SUBSCRIBE });
    expect(fake.subscribeMessage.mock.calls[0]?.[1]).not.toHaveProperty("entry_id");
    fake.push(stateView());
    await card.updateComplete;
    expect(title(card)).toBe("Irrigation");

    // undefined → "" and back: the effective controller is unchanged, so nothing moves.
    card.setConfig({ type: CARD_TYPE });
    await settle(card);
    card.setConfig({ type: CARD_TYPE, entry_id: "" });
    await settle(card);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);
    expect(fake.unsubscribe).not.toHaveBeenCalled();
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(2);

    // "" → "a": another controller, asked for by id.
    card.setConfig({ type: CARD_TYPE, entry_id: "a" });
    await settle(card);
    expect(fake.unsubscribe).toHaveBeenCalledTimes(1);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(2);
    expect(fake.subscribeMessage.mock.calls[1]?.[1]).toEqual({ type: WS_TYPE_STATE_SUBSCRIBE, entry_id: "a" });

    // "a" → "": back to auto-detection, without an entry_id on the wire.
    card.setConfig({ type: CARD_TYPE, entry_id: "" });
    await settle(card);
    expect(fake.unsubscribe).toHaveBeenCalledTimes(2);
    expect(fake.subscribeMessage).toHaveBeenCalledTimes(3);
    expect(fake.subscribeMessage.mock.calls[2]?.[1]).toEqual({ type: WS_TYPE_STATE_SUBSCRIBE });
    expect(fake.subscribeMessage.mock.calls[2]?.[1]).not.toHaveProperty("entry_id");

    // The one-shot refetch on a visible tab goes the same way.
    fake.push(stateView());
    await card.updateComplete;
    fake.callWS.mockResolvedValueOnce(stateView());
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    await settle(card);
    expect(fake.callWS).toHaveBeenCalledTimes(1);
    expect(fake.callWS.mock.calls[0]?.[0]).toEqual({ type: WS_TYPE_STATE_GET });
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

    // Card size in ~50 px units: the header, then per cycle its header line
    // plus its two lanes, then the strip (header + two kind rows) on the
    // same pixel budget getGridOptions uses.
    const stripPx = HISTORY_HEADER_PX + 2 * HISTORY_ROW_PX;
    expect(card.getCardSize()).toBe(1 + 2 * (1 + Math.ceil((2 * LANE_HEIGHT) / 50)) + Math.ceil(stripPx / 50));
    // Two cycles of two lanes each: header + 2 × (40 + 2 × LANE_HEIGHT) px = 248 px,
    // then the strip: 72 px header and two 28 px kind rows = 128 px → 376 px → 6 rows.
    expect(card.getGridOptions().rows).toBe(Math.ceil((56 + 2 * (40 + 2 * LANE_HEIGHT) + stripPx) / 64));
    expect(card.getGridOptions().rows).toBeGreaterThan(before);
  });

  it("sizes the strip by its kind rows: one row less when the morning cycle is disabled", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;
    const both = { size: card.getCardSize(), px: card.getGridOptions().rows };

    fake.push(stateView({ morningEnabled: false }));
    await card.updateComplete;
    expect(card.getCardSize()).toBeLessThan(both.size);
    expect(card.getGridOptions().rows).toBeLessThan(both.px);
    // Evening alone: one cycle of two lanes, then a strip of one kind row.
    const stripPx = HISTORY_HEADER_PX + HISTORY_ROW_PX;
    expect(card.getCardSize()).toBe(1 + (1 + Math.ceil((2 * LANE_HEIGHT) / 50)) + Math.ceil(stripPx / 50));
    // 56 + (40 + 2 × LANE_HEIGHT) + 72 + 28 = 252 px → 4 rows.
    expect(card.getGridOptions().rows).toBe(Math.ceil((56 + 40 + 2 * LANE_HEIGHT + stripPx) / 64));
  });

  // --------------------------------------------------------- history strip

  it("renders the strip under today's rows: seven day columns ending today, one row per kind", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ history: fullWeek() }));
    await card.updateComplete;

    const section = strip(card);
    const title = section.querySelector(".history-header");
    expect(title?.textContent).toContain("Last 7 days");
    // Named by its visible title, not by a hand-copied label.
    expect(title?.id).toBeTruthy();
    expect(section.getAttribute("aria-labelledby")).toBe(title?.id);
    // Under the cycles, not above them.
    const body = card.shadowRoot?.querySelector(".content");
    const order = [...(body?.children ?? [])].map((el) => el.className);
    expect(order.indexOf("history")).toBeGreaterThan(order.lastIndexOf("cycle"));

    const days = [...section.querySelectorAll(".history-day")];
    expect(days.map((el) => el.getAttribute("data-day"))).toEqual(WEEK);
    expect(days.map((el) => el.textContent?.trim())).toEqual(["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"]);
    expect(days.map((el) => el.hasAttribute("data-today"))).toEqual([false, false, false, false, false, false, true]);

    const kinds = [...section.querySelectorAll(".history-kind")].map((el) => el.textContent?.trim());
    expect(kinds).toEqual(["Morning", "Evening"]);
    expect(section.querySelectorAll(".outcome")).toHaveLength(14);
    expect(outcomesOf(card, "morning")).toEqual(Array(7).fill("ran"));
    expect(outcomesOf(card, "evening")).toEqual(Array(7).fill("ran"));
    expect([...section.querySelectorAll(".outcome")].every((el) => el.getAttribute("data-anomaly") === "false")).toBe(
      true,
    );
    expect(iconOf(cell(card, "2026-09-23", "evening"))).toBe("mdi:check-circle");
  });

  it("carries each row's outcome verbatim, and flags missed and recovered as the anomalies", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(
      stateView({
        history: [
          historyRow("2026-09-20", "evening", "missed", { status: "missed", runs: 0 }),
          historyRow("2026-09-21", "morning", "recovered", { recovery: "late_rerun", late_rerun: true }),
          historyRow("2026-09-22", "evening", "cancelled", { status: "cancelled" }),
          historyRow("2026-09-19", "evening", "waived", { status: "waived", waived_by: "run_now" }),
          historyRow("2026-09-18", "evening", "reduced", { rain_total_mm: 4.2 }),
          historyRow("2026-09-17", "evening", "ran"),
        ],
      }),
    );
    await card.updateComplete;

    const flagged = [...strip(card).querySelectorAll('.outcome[data-anomaly="true"]')].map((el) =>
      [el.getAttribute("data-day"), el.getAttribute("data-kind"), el.getAttribute("data-outcome")].join(" "),
    );
    expect(flagged.sort()).toEqual(["2026-09-20 evening missed", "2026-09-21 morning recovered"]);
    expect(iconOf(cell(card, "2026-09-20", "evening"))).toBe("mdi:alert-circle");
    expect(iconOf(cell(card, "2026-09-21", "morning"))).toBe("mdi:backup-restore");

    const expectNominal = (day: string, kind: CycleKind, outcome: Outcome, icon: string): void => {
      const el = cell(card, day, kind);
      expect(el.getAttribute("data-outcome")).toBe(outcome);
      expect(el.getAttribute("data-anomaly")).toBe("false");
      expect(iconOf(el)).toBe(icon);
    };
    expectNominal("2026-09-22", "evening", "cancelled", "mdi:cancel");
    expectNominal("2026-09-19", "evening", "waived", "mdi:hand-back-right");
    expectNominal("2026-09-18", "evening", "reduced", "mdi:weather-rainy");
    expectNominal("2026-09-17", "evening", "ran", "mdi:check-circle");
  });

  it("leaves a hollow no-record cell where no row exists, today's unrun evening included", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ history: [historyRow(IRRIGATION_DAY, "morning", "ran")] }));
    await card.updateComplete;

    const today = cell(card, IRRIGATION_DAY, "evening");
    expect(today.getAttribute("data-outcome")).toBe("none");
    expect(today.getAttribute("data-anomaly")).toBe("false");
    expect(iconOf(today)).toBe("mdi:circle-outline");
    expect(today.getAttribute("aria-label")).toBe("Wed, Sep 23 · Evening · No record");
    expect(cell(card, IRRIGATION_DAY, "morning").getAttribute("data-outcome")).toBe("ran");
    expect(outcomesOf(card, "evening")).toEqual(Array(7).fill("none"));
  });

  it("gives every cell a tooltip and an accessible label built from the row", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(
      stateView({
        history: [historyRow("2026-09-21", "evening", "reduced", { effective_s: 720, rain_total_mm: 4.2 })],
      }),
    );
    await card.updateComplete;

    const el = cell(card, "2026-09-21", "evening");
    const expected = "Mon, Sep 21 · Evening · Reduced by rain · 12 min watered · 4.2 mm rain";
    expect(el.getAttribute("title")).toBe(expected);
    expect(el.getAttribute("aria-label")).toBe(expected);
    expect(el.getAttribute("role")).toBe("img");
  });

  it("shows the evening row alone when the morning cycle is disabled, both when an old morning row exists", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ morningEnabled: false, history: [historyRow("2026-09-22", "evening", "ran")] }));
    await card.updateComplete;
    expect([...strip(card).querySelectorAll(".history-kind")].map((el) => el.textContent?.trim())).toEqual([
      "Evening",
    ]);
    expect(strip(card).querySelectorAll(".outcome")).toHaveLength(7);

    fake.push(stateView({ morningEnabled: false, history: [historyRow("2026-09-19", "morning", "ran")] }));
    await card.updateComplete;
    expect([...strip(card).querySelectorAll(".history-kind")].map((el) => el.textContent?.trim())).toEqual([
      "Morning",
      "Evening",
    ]);
    expect(outcomesOf(card, "morning")).toEqual(["none", "none", "ran", "none", "none", "none", "none"]);
    // Today's timeline still shows the evening only: the strip has its own rows.
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(1);
  });

  it("renders the strip with all hollow cells for an empty history, and with no cycle planned today", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ history: [] }));
    await card.updateComplete;
    expect(strip(card).querySelectorAll(".outcome")).toHaveLength(14);
    expect(strip(card).querySelectorAll('.outcome[data-outcome="none"]')).toHaveLength(14);

    fake.push(stateView({ cycles: [], history: [historyRow("2026-09-22", "evening", "ran")] }));
    await card.updateComplete;
    expect(card.shadowRoot?.textContent).toContain("No cycle is planned today.");
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(0);
    expect(cell(card, "2026-09-22", "evening").getAttribute("data-outcome")).toBe("ran");
  });

  it("ignores out-of-window rows and a row with a malformed day without throwing", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(
      stateView({
        history: [
          historyRow("2026-09-16", "evening", "missed"),
          historyRow("2026-09-24", "evening", "missed"),
          historyRow("garbage", "evening", "missed"),
          historyRow("2026-09-17", "evening", "ran"),
        ],
      }),
    );
    await card.updateComplete;

    expect(strip(card).querySelectorAll('.outcome[data-anomaly="true"]')).toHaveLength(0);
    expect(outcomesOf(card, "evening")).toEqual(["ran", "none", "none", "none", "none", "none", "none"]);
  });

  it("draws an outcome this bundle does not know as an unknown record, carrying the raw value", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ history: [historyRow("2026-09-22", "evening", "drizzle" as Outcome)] }));
    await card.updateComplete;

    const el = cell(card, "2026-09-22", "evening");
    expect(el.getAttribute("data-outcome")).toBe("drizzle");
    expect(el.getAttribute("data-anomaly")).toBe("false");
    expect(iconOf(el)).toBe("mdi:help-circle");
    expect(el.getAttribute("title")).toContain("Unknown outcome");
  });

  it("skips the strip — and still draws today's rows — when the irrigation day is unusable", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    expect(() => {
      fake.push(stateView({ irrigationDay: "garbage", history: [historyRow("2026-09-22", "evening", "ran")] }));
    }).not.toThrow();
    await card.updateComplete;

    expect(card.shadowRoot?.querySelector("section.history")).toBeNull();
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(2);
    expect(card.shadowRoot?.querySelector(".message.error")).toBeNull();
  });

  it("labels the days in hass.locale.language, and in the browser's locale without one", async () => {
    const french = createHass();
    french.hass = { ...french.hass, locale: { ...LOCALE, language: "fr" } } as HomeAssistant;
    const card = await connectedCard(french);
    french.push(stateView());
    await card.updateComplete;
    expect(strip(card).querySelector(".history-day")?.textContent?.trim()).toMatch(/^jeu\.?$/);

    const bare = createHass();
    bare.hass = { ...bare.hass, locale: undefined } as unknown as HomeAssistant;
    const plain = await connectedCard(bare);
    bare.push(stateView());
    await plain.updateComplete;
    const expected = new Intl.DateTimeFormat(undefined, { weekday: "short", timeZone: "UTC" }).format(
      new Date("2026-09-17T00:00:00Z"),
    );
    expect(strip(plain).querySelector(".history-day")?.textContent?.trim()).toBe(expected);
  });

  it("renders once per push and reuses the history model across sixty idle ticks", async () => {
    clockAt("2026-09-23T12:00:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    const build = vi.mocked(history.buildHistory);
    build.mockClear();
    const render = renderSpy(card);
    pushNow(fake, { history: fullWeek() });
    await card.updateComplete;
    expect(render).toHaveBeenCalledTimes(1);
    expect(build).toHaveBeenCalledTimes(1);
    const model = build.mock.results[0]?.value as history.HistoryModel;
    expect(model.cells.size).toBe(14);

    await vi.advanceTimersByTimeAsync(SLOW_TICK_MS * 60);
    card.getCardSize();
    card.getGridOptions();
    await card.updateComplete;
    expect(render).toHaveBeenCalledTimes(1);
    expect(build).toHaveBeenCalledTimes(1);

    // The next document rebuilds it: one more call, one more render.
    pushNow(fake, { history: fullWeek() });
    await card.updateComplete;
    expect(render).toHaveBeenCalledTimes(2);
    expect(build).toHaveBeenCalledTimes(2);
    expect(build.mock.results[1]?.value).not.toBe(model);
  });

  // ---------------------------------------------------------------- health

  it("shows a quiet nominal chip in the header and no banner when nothing is open", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;

    const el = chip(card);
    expect(el).not.toBeNull();
    expect(el?.getAttribute("data-state")).toBe("nominal");
    expect(el?.getAttribute("role")).toBe("img");
    expect(el?.getAttribute("aria-label")).toBe("All is well");
    expect(text(el)).toBe("All is well");
    expect(iconOf(el!)).toBe("mdi:check-circle-outline");
    expect(banner(card)).toBeNull();
    // Nothing on the card is in the error colour: no anomaly cell, no failed zone.
    expect(card.shadowRoot?.querySelector('[data-anomaly="true"]')).toBeNull();
    expect(card.shadowRoot?.querySelector('[data-status="failed"]')).toBeNull();
    expect(card.shadowRoot?.querySelector(".message.error")).toBeNull();
  });

  it("lists every open item once, in document order, with its sentence and subject, under a role=status banner", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(
      stateView({
        health: open(anomaly("missed_cycle"), anomaly("pump_on_unconfirmed", { entity_id: "switch.pump" })),
      }),
    );
    await card.updateComplete;

    const el = chip(card);
    expect(el?.getAttribute("data-state")).toBe("anomaly");
    expect(text(el)).toBe("2 issues");
    expect(el?.getAttribute("aria-label")).toBe("2 issues need attention");
    expect(iconOf(el!)).toBe("mdi:alert");

    const section = banner(card);
    expect(section).not.toBeNull();
    expect(section?.getAttribute("role")).toBe("status");
    expect(section?.getAttribute("aria-live")).toBe("polite");
    expect(text(section?.querySelector(".anomalies-header"))).toBe("2 issues need attention");
    // First in the body: above today's rows and the strip.
    const body = card.shadowRoot?.querySelector(".content");
    expect(body?.firstElementChild?.classList.contains("anomalies")).toBe(true);

    const items = [...(section?.querySelectorAll("li[data-anomaly]") ?? [])];
    expect(items.map((li) => li.getAttribute("data-anomaly"))).toEqual(["missed_cycle", "pump_on_unconfirmed"]);
    expect(items.map((li) => text(li))).toEqual([
      "A scheduled cycle did not run",
      "Pump did not confirm turning on · switch.pump",
    ]);
    expect(items.map((li) => iconOf(li))).toEqual(["mdi:alert-circle", "mdi:water-pump-off"]);
    // Display only: no button, nothing to click, no service call.
    expect(section?.querySelector("button, a, ha-button, mwc-button")).toBeNull();
    expect(fake.callWS).not.toHaveBeenCalled();
  });

  it("names a zone anomaly's subject from plan.zones, and shows an unknown zone id raw", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(
      stateView({
        health: open(
          anomaly("valve_open_unconfirmed", { zone_id: "z1" }),
          anomaly("valve_close_unconfirmed", { zone_id: "zone-9" }),
        ),
      }),
    );
    await card.updateComplete;

    const items = [...(banner(card)?.querySelectorAll("li[data-anomaly]") ?? [])];
    expect(items.map((li) => text(li))).toEqual([
      "Valve did not confirm opening · Lawn",
      "Valve did not confirm closing · zone-9",
    ]);
    expect(text(chip(card))).toBe("2 issues");
  });

  it("reads 1 issue, singular, for one open item", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ health: open(anomaly("valve_open_unconfirmed", { zone_id: "z1" })) }));
    await card.updateComplete;

    expect(text(chip(card))).toBe("1 issue");
    expect(chip(card)?.getAttribute("aria-label")).toBe("1 issue needs attention");
    expect(text(banner(card)?.querySelector(".anomalies-header"))).toBe("1 issue needs attention");
    expect(banner(card)?.querySelectorAll("li[data-anomaly]")).toHaveLength(1);
  });

  it("counts a kind this bundle does not know as an unknown anomaly carrying the raw kind, and drops a malformed item", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    expect(() => {
      fake.push(stateView({ health: open(anomaly("solar_flare"), { zone_id: "z1" }) }));
    }).not.toThrow();
    await card.updateComplete;

    expect(text(chip(card))).toBe("1 issue");
    const items = [...(banner(card)?.querySelectorAll("li[data-anomaly]") ?? [])];
    expect(items).toHaveLength(1);
    expect(items[0]?.getAttribute("data-anomaly")).toBe("solar_flare");
    expect(iconOf(items[0]!)).toBe("mdi:help-circle");
    expect(text(items[0])).toBe("Unknown anomaly (solar_flare)");

    // Nothing well-formed left: nominal, no banner.
    fake.push(stateView({ health: open({ zone_id: "z1" }) }));
    await card.updateComplete;
    expect(chip(card)?.getAttribute("data-state")).toBe("nominal");
    expect(banner(card)).toBeNull();
  });

  it("clears the banner and the chip on the next push whose open is empty, never showing health.last", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ health: open(anomaly("missed_cycle")) }));
    await card.updateComplete;
    expect(banner(card)).not.toBeNull();
    expect(chip(card)?.getAttribute("data-state")).toBe("anomaly");

    fake.push(
      stateView({
        health: { open: [], last: { anomaly: "missed_cycle", cycle_id: "2026-09-23-morning", kind: "morning" } },
      }),
    );
    await card.updateComplete;

    expect(banner(card)).toBeNull();
    expect(chip(card)?.getAttribute("data-state")).toBe("nominal");
    expect(text(chip(card))).toBe("All is well");
    expect(card.shadowRoot?.textContent).not.toContain("2026-09-23-morning");
    expect(card.shadowRoot?.textContent).not.toContain("did not run");
    // The card asked nothing of the backend to get there.
    expect(fake.callWS).not.toHaveBeenCalled();
  });

  it("shows the title alone — no chip — while connecting and while an error is on screen", async () => {
    vi.useFakeTimers();
    const fake = createHass();
    fake.subscribeMessage.mockRejectedValueOnce({ code: "not_loaded", message: "" });
    const card = await connectedCard(fake, { title: "Garden" });
    expect(title(card)).toBe("Garden");
    expect(chip(card)).toBeNull();
    expect(card.shadowRoot?.querySelector(".message.error")).not.toBeNull();

    await vi.advanceTimersByTimeAsync(RETRY_DELAY_MS);
    // Subscribed, no document yet: the error stays until one arrives, still no chip.
    expect(card.shadowRoot?.querySelector(".message.error")).not.toBeNull();
    expect(chip(card)).toBeNull();

    // A card that never failed is merely connecting: title alone as well.
    const quiet = await connectedCard(createHass(), { title: "Quiet" });
    expect(quiet.shadowRoot?.textContent).toContain("Connecting");
    expect(title(quiet)).toBe("Quiet");
    expect(chip(quiet)).toBeNull();

    fake.push(stateView({ health: open(anomaly("missed_cycle")) }));
    await card.updateComplete;
    expect(chip(card)?.getAttribute("data-state")).toBe("anomaly");

    // A re-subscribe refused after a reconnect: the body shows the error and
    // the header drops the chip with it, rather than asserting health it
    // cannot refresh.
    fake.subscribeMessage.mockRejectedValueOnce({ code: "not_loaded", message: "" });
    fake.fire("disconnected");
    fake.fire("ready");
    await settle(card);
    expect(card.shadowRoot?.querySelector(".message.error")).not.toBeNull();
    expect(title(card)).toBe("Garden");
    expect(chip(card)).toBeNull();
  });

  it("keeps one always-mounted live announcer in the header that reads the health summary", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    const announcer = card.shadowRoot?.querySelector(".card-header .health-announcer");
    expect(announcer).not.toBeNull();
    expect(announcer?.getAttribute("role")).toBe("status");
    expect(announcer?.getAttribute("aria-live")).toBe("polite");
    expect(announcer?.classList.contains("sr-only")).toBe(true);
    expect(text(announcer)).toBe("");
    expect(chip(card)).toBeNull();

    fake.push(stateView());
    await card.updateComplete;
    expect(card.shadowRoot?.querySelector(".card-header .health-announcer")).toBe(announcer);
    expect(text(announcer)).toBe("All is well");
    expect(banner(card)).toBeNull();

    fake.push(
      stateView({
        health: open(anomaly("missed_cycle"), anomaly("pump_on_unconfirmed", { entity_id: "switch.pump" })),
      }),
    );
    await card.updateComplete;
    expect(card.shadowRoot?.querySelector(".card-header .health-announcer")).toBe(announcer);
    expect(text(announcer)).toBe("2 issues need attention");

    fake.push(stateView());
    await card.updateComplete;
    expect(card.shadowRoot?.querySelector(".card-header .health-announcer")).toBe(announcer);
    expect(text(announcer)).toBe("All is well");
  });

  it("renders once per push and reuses the health model across sixty idle ticks", async () => {
    clockAt("2026-09-23T12:00:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    const build = vi.mocked(health.buildHealth);
    build.mockClear();
    const render = renderSpy(card);
    pushNow(fake, { health: open(anomaly("missed_cycle")) });
    await card.updateComplete;
    expect(render).toHaveBeenCalledTimes(1);
    expect(build).toHaveBeenCalledTimes(1);
    const model = build.mock.results[0]?.value as health.HealthModel;
    expect(model.state).toBe("anomaly");

    await vi.advanceTimersByTimeAsync(SLOW_TICK_MS * 60);
    card.getCardSize();
    card.getGridOptions();
    await card.updateComplete;
    expect(render).toHaveBeenCalledTimes(1);
    expect(build).toHaveBeenCalledTimes(1);

    pushNow(fake);
    await card.updateComplete;
    expect(render).toHaveBeenCalledTimes(2);
    expect(build).toHaveBeenCalledTimes(2);
    expect(build.mock.results[1]?.value).not.toBe(model);
    expect(banner(card)).toBeNull();
  });

  it("grows its sizing hints by the banner's header and one line per open item", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;
    const nominal = { size: card.getCardSize(), rows: card.getGridOptions().rows };

    fake.push(
      stateView({
        health: open(anomaly("missed_cycle"), anomaly("pump_on_unconfirmed", { entity_id: "switch.pump" })),
      }),
    );
    await card.updateComplete;

    const bannerPx = ANOMALY_HEADER_PX + 2 * ANOMALY_ROW_PX;
    const stripPx = HISTORY_HEADER_PX + 2 * HISTORY_ROW_PX;
    expect(card.getCardSize()).toBe(nominal.size + Math.ceil(bannerPx / 50));
    expect(card.getGridOptions().rows).toBe(
      Math.ceil((56 + bannerPx + 2 * (40 + 2 * LANE_HEIGHT) + stripPx) / 64),
    );
    expect(card.getGridOptions().rows).toBeGreaterThan(nominal.rows);

    fake.push(stateView());
    await card.updateComplete;
    expect(card.getCardSize()).toBe(nominal.size);
    expect(card.getGridOptions().rows).toBe(nominal.rows);
  });

  // ---------------------------------------------------------------- options

  it("reads Irrigation for a blank title — empty, whitespace or absent — with the header unchanged", async () => {
    const fake = createHass();
    const card = await connectedCard(fake, { title: "" });
    fake.push(stateView());
    await card.updateComplete;

    expect(title(card)).toBe("Irrigation");
    expect(chip(card)?.getAttribute("data-state")).toBe("nominal");
    const nominal = { size: card.getCardSize(), rows: card.getGridOptions().rows };

    card.setConfig({ type: CARD_TYPE, title: "   " });
    await card.updateComplete;
    expect(title(card)).toBe("Irrigation");

    card.setConfig({ type: CARD_TYPE });
    await card.updateComplete;
    expect(title(card)).toBe("Irrigation");

    card.setConfig({ type: CARD_TYPE, title: "Garden" });
    await card.updateComplete;
    expect(title(card)).toBe("Garden");
    expect(card.getCardSize()).toBe(nominal.size);
    expect(card.getGridOptions().rows).toBe(nominal.rows);
  });

  it("show_title: false drops the heading and keeps the chip and the announcer in the header, same size", async () => {
    const fake = createHass();
    const card = await connectedCard(fake, { show_title: false, title: "Garden" });
    fake.push(stateView());
    await card.updateComplete;

    const header = card.shadowRoot?.querySelector("ha-card > .card-header");
    expect(header).not.toBeNull();
    expect(header?.querySelector("h1, .title")).toBeNull();
    expect(header?.textContent).not.toContain("Garden");
    expect(header?.textContent).not.toContain("Irrigation");
    expect(chip(card)?.getAttribute("data-state")).toBe("nominal");
    expect(header?.querySelector(".health-announcer")?.textContent).toBe("All is well");
    expect(card.shadowRoot?.querySelector("[hidden]")).toBeNull();

    // The header is still there, so the sizing hints do not move.
    const titledFake = createHass();
    const titledCard = await connectedCard(titledFake, { title: "Garden" });
    titledFake.push(stateView());
    await titledCard.updateComplete;
    expect(titledCard.shadowRoot?.querySelector(".card-header h1")).not.toBeNull();
    expect(card.getCardSize()).toBe(titledCard.getCardSize());
    expect(card.getGridOptions()).toEqual(titledCard.getGridOptions());

    // Switched back on: the heading returns with its title.
    card.setConfig({ type: CARD_TYPE, show_title: true, title: "Garden" });
    await card.updateComplete;
    expect(title(card)).toBe("Garden");
  });

  it("show_health: false hides the chip, the banner and the announcer, with two anomalies open, and sizes as the nominal card", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;
    const nominal = { size: card.getCardSize(), rows: card.getGridOptions().rows };

    card.setConfig({ type: CARD_TYPE, show_health: false });
    fake.push(
      stateView({
        health: open(anomaly("missed_cycle"), anomaly("pump_on_unconfirmed", { entity_id: "switch.pump" })),
      }),
    );
    await card.updateComplete;

    expect(title(card)).toBe("Irrigation");
    expect(chip(card)).toBeNull();
    expect(banner(card)).toBeNull();
    expect(card.shadowRoot?.querySelector(".health-announcer")).toBeNull();
    expect(card.shadowRoot?.querySelector("[hidden], [role=status]")).toBeNull();
    expect(card.shadowRoot?.textContent).not.toContain("did not run");
    expect(card.shadowRoot?.textContent).not.toContain("issue");
    // Rows and strip are untouched.
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(2);
    expect(strip(card).querySelectorAll(".outcome")).toHaveLength(14);
    expect(card.getCardSize()).toBe(nominal.size);
    expect(card.getGridOptions().rows).toBe(nominal.rows);

    // Switched back on against the same document: chip and banner return.
    card.setConfig({ type: CARD_TYPE, show_health: true });
    await card.updateComplete;
    expect(chip(card)?.getAttribute("data-state")).toBe("anomaly");
    expect(text(chip(card))).toBe("2 issues");
    expect(banner(card)?.querySelectorAll("li[data-anomaly]")).toHaveLength(2);
    expect(card.getCardSize()).toBe(nominal.size + Math.ceil((ANOMALY_HEADER_PX + 2 * ANOMALY_ROW_PX) / 50));
  });

  it("show_history: false drops the strip, and its pixel budget from both sizing hints", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView({ history: fullWeek() }));
    await card.updateComplete;
    const both = { size: card.getCardSize(), rows: card.getGridOptions().rows };

    card.setConfig({ type: CARD_TYPE, show_history: false });
    await card.updateComplete;

    expect(card.shadowRoot?.querySelector("section.history")).toBeNull();
    expect(card.shadowRoot?.textContent).not.toContain("Last 7 days");
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(2);
    expect(chip(card)).not.toBeNull();
    const stripPx = HISTORY_HEADER_PX + 2 * HISTORY_ROW_PX;
    expect(card.getCardSize()).toBe(both.size - Math.ceil(stripPx / 50));
    expect(card.getCardSize()).toBe(1 + 2 * (1 + Math.ceil((2 * LANE_HEIGHT) / 50)));
    expect(card.getGridOptions().rows).toBe(Math.ceil((56 + 2 * (40 + 2 * LANE_HEIGHT)) / 64));
    expect(card.getGridOptions().rows).toBeLessThan(both.rows);
  });

  it("omits the header altogether with the title and health off, dropping its unit and its 56 px", async () => {
    const fake = createHass();
    const card = await connectedCard(fake);
    fake.push(stateView());
    await card.updateComplete;
    const nominal = { size: card.getCardSize(), rows: card.getGridOptions().rows };

    card.setConfig({ type: CARD_TYPE, show_title: false, show_health: false });
    await card.updateComplete;

    expect(card.shadowRoot?.querySelector(".card-header")).toBeNull();
    expect(card.shadowRoot?.querySelector(".health-announcer")).toBeNull();
    const haCard = card.shadowRoot?.querySelector("ha-card");
    expect([...(haCard?.children ?? [])].map((el) => el.className)).toEqual(["content"]);
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(2);
    expect(strip(card)).not.toBeNull();
    expect(card.getCardSize()).toBe(nominal.size - 1);
    const stripPx = HISTORY_HEADER_PX + 2 * HISTORY_ROW_PX;
    expect(card.getGridOptions().rows).toBe(Math.ceil((2 * (40 + 2 * LANE_HEIGHT) + stripPx) / 64));
    expect(card.getGridOptions().rows).toBe(nominal.rows - 1);
  });

  it("renders no header with the title and health off before any document either: while connecting and on an error", async () => {
    // A header band with neither heading nor chip must not appear while
    // the body says "Connecting", nor above a persistent error message.
    const fake = createHass({ deferred: true });
    const card = await connectedCard(fake, { show_title: false, show_health: false, title: "Garden" });
    expect(card.shadowRoot?.textContent).toContain("Connecting");
    expect(card.shadowRoot?.querySelector(".card-header")).toBeNull();
    expect(card.shadowRoot?.querySelector(".health-announcer")).toBeNull();
    expect(card.getCardSize()).toBe(2);

    fake.rejectSubscribe({ code: "not_found", message: "" });
    await settle(card);
    expect(card.shadowRoot?.querySelector(".message.error")?.textContent).toContain(
      "No irrigation controller was found",
    );
    expect(card.shadowRoot?.querySelector(".card-header")).toBeNull();
    expect([...(card.shadowRoot?.querySelector("ha-card")?.children ?? [])].map((el) => el.className)).toEqual([
      "content",
    ]);
  });

  it("show_title: false alone renders no header until a document arrives: not while connecting, not on an error", async () => {
    // Health is on, but the chip needs a document: until then the header
    // would hold nothing but the announcer, an empty padded band.
    const fake = createHass({ deferred: true });
    const card = await connectedCard(fake, { show_title: false });
    expect(card.shadowRoot?.textContent).toContain("Connecting");
    expect(card.shadowRoot?.querySelector(".card-header")).toBeNull();
    const bothOff = await connectedCard(createHass({ deferred: true }), { show_title: false, show_health: false });

    fake.rejectSubscribe({ code: "not_found", message: "" });
    await settle(card);
    expect(card.shadowRoot?.querySelector(".message.error")).not.toBeNull();
    expect(card.shadowRoot?.querySelector(".card-header")).toBeNull();
    expect(card.getGridOptions().rows).toBe(bothOff.getGridOptions().rows);
    expect(card.getCardSize()).toBe(bothOff.getCardSize());

    // A document: the header comes with the chip and the announcer, no heading.
    card.setConfig({ type: CARD_TYPE, show_title: false, entry_id: "a" });
    await settle(card);
    fake.resolveSubscribe();
    await settle(card);
    fake.push(stateView());
    await card.updateComplete;
    const header = card.shadowRoot?.querySelector(".card-header");
    expect(header).not.toBeNull();
    expect(header?.querySelector("h1")).toBeNull();
    expect(chip(card)?.getAttribute("data-state")).toBe("nominal");
    expect(header?.querySelector(".health-announcer")?.textContent).toBe("All is well");
  });

  it("flips a display option live through setConfig: re-rendered, document kept, subscription untouched", async () => {
    const fake = createHass();
    const card = await connectedCard(fake, { entry_id: "a" });
    fake.push(stateView());
    await card.updateComplete;
    expect(strip(card)).not.toBeNull();
    const render = renderSpy(card);

    card.setConfig({ type: CARD_TYPE, entry_id: "a", show_history: false });
    await settle(card);
    expect(render).toHaveBeenCalledTimes(1);
    expect(card.shadowRoot?.querySelector("section.history")).toBeNull();
    // The document stays: no "Connecting", rows still drawn.
    expect(card.shadowRoot?.querySelectorAll("section.cycle")).toHaveLength(2);
    expect(card.shadowRoot?.textContent).not.toContain("Connecting");

    card.setConfig({ type: CARD_TYPE, entry_id: "a", show_history: true, show_health: false, show_title: false });
    await settle(card);
    expect(render).toHaveBeenCalledTimes(2);
    expect(strip(card)).not.toBeNull();
    expect(card.shadowRoot?.querySelector(".card-header")).toBeNull();

    card.setConfig({ type: CARD_TYPE, entry_id: "a", show_title: true, title: "Garden" });
    await settle(card);
    expect(render).toHaveBeenCalledTimes(3);
    expect(title(card)).toBe("Garden");
    expect(chip(card)).not.toBeNull();

    expect(fake.subscribeMessage).toHaveBeenCalledTimes(1);
    expect(fake.unsubscribe).not.toHaveBeenCalled();
    expect(fake.callWS).not.toHaveBeenCalled();
  });

  // ------------------------------------------------------------ zone labels

  it("marks failed, completed and skipped zone labels by more than colour: a glyph and hidden text", async () => {
    clockAt("2026-09-23T18:40:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    const zones = eveningRun().zones;
    pushNow(fake, {
      current: null,
      last: eveningRun({
        status: "completed",
        live_zone_id: null,
        zones: [
          { ...zones[0]!, status: "failed", effective_s: 0 },
          { ...zones[1]!, status: "completed", actual_start: zones[1]!.planned_start, actual_end: zones[1]!.planned_end, effective_s: 600 },
        ],
      }),
    });
    await card.updateComplete;

    const failed = labelOf(card, "z1");
    expect(failed.getAttribute("data-status")).toBe("failed");
    expect(iconOf(failed)).toBe("mdi:alert-circle");
    expect(failed.querySelector(".sr-only")?.textContent).toBe("failed");
    expect(text(failed.querySelector(".zone"))).toBe("Lawn");
    expect(text(failed)).toContain("Lawn");

    const completed = labelOf(card, "z2");
    expect(completed.getAttribute("data-status")).toBe("completed");
    expect(iconOf(completed)).toBe("mdi:check");
    expect(completed.querySelector(".sr-only")?.textContent).toBe("completed");

    pushNow(fake, {
      current: null,
      last: eveningRun({
        status: "completed",
        live_zone_id: null,
        zones: [
          { ...zones[0]!, status: "skipped" },
          { ...zones[1]!, status: "completed" },
        ],
      }),
    });
    await card.updateComplete;
    const skipped = labelOf(card, "z1");
    expect(skipped.getAttribute("data-status")).toBe("skipped");
    // The strikethrough is the visible cue; the hidden word is the spoken one.
    expect(skipped.querySelector("ha-icon")).toBeNull();
    expect(skipped.querySelector(".sr-only")?.textContent).toBe("skipped");
  });

  it("leaves pending, running and planned labels without a glyph or hidden text", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;

    const running = labelOf(card, "z1");
    expect(running.getAttribute("data-status")).toBe("running");
    expect(running.querySelector("ha-icon")).toBeNull();
    expect(running.querySelector(".sr-only")).toBeNull();
    const pending = labelOf(card, "z2");
    expect(pending.getAttribute("data-status")).toBe("pending");
    expect(pending.querySelector("ha-icon")).toBeNull();
    expect(pending.querySelector(".sr-only")).toBeNull();

    const morning = card.shadowRoot?.querySelector('section.cycle[data-kind="morning"]');
    const planned = [...(morning?.querySelectorAll("li.label") ?? [])];
    expect(planned.every((li) => li.getAttribute("data-status") === "planned")).toBe(true);
    expect(planned.every((li) => li.querySelector("ha-icon") === null && li.querySelector(".sr-only") === null)).toBe(
      true,
    );
  });

  // ------------------------------------------------------------- stylesheet

  /** The card's stylesheet as one string, whatever shape `styles` takes. */
  function stylesheet(): string {
    const styles = HaIrrigationTimelineCard.styles;
    const list = Array.isArray(styles) ? styles : [styles];
    return list.map((entry) => (entry as { cssText?: string }).cssText ?? String(entry)).join("\n");
  }

  it("disables the progress transition under prefers-reduced-motion: reduce", () => {
    const css = stylesheet();
    const media = css.match(/@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*?)\}\s*\}/);
    expect(media).not.toBeNull();
    expect(media?.[1]).toMatch(/\.progress\s*\{[^}]*transition:\s*none/);
    // Outside that media query the fill still glides.
    expect(css).toMatch(new RegExp(`\\.progress\\s*\\{[^}]*transition: transform ${TRANSITION_MS}ms linear`));
  });

  it("carries no light-only literal: every colour fallback holds in a dark theme", () => {
    const css = stylesheet();
    // The cursor and the track used to end in #212121 / rgba(0,0,0,.12), both
    // invisible on a dark card; they now follow currentColor.
    expect(css).not.toMatch(/#212121/i);
    expect(css).not.toMatch(/rgba\(\s*0\s*,\s*0\s*,\s*0/);
    expect(css).toMatch(/\.cursor\s*\{[^}]*stroke: var\(--hic-cursor-color, var\(--primary-text-color, currentColor\)\)/);
    expect(css).toMatch(/color-mix\(in srgb, currentColor 12%, transparent\)/);
    // The card reads its hooks and defines none of them.
    expect(css).not.toMatch(/^\s*--hic-[a-z-]*color[a-z-]*\s*:/m);
    expect(css).not.toMatch(/--hic-health-anomaly-bg\s*:/);
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

  // ------------------------------------------------------------ live clock

  it("fills the running zone and places the cursor from the server-aligned clock, once per second", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;

    const row = evening(card);
    // The golden values: cursor 5/30 of the axis, Lawn a quarter through, Beds empty.
    expect(cursorX(row)).toBeCloseTo(166.67, 1);
    const overlays = row.querySelectorAll("rect.progress");
    expect(overlays).toHaveLength(1);
    expect(overlays[0]?.getAttribute("data-zone")).toBe("z1");
    expect(scaleOf(overlays[0])).toBeCloseTo(0.25, 6);
    expect(row.querySelector('rect.segment[data-zone="z2"]')?.getAttribute("data-status")).toBe("pending");

    const render = renderSpy(card);
    await vi.advanceTimersByTimeAsync(FAST_TICK_MS);
    expect(render).toHaveBeenCalledTimes(1);
    // 18:05:01: the cursor moved 1 s along a 30 min axis, the fill 1 s along 20 min.
    expect(cursorX(row)).toBeCloseTo((301 / 1800) * 1000, 3);
    expect(scaleOf(row.querySelector("rect.progress"))).toBeCloseTo(301 / 1200, 6);

    await vi.advanceTimersByTimeAsync(FAST_TICK_MS * 59);
    expect(render).toHaveBeenCalledTimes(60);
    expect(cursorX(row)).toBeCloseTo(200, 3);
    expect(scaleOf(row.querySelector("rect.progress"))).toBeCloseTo(0.3, 6);
  });

  it("never accumulates ticks: after a 3-minute throttle the fill lands at the wall-clock position", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    const row = evening(card);

    // The tab was throttled: the wall clock moved 3 minutes, no interval fired.
    vi.setSystemTime(new Date("2026-09-23T18:08:00+00:00"));
    expect(cursorX(row)).toBeCloseTo(166.67, 1);

    const render = renderSpy(card);
    await vi.advanceTimersByTimeAsync(FAST_TICK_MS);
    await card.updateComplete;

    expect(render).toHaveBeenCalledTimes(1);
    // One tick, three minutes and one second further: 8:01 / 30:00 of the axis.
    expect(cursorX(row)).toBeCloseTo((481 / 1800) * 1000, 3);
    expect(scaleOf(row.querySelector("rect.progress"))).toBeCloseTo(481 / 1200, 6);
  });

  it("ticks at once when the tab becomes visible", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    const row = evening(card);
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    fake.callWS.mockRejectedValue({ code: "not_loaded", message: "" });

    vi.setSystemTime(new Date("2026-09-23T18:10:00+00:00"));
    document.dispatchEvent(new Event("visibilitychange"));
    await card.updateComplete;

    // The tick lands before (and regardless of) the fetch's answer.
    expect(cursorX(row)).toBeCloseTo(333.33, 1);
    expect(scaleOf(row.querySelector("rect.progress"))).toBeCloseTo(0.5, 6);
    await settle(card);
    // The refused fetch changes nothing: no error, document kept.
    expect(card.shadowRoot?.querySelector(".message.error")).toBeNull();
    expect(cursorX(row)).toBeCloseTo(333.33, 1);

    // Going hidden ticks nothing and fetches nothing.
    const render = renderSpy(card);
    fake.callWS.mockClear();
    visibility.mockReturnValue("hidden");
    vi.setSystemTime(new Date("2026-09-23T18:11:00+00:00"));
    document.dispatchEvent(new Event("visibilitychange"));
    await card.updateComplete;
    expect(render).not.toHaveBeenCalled();
    expect(fake.callWS).not.toHaveBeenCalled();
    expect(cursorX(row)).toBeCloseTo(333.33, 1);
  });

  it("re-estimates a delivery delay booked as offset with a fresh fetch when the tab turns visible", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake, { entry_id: "entry-1" });
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    expect(card.clockOffsetMs).toBe(0);
    const row = evening(card);

    // The tab froze at 18:05; the push stamped 18:05 is processed at 18:07 —
    // two minutes of queueing read as a clock two minutes slow.
    vi.setSystemTime(new Date("2026-09-23T18:07:00+00:00"));
    fake.push(stateView({ current: eveningRun(), generatedAt: "2026-09-23T18:05:00+00:00" }));
    await card.updateComplete;
    expect(card.clockOffsetMs).toBe(-120_000);
    expect(cursorX(row)).toBeCloseTo(166.67, 1);

    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    fake.callWS.mockResolvedValue(stateView({ current: eveningRun(), generatedAt: "2026-09-23T18:07:00+00:00" }));
    document.dispatchEvent(new Event("visibilitychange"));
    await settle(card);

    expect(fake.callWS).toHaveBeenCalledTimes(1);
    expect(fake.callWS).toHaveBeenCalledWith({ type: WS_TYPE_STATE_GET, entry_id: "entry-1" });
    expect(card.clockOffsetMs).toBe(0);
    expect(cursorX(row)).toBeCloseTo((420 / 1800) * 1000, 3);
  });

  it("drops a fetched document that lands after the card was removed or re-pointed", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    let resolve: ((view: StateView) => void) | undefined;
    fake.callWS.mockImplementation(() => new Promise<StateView>((done) => (resolve = done)));

    document.dispatchEvent(new Event("visibilitychange"));
    expect(fake.callWS).toHaveBeenCalledTimes(1);
    card.remove();
    resolve?.(stateView({ current: eveningRun(), generatedAt: "2026-09-23T18:15:00+00:00" }));
    await settle(card);

    expect(card.clockOffsetMs).toBe(0);
    // Without an open subscription there is nothing to fetch for either.
    document.dispatchEvent(new Event("visibilitychange"));
    expect(fake.callWS).toHaveBeenCalledTimes(1);
  });

  it("corrects for clock drift: a generated_at 10 minutes ahead moves the cursor, silently", async () => {
    clockAt("2026-09-23T18:00:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    expect(card.clockOffsetMs).toBe(0);

    fake.push(stateView({ current: eveningRun(), generatedAt: "2026-09-23T18:10:00+00:00" }));
    await card.updateComplete;

    expect(card.clockOffsetMs).toBe(600_000);
    const row = evening(card);
    // The browser says 18:00; the server says 18:10 — the server wins.
    expect(cursorX(row)).toBeCloseTo(333.33, 1);
    expect(scaleOf(row.querySelector("rect.progress"))).toBeCloseTo(0.5, 6);
    expect(card.shadowRoot?.textContent).not.toMatch(/drift|clock/i);

    // An unparsable stamp keeps the previous estimate.
    fake.push(stateView({ current: eveningRun(), generatedAt: "not an instant" }));
    await card.updateComplete;
    expect(card.clockOffsetMs).toBe(600_000);

    // Every push re-estimates: the newest wins.
    fake.push(stateView({ current: eveningRun(), generatedAt: "2026-09-23T18:00:30+00:00" }));
    await card.updateComplete;
    expect(card.clockOffsetMs).toBe(30_000);
  });

  it("ticks every minute without a run, and shows the cursor crossing a plan row", async () => {
    // Inside the morning PLAN window (05:00–05:25), nothing running.
    clockAt("2026-09-23T05:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake);
    await card.updateComplete;

    const morning = card.shadowRoot?.querySelector('section.cycle[data-kind="morning"]') as HTMLElement;
    expect(cursorX(morning)).toBeCloseTo(200, 3);
    expect(morning.querySelector("rect.progress")).toBeNull();
    expect(cursorX(evening(card))).toBeUndefined();

    const render = renderSpy(card);
    await vi.advanceTimersByTimeAsync(SLOW_TICK_MS - 1);
    expect(render).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(render).toHaveBeenCalledTimes(1);
    expect(cursorX(morning)).toBeCloseTo(240, 3);
  });

  it("renders zero times over fifty hass replacements and sixty idle ticks, once on a push", async () => {
    // Midday: outside both plan windows, no run.
    clockAt("2026-09-23T12:00:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake);
    await card.updateComplete;
    expect(card.shadowRoot?.querySelector("line.cursor")).toBeNull();

    const render = renderSpy(card);
    for (let i = 0; i < 50; i += 1) {
      card.hass = fake.replace();
    }
    await vi.advanceTimersByTimeAsync(SLOW_TICK_MS * 60);
    await card.updateComplete;
    expect(render).not.toHaveBeenCalled();

    pushNow(fake, { morningEnabled: false });
    await card.updateComplete;
    expect(render).toHaveBeenCalledTimes(1);
  });

  it("switches the tick period with the document: 60 s idle, 1 s while running, 60 s once the cycle ends", async () => {
    clockAt("2026-09-23T17:50:00+00:00");
    const armed = vi.spyOn(globalThis, "setInterval");
    const delays = (): unknown[] => armed.mock.calls.map((call) => call[1]);
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake);
    await card.updateComplete;
    expect(delays()).toEqual([SLOW_TICK_MS]);
    expect(vi.getTimerCount()).toBe(1);

    // A run starts while the card is up.
    vi.setSystemTime(new Date("2026-09-23T18:05:00+00:00"));
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    expect(delays()).toEqual([SLOW_TICK_MS, FAST_TICK_MS]);
    expect(vi.getTimerCount()).toBe(1);

    // Another push with the same period re-arms nothing.
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    expect(delays()).toEqual([SLOW_TICK_MS, FAST_TICK_MS]);

    // The cycle ends at 18:30; the cursor has left the row.
    vi.setSystemTime(new Date("2026-09-23T18:40:00+00:00"));
    pushNow(fake, { current: null, last: completedEveningRun() });
    await card.updateComplete;
    expect(delays()).toEqual([SLOW_TICK_MS, FAST_TICK_MS, SLOW_TICK_MS]);
    expect(vi.getTimerCount()).toBe(1);

    const render = renderSpy(card);
    await vi.advanceTimersByTimeAsync(SLOW_TICK_MS * 5);
    // The slow ticks move nothing visible.
    expect(render).not.toHaveBeenCalled();
  });

  it("renders the tick that takes the cursor off the end of its row, then no more", async () => {
    // One second before the running row's axis ends.
    clockAt("2026-09-23T18:29:59.500+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: eveningRun({ live_zone_id: "z2", zones: eveningRun().zones.map((zone) =>
      zone.zone_id === "z1" ? { ...zone, status: "completed" } : { ...zone, status: "running" },
    ) }) });
    await card.updateComplete;
    const row = evening(card);
    expect(cursorX(row)).toBeDefined();

    const render = renderSpy(card);
    await vi.advanceTimersByTimeAsync(FAST_TICK_MS);
    expect(render).toHaveBeenCalledTimes(1);
    expect(cursorX(row)).toBeUndefined();
    // The engine has not said "completed" yet: the running zone is drawn full.
    expect(scaleOf(row.querySelector("rect.progress"))).toBe(1);

    await vi.advanceTimersByTimeAsync(FAST_TICK_MS * 10);
    expect(render).toHaveBeenCalledTimes(1);
  });

  it("clears the interval and the visibility listener when removed from the DOM", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    expect(vi.getTimerCount()).toBe(1);
    const removeListener = vi.spyOn(document, "removeEventListener");

    card.remove();

    expect(vi.getTimerCount()).toBe(0);
    expect(fake.unsubscribe).toHaveBeenCalledTimes(1);
    expect(removeListener).toHaveBeenCalledWith("visibilitychange", expect.any(Function));

    // Re-attached five minutes later: the ticker comes back with the document
    // it still holds, and the cursor catches up at once, not at the next tick.
    const before = cursorX(evening(card));
    vi.setSystemTime(new Date("2026-09-23T18:10:00+00:00"));
    document.body.append(card);
    await settle(card);
    expect(vi.getTimerCount()).toBe(1);
    expect(before).toBeCloseTo(166.67, 1);
    expect(cursorX(evening(card))).toBeCloseTo(333.33, 1);
  });

  it("stops the ticker when re-pointed at another controller", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake, { entry_id: "a" });
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    expect(vi.getTimerCount()).toBe(1);

    card.setConfig({ type: CARD_TYPE, entry_id: "b" });
    await settle(card);

    expect(vi.getTimerCount()).toBe(0);
  });

  // -------------------------------------------------------------- statuses

  it("colours segments by their engine status on the push that reports them, overlay on the running one only", async () => {
    clockAt("2026-09-23T18:22:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    const zones = eveningRun().zones;
    pushNow(fake, {
      current: eveningRun({
        live_zone_id: "z2",
        zones: [
          { ...zones[0]!, status: "completed", actual_end: "2026-09-23T18:20:00+00:00", effective_s: 1200 },
          { ...zones[1]!, status: "running", actual_start: "2026-09-23T18:20:01+00:00" },
        ],
      }),
    });
    await card.updateComplete;

    const row = evening(card);
    expect(row.getAttribute("data-status")).toBe("running");
    expect(row.querySelector('rect.segment[data-zone="z1"]')?.getAttribute("data-status")).toBe("completed");
    expect(row.querySelector('rect.segment[data-zone="z2"]')?.getAttribute("data-status")).toBe("running");
    const overlays = row.querySelectorAll("rect.progress");
    expect(overlays).toHaveLength(1);
    expect(overlays[0]?.getAttribute("data-zone")).toBe("z2");
    // Beds runs 18:20–18:30; at 18:22 it is a fifth through.
    expect(scaleOf(overlays[0])).toBeCloseTo(0.2, 6);

    pushNow(fake, {
      current: eveningRun({
        live_zone_id: "z2",
        zones: [
          { ...zones[0]!, status: "failed" },
          { ...zones[1]!, status: "running" },
        ],
      }),
    });
    await card.updateComplete;
    expect(row.querySelector('rect.segment[data-zone="z1"]')?.getAttribute("data-status")).toBe("failed");
    expect(row.querySelectorAll("rect.progress")).toHaveLength(1);
  });

  it("keeps the finished run on screen from runs.last, marked completed, without a cursor", async () => {
    clockAt("2026-09-23T18:40:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: null, last: completedEveningRun() });
    await card.updateComplete;

    const row = evening(card);
    expect(row.getAttribute("data-source")).toBe("run");
    expect(row.getAttribute("data-status")).toBe("completed");
    expect(row.querySelector(".cycle-status")?.textContent).toBe("Completed");
    expect([...row.querySelectorAll("rect.segment")].map((rect) => rect.getAttribute("data-status"))).toEqual([
      "completed",
      "completed",
    ]);
    expect(row.querySelector("rect.progress")).toBeNull();
    expect(cursorX(row)).toBeUndefined();
    // The run's quoted envelope, not the plan's 18:35.
    expect(row.querySelector(".cycle-header")?.textContent).toContain(time("2026-09-23T18:30:00+00:00"));
  });

  it("says Cancelled in the header of a cancelled run, z1 terminal and z2 still pending", async () => {
    clockAt("2026-09-23T18:12:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, {
      current: null,
      last: eveningRun({
        status: "cancelled",
        live_zone_id: null,
        zones: [
          zoneRun({ zone_id: "z1", name: "Lawn", status: "completed", planned_start: "2026-09-23T18:00:00+00:00", planned_end: "2026-09-23T18:20:00+00:00" }),
          zoneRun({ zone_id: "z2", name: "Beds", status: "pending", planned_start: "2026-09-23T18:20:00+00:00", planned_end: "2026-09-23T18:30:00+00:00" }),
        ],
      }),
    });
    await card.updateComplete;

    const row = evening(card);
    expect(row.getAttribute("data-status")).toBe("cancelled");
    expect(row.querySelector(".cycle-status")?.textContent).toBe("Cancelled");
    expect(row.querySelector('rect.segment[data-zone="z1"]')?.getAttribute("data-status")).toBe("completed");
    expect(row.querySelector('rect.segment[data-zone="z2"]')?.getAttribute("data-status")).toBe("pending");
    expect(row.querySelector("rect.progress")).toBeNull();
    // 18:12 is inside the row's axis, but a finished run has no "now".
    expect(cursorX(row)).toBeUndefined();
    const render = renderSpy(card);
    await vi.advanceTimersByTimeAsync(SLOW_TICK_MS * 3);
    expect(render).not.toHaveBeenCalled();
  });

  it("says Interrupted for a run the startup reconciler closed", async () => {
    clockAt("2026-09-23T18:12:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: null, last: eveningRun({ status: "interrupted", live_zone_id: null }) });
    await card.updateComplete;

    const row = evening(card);
    expect(row.getAttribute("data-status")).toBe("interrupted");
    expect(row.querySelector(".cycle-status")?.textContent).toBe("Interrupted");
    expect(cursorX(row)).toBeUndefined();
  });

  it("draws the plan again when runs.last is yesterday's run, with no status word", async () => {
    clockAt("2026-09-23T12:00:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, {
      last: completedEveningRun({
        cycle_id: "2026-09-22-evening",
        configured_start: "2026-09-22T18:00:00+00:00",
        scheduled_start: "2026-09-22T18:00:00+00:00",
      }),
    });
    await card.updateComplete;

    const row = evening(card);
    expect(row.getAttribute("data-source")).toBe("plan");
    expect(row.getAttribute("data-status")).toBe("planned");
    expect(row.querySelector(".cycle-status")).toBeNull();
  });

  it("does not put a status word on a planned or running row", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;

    expect(card.shadowRoot?.querySelectorAll(".cycle-status")).toHaveLength(0);
  });

  it("rebuilds the geometry only when the document changes (memoised across ticks)", async () => {
    clockAt("2026-09-23T18:05:00+00:00");
    const fake = createHass();
    const card = await connectedCard(fake);
    const build = vi.mocked(timeline.buildTimeline);
    build.mockClear();
    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    const afterPush = build.mock.calls.length;
    expect(afterPush).toBe(1);

    await vi.advanceTimersByTimeAsync(FAST_TICK_MS * 5);
    card.getCardSize();
    card.getGridOptions();
    expect(build).toHaveBeenCalledTimes(afterPush);

    // The zone is the memo's other key: a new one rebuilds on the next read.
    card.hass = { ...fake.replace(), config: { time_zone: "UTC" } } as HomeAssistant;
    expect(build).toHaveBeenCalledTimes(afterPush);
    await vi.advanceTimersByTimeAsync(FAST_TICK_MS);
    expect(build).toHaveBeenCalledTimes(afterPush + 1);
    expect(build.mock.calls[afterPush]?.[1]).toBe("UTC");

    pushNow(fake, { current: eveningRun() });
    await card.updateComplete;
    expect(build).toHaveBeenCalledTimes(afterPush + 2);
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
