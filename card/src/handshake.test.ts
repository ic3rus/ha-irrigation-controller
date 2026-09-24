import { describe, expect, it } from "vitest";

import { CARD_VERSION } from "./editor";
import { isStale, STATE_SCHEMA_VERSION, staleHint } from "./handshake";

// Never hardcode the card's version: a release bump must not break these.
// "999.0.0" is an integration newer than the card, "0.0.0" an older one.
const NEWER = "999.0.0";
const OLDER = "0.0.0";
const RELOAD = "Reload the page — in the companion app, reset the frontend cache.";
const RESTART = "Restart Home Assistant to finish the update.";
const SCHEMA = STATE_SCHEMA_VERSION;

describe("isStale", () => {
  it("is false when both version and schema match the bundle", () => {
    expect(isStale({ schema_version: SCHEMA, version: CARD_VERSION })).toBe(false);
  });

  it("is true when the integration version differs", () => {
    expect(isStale({ schema_version: SCHEMA, version: NEWER })).toBe(true);
    expect(isStale({ schema_version: SCHEMA, version: OLDER })).toBe(true);
  });

  it("is true when the schema differs", () => {
    expect(isStale({ schema_version: SCHEMA + 1, version: CARD_VERSION })).toBe(true);
  });

  it("is true when both differ", () => {
    expect(isStale({ schema_version: SCHEMA + 1, version: NEWER })).toBe(true);
  });
});

describe("staleHint", () => {
  it("tells an older card to reload, naming both versions", () => {
    expect(staleHint({ schema_version: SCHEMA, version: NEWER })).toBe(
      `This card (${CARD_VERSION}) does not match the installed integration (${NEWER}). ${RELOAD}`,
    );
  });

  it("tells a newer card to restart Home Assistant", () => {
    expect(staleHint({ schema_version: SCHEMA, version: OLDER })).toBe(
      `This card (${CARD_VERSION}) does not match the installed integration (${OLDER}). ${RESTART}`,
    );
  });

  it("names both schemas and reloads when only the integration's schema is higher", () => {
    expect(staleHint({ schema_version: SCHEMA + 1, version: CARD_VERSION })).toBe(
      `This card (${CARD_VERSION}, schema ${SCHEMA}) does not match the installed integration ` +
        `(${CARD_VERSION}, schema ${SCHEMA + 1}). ${RELOAD}`,
    );
  });

  it("names both schemas and restarts when only the card's schema is higher", () => {
    expect(staleHint({ schema_version: SCHEMA - 1, version: CARD_VERSION })).toBe(
      `This card (${CARD_VERSION}, schema ${SCHEMA}) does not match the installed integration ` +
        `(${CARD_VERSION}, schema ${SCHEMA - 1}). ${RESTART}`,
    );
  });

  it("falls back to the schema when a version does not parse", () => {
    expect(staleHint({ schema_version: SCHEMA - 1, version: "dev" })).toBe(
      `This card (${CARD_VERSION}, schema ${SCHEMA}) does not match the installed integration ` +
        `(dev, schema ${SCHEMA - 1}). ${RESTART}`,
    );
    expect(staleHint({ schema_version: SCHEMA, version: "dev" })).toBe(
      `This card (${CARD_VERSION}) does not match the installed integration (dev). ${RELOAD}`,
    );
  });

  it("names versions and schemas when both differ; the version decides the fix", () => {
    expect(staleHint({ schema_version: SCHEMA + 1, version: NEWER })).toBe(
      `This card (${CARD_VERSION}, schema ${SCHEMA}) does not match the installed integration ` +
        `(${NEWER}, schema ${SCHEMA + 1}). ${RELOAD}`,
    );
    expect(staleHint({ schema_version: SCHEMA + 1, version: OLDER })).toBe(
      `This card (${CARD_VERSION}, schema ${SCHEMA}) does not match the installed integration ` +
        `(${OLDER}, schema ${SCHEMA + 1}). ${RESTART}`,
    );
  });
});
