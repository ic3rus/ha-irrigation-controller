import { beforeEach, describe, expect, it } from "vitest";

import {
  HaIrrigationTimelineCard,
  type IrrigationTimelineCardConfig,
} from "./ha-irrigation-timeline-card";

const CARD_TYPE = "ha-irrigation-timeline-card";

function createCard(): HaIrrigationTimelineCard {
  const card = document.createElement(CARD_TYPE);
  document.body.append(card);
  return card;
}

describe("ha-irrigation-timeline-card", () => {
  beforeEach(() => {
    document.body.replaceChildren();
  });

  it("registers the custom element", () => {
    expect(customElements.get(CARD_TYPE)).toBe(HaIrrigationTimelineCard);
  });

  it("declares hass as a reactive property", () => {
    // A plain class field would not re-render on state changes from Lovelace.
    expect(HaIrrigationTimelineCard.elementProperties.has("hass")).toBe(true);
  });

  it("advertises itself exactly once in window.customCards", () => {
    const entries = (window.customCards ?? []).filter(
      (card) => card["type"] === CARD_TYPE,
    );
    expect(entries).toHaveLength(1);
  });

  it("renders nothing until it is configured", async () => {
    const card = createCard();
    await card.updateComplete;
    expect(card.shadowRoot?.querySelector("ha-card")).toBeNull();
  });

  it("renders inside ha-card once configured", async () => {
    const card = createCard();
    card.setConfig({ type: CARD_TYPE, title: "Garden" });
    await card.updateComplete;

    const haCard = card.shadowRoot?.querySelector("ha-card");
    expect(haCard).not.toBeNull();
    expect((haCard as (HTMLElement & { header?: string }) | null)?.header).toBe(
      "Garden",
    );
    expect(card.shadowRoot?.textContent).toContain("timeline card skeleton");
  });

  it("falls back to a default header", async () => {
    const card = createCard();
    card.setConfig({ type: CARD_TYPE });
    await card.updateComplete;

    const haCard = card.shadowRoot?.querySelector("ha-card");
    expect((haCard as (HTMLElement & { header?: string }) | null)?.header).toBe(
      "Irrigation",
    );
  });

  it("throws on an invalid configuration instead of rendering blank", () => {
    const card = createCard();
    const invalid = [null, undefined, "nope", 42];

    for (const value of invalid) {
      expect(() =>
        card.setConfig(value as unknown as IrrigationTimelineCardConfig),
      ).toThrow(/invalid configuration/);
    }
  });

  it("reports a card size", () => {
    expect(createCard().getCardSize()).toBe(2);
  });

  it("survives being evaluated twice (cache-busted ?v= reload)", async () => {
    // The documented dev-loop workaround for card caching is a ?v= query string,
    // which makes the browser evaluate this module a second time. An unguarded
    // customElements.define would throw NotSupportedError here.
    // Held in a variable so tsc does not try to resolve the ?v= specifier.
    const cacheBusted = "./ha-irrigation-timeline-card?v=2";
    await expect(import(/* @vite-ignore */ cacheBusted)).resolves.toBeDefined();

    const entries = (window.customCards ?? []).filter(
      (card) => card["type"] === CARD_TYPE,
    );
    expect(entries).toHaveLength(1);
  });

  it("would collide without the registration guard", () => {
    // Proves the hazard the guard above exists for is real, not theoretical.
    expect(() =>
      customElements.define(CARD_TYPE, class extends HTMLElement {}),
    ).toThrow();
  });
});
