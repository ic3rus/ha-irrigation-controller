import { describe, expect, it } from "vitest";

import {
  assertCardConfig,
  CARD_TYPE,
  CONFIG_SCHEMA,
  configForm,
  DEFAULT_TITLE,
  entryIdOf,
  headingOf,
  HELPERS,
  INTEGRATION_DOMAIN,
  LABELS,
  showHealth,
  showHistory,
  showTitle,
  stubConfig,
} from "./editor";
import type { CardOptions, IrrigationTimelineCardConfig } from "./editor";

/**
 * The keys of `CardOptions` (= `IrrigationTimelineCardConfig` minus `type`),
 * in display order. `EVERY_OPTION` is the compile-time guard: an option
 * added to `CardOptions` without a line here fails `tsc` (the `Record` is
 * missing a key), and the schema test then fails until the schema lists it.
 */
const EVERY_OPTION: Record<keyof CardOptions, true> = {
  title: true,
  entry_id: true,
  show_title: true,
  show_history: true,
  show_health: true,
};
const OPTIONS: Array<keyof CardOptions> = ["title", "entry_id", "show_title", "show_history", "show_health"];

function config(extra: Record<string, unknown> = {}): IrrigationTimelineCardConfig {
  return { type: `custom:${CARD_TYPE}`, ...extra } as IrrigationTimelineCardConfig;
}

describe("CONFIG_SCHEMA", () => {
  it("lists every card option and nothing else, in order", () => {
    expect(CONFIG_SCHEMA.map((item) => item.name)).toEqual(OPTIONS);
    expect([...OPTIONS].sort()).toEqual(Object.keys(EVERY_OPTION).sort());
  });

  it("uses a text selector for the title, a config_entry selector filtered on this integration, and three boolean switches defaulting to on", () => {
    expect(CONFIG_SCHEMA[0]).toEqual({ name: "title", selector: { text: {} } });
    expect(CONFIG_SCHEMA[1]).toEqual({
      name: "entry_id",
      selector: { config_entry: { integration: INTEGRATION_DOMAIN } },
    });
    expect(INTEGRATION_DOMAIN).toBe("ha_irrigation_controller");
    expect(CONFIG_SCHEMA[2]).toEqual({ name: "show_title", default: true, selector: { boolean: {} } });
    expect(CONFIG_SCHEMA[3]).toEqual({ name: "show_history", default: true, selector: { boolean: {} } });
    expect(CONFIG_SCHEMA[4]).toEqual({ name: "show_health", default: true, selector: { boolean: {} } });
  });

  it("marks nothing required: the zero-config card is the happy path", () => {
    expect(CONFIG_SCHEMA.every((item) => item.required === undefined)).toBe(true);
    // The text and picker items have no default either: an absent key stays absent.
    expect(CONFIG_SCHEMA.slice(0, 2).every((item) => item.default === undefined)).toBe(true);
  });
});

describe("LABELS and HELPERS", () => {
  it("give every schema name a label and a helper", () => {
    for (const item of CONFIG_SCHEMA) {
      expect(LABELS[item.name as keyof CardOptions]).toBeTruthy();
      expect(HELPERS[item.name as keyof CardOptions]).toBeTruthy();
    }
    expect(Object.keys(LABELS).sort()).toEqual([...OPTIONS].sort());
    expect(Object.keys(HELPERS).sort()).toEqual([...OPTIONS].sort());
  });

  it("pins the labels", () => {
    expect(OPTIONS.map((name) => LABELS[name])).toEqual([
      "Title",
      "Controller",
      "Show title",
      "Show the last 7 days",
      "Show health",
    ]);
  });

  it("tells the title field how a blank reads and where the heading is hidden", () => {
    expect(HELPERS.title).toBe(`The card's heading; reads "${DEFAULT_TITLE}" when empty. Use Show title to hide it.`);
  });
});

describe("configForm", () => {
  it("returns the schema, the shared validator and the label/helper lookups", () => {
    const form = configForm();
    expect(form.schema).toEqual(CONFIG_SCHEMA);
    expect(form.assertConfig).toBe(assertCardConfig);
    expect(form.computeLabel?.({ name: "entry_id", selector: {} })).toBe("Controller");
    expect(form.computeHelper?.({ name: "entry_id", selector: {} })).toBe(HELPERS.entry_id);
  });

  it("copies the schema item by item, so an editor mutating its form does not rewrite the module's schema", () => {
    const form = configForm();
    expect(form.schema).not.toBe(CONFIG_SCHEMA);
    expect(form.schema[2]).not.toBe(CONFIG_SCHEMA[2]);
    expect(form.schema[2]?.selector).not.toBe(CONFIG_SCHEMA[2]?.selector);
    form.schema[2]!.default = false;
    form.schema[2]!.selector["boolean"] = { mutated: true };
    (form.schema[1]!.selector["config_entry"] as { integration: string }).integration = "other";
    form.schema.pop();
    expect(CONFIG_SCHEMA[2]).toEqual({ name: "show_title", default: true, selector: { boolean: {} } });
    expect(CONFIG_SCHEMA[1]).toEqual({
      name: "entry_id",
      selector: { config_entry: { integration: INTEGRATION_DOMAIN } },
    });
    expect(CONFIG_SCHEMA).toHaveLength(5);
    expect(configForm().schema).toEqual(CONFIG_SCHEMA);
  });

  it("answers undefined for a name it does not know, so HA's own fallback applies", () => {
    const form = configForm();
    expect(form.computeLabel?.({ name: "unknown", selector: {} })).toBeUndefined();
    expect(form.computeHelper?.({ name: "unknown", selector: {} })).toBeUndefined();
    // A prototype name is not a label either.
    expect(form.computeLabel?.({ name: "constructor", selector: {} })).toBeUndefined();
    expect(form.computeHelper?.({ name: "toString", selector: {} })).toBeUndefined();
  });

  it("validates through the same function setConfig uses: same faults, same messages", () => {
    const form = configForm();
    expect(() => form.assertConfig?.(config({ entry_id: 7 }))).toThrow(/entry_id must be a string/);
    expect(() => form.assertConfig?.(config({ show_title: 1 }))).toThrow(/show_title must be a boolean/);
    expect(() => form.assertConfig?.(config({ show_health: "no" }))).toThrow(/show_health must be a boolean/);
    expect(() => form.assertConfig?.(config())).not.toThrow();
  });
});

describe("assertCardConfig", () => {
  it("refuses anything that is not an object", () => {
    for (const value of [null, undefined, "nope", 42, true]) {
      expect(() => assertCardConfig(value)).toThrow(`${CARD_TYPE}: invalid configuration`);
    }
  });

  it("refuses a non-string entry_id", () => {
    expect(() => assertCardConfig(config({ entry_id: 7 }))).toThrow(
      `${CARD_TYPE}: invalid configuration (entry_id must be a string)`,
    );
    expect(() => assertCardConfig(config({ entry_id: null }))).toThrow(/entry_id must be a string/);
  });

  it("refuses a non-boolean switch, naming it", () => {
    expect(() => assertCardConfig(config({ show_title: 1 }))).toThrow(
      `${CARD_TYPE}: invalid configuration (show_title must be a boolean)`,
    );
    expect(() => assertCardConfig(config({ show_history: "yes" }))).toThrow(
      `${CARD_TYPE}: invalid configuration (show_history must be a boolean)`,
    );
    expect(() => assertCardConfig(config({ show_health: "no" }))).toThrow(
      `${CARD_TYPE}: invalid configuration (show_health must be a boolean)`,
    );
    expect(() => assertCardConfig(config({ show_health: 1 }))).toThrow(/show_health must be a boolean/);
  });

  it("leaves the title unvalidated, as before this story", () => {
    expect(() => assertCardConfig(config({ title: 123 }))).not.toThrow();
    expect(() => assertCardConfig(config({ title: false }))).not.toThrow();
  });

  it("passes the empty config, the stub, a blank controller, and every option set", () => {
    expect(() => assertCardConfig({})).not.toThrow();
    expect(() => assertCardConfig({ type: `custom:${CARD_TYPE}`, ...stubConfig() })).not.toThrow();
    expect(() => assertCardConfig(config({ entry_id: "" }))).not.toThrow();
    expect(() =>
      assertCardConfig(
        config({ title: "", entry_id: "e", show_title: false, show_history: false, show_health: false }),
      ),
    ).not.toThrow();
  });
});

describe("stubConfig", () => {
  it("is the zero-config default: no options at all", () => {
    expect(stubConfig()).toEqual({});
    expect(Object.keys(stubConfig())).toHaveLength(0);
    // A fresh object each time: the picker may mutate what it gets.
    expect(stubConfig()).not.toBe(stubConfig());
  });
});

describe("entryIdOf", () => {
  it("is undefined — auto-detect — for an absent or blank entry_id", () => {
    expect(entryIdOf(undefined)).toBeUndefined();
    expect(entryIdOf(config())).toBeUndefined();
    expect(entryIdOf(config({ entry_id: "" }))).toBeUndefined();
    expect(entryIdOf(config({ entry_id: "  " }))).toBeUndefined();
  });

  it("is the id itself otherwise, trimmed so a padded id never reaches the wire", () => {
    expect(entryIdOf(config({ entry_id: "a" }))).toBe("a");
    expect(entryIdOf(config({ entry_id: " a " }))).toBe("a");
  });
});

describe("showTitle / showHistory / showHealth", () => {
  it("read true unless the key is exactly false", () => {
    for (const read of [showTitle, showHistory, showHealth]) {
      expect(read(config())).toBe(true);
    }
    expect(showTitle(config({ show_title: true }))).toBe(true);
    expect(showTitle(config({ show_title: false }))).toBe(false);
    expect(showHistory(config({ show_history: true }))).toBe(true);
    expect(showHistory(config({ show_history: false }))).toBe(false);
    expect(showHealth(config({ show_health: true }))).toBe(true);
    expect(showHealth(config({ show_health: false }))).toBe(false);
  });

  it("read true without a config at all (the card before setConfig)", () => {
    expect(showTitle(undefined)).toBe(true);
    expect(showHistory(undefined)).toBe(true);
    expect(showHealth(undefined)).toBe(true);
  });

  it("read their own switch only", () => {
    const off = config({ show_title: false });
    expect(showTitle(off)).toBe(false);
    expect(showHistory(off)).toBe(true);
    expect(showHealth(off)).toBe(true);
  });
});

describe("headingOf", () => {
  it("defaults to Irrigation without a title", () => {
    expect(DEFAULT_TITLE).toBe("Irrigation");
    expect(headingOf(config())).toBe("Irrigation");
    expect(headingOf(undefined)).toBe("Irrigation");
  });

  it("returns the configured title verbatim", () => {
    expect(headingOf(config({ title: "Garden" }))).toBe("Garden");
    expect(headingOf(config({ title: " Garden " }))).toBe(" Garden ");
  });

  it("reads Irrigation for a blank title too: empty or whitespace", () => {
    expect(headingOf(config({ title: "" }))).toBe("Irrigation");
    expect(headingOf(config({ title: "  " }))).toBe("Irrigation");
    expect(headingOf(config({ title: "\t\n" }))).toBe("Irrigation");
  });

  it("reads Irrigation for a YAML title with no value (null)", () => {
    expect(headingOf(config({ title: null }))).toBe("Irrigation");
  });

  it("does not throw on a non-string title from raw YAML", () => {
    expect(headingOf(config({ title: 123 }))).toBe("123");
  });
});
