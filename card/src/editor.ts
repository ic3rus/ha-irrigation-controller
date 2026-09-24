/**
 * The card's configuration contract — pure, DOM-free, hass-free (Story 4.6).
 *
 * Everything Lovelace's editor and picker need from the card that is not
 * rendering lives here: the config type and its validator (shared by
 * `setConfig` and the editor's `assertConfig`, so both refuse the same
 * thing with the same words), the `ha-form` schema with its labels and
 * helpers, the zero-config stub the picker adds, and the small readers the
 * element uses to honour the display options.
 *
 * The editor is schema-only: `hui-form-editor` owns `ha-form`, its `.data`,
 * validation and the `config-changed` event, so the card ships no editor
 * element and cannot drift from HA's editor conventions.
 */

import type { LovelaceCardConfig } from "custom-card-helpers";

export const CARD_VERSION = "0.1.0";
export const CARD_TYPE = "ha-irrigation-timeline-card";

/** The integration's domain: the `config_entry` selector lists its entries. */
export const INTEGRATION_DOMAIN = "ha_irrigation_controller";

/** The heading the card shows when `title` is absent or blank. */
export const DEFAULT_TITLE = "Irrigation";

/**
 * Every option the card takes, and nothing else. Kept free of an index
 * signature (unlike `LovelaceCardConfig`, whose `[key: string]: any`
 * collapses `keyof` to `string`) so the tests can type the schema's names
 * as `keyof CardOptions` and fail `tsc` on an option without a schema item.
 */
export interface CardOptions {
  /** The heading. Absent or blank (empty or whitespace): "Irrigation". */
  title?: string;
  /**
   * The controller entry to read. Absent or blank — `""` is what the
   * Controller picker writes when cleared — the single controller answers.
   */
  entry_id?: string;
  /** The heading; `true` when absent. Off, the header keeps the chip. */
  show_title?: boolean;
  /** The 7-day history strip; `true` when absent. */
  show_history?: boolean;
  /** The health chip, the anomaly banner and the announcer; `true` when absent. */
  show_health?: boolean;
}

export interface IrrigationTimelineCardConfig extends LovelaceCardConfig, CardOptions {
  type: string;
}

/**
 * One `ha-form` item, as HA's `src/components/ha-form/types.ts` shapes it
 * (the subset this card needs; `custom-card-helpers` 2.0.0 has no type
 * for it). A non-required item's `default` is handed to its selector as
 * the placeholder, which is how an absent switch reads "on".
 */
export interface HaFormSchema {
  name: string;
  required?: boolean;
  default?: unknown;
  selector: Record<string, unknown>;
}

/** What `getConfigForm()` returns — HA's `src/panels/lovelace/types.ts` `LovelaceConfigForm`. */
export interface LovelaceConfigForm {
  schema: HaFormSchema[];
  assertConfig?: (config: LovelaceCardConfig) => void;
  // HA passes `(schema, localize)`; the card has no use for `localize`.
  computeLabel?: (schema: HaFormSchema, localize?: unknown) => string | undefined;
  computeHelper?: (schema: HaFormSchema, localize?: unknown) => string | undefined;
}

/** The three display switches, in display order; each defaults to `true`. */
const SWITCHES = ["show_title", "show_history", "show_health"] as const satisfies ReadonlyArray<
  keyof CardOptions
>;

/**
 * Every option the card takes, and nothing else: the keys of `CardOptions`,
 * in display order. The switches carry `default: true` so an absent key
 * shows "on" in the editor while `getStubConfig()` stays `{}`.
 */
export const CONFIG_SCHEMA: readonly HaFormSchema[] = [
  { name: "title", selector: { text: {} } },
  { name: "entry_id", selector: { config_entry: { integration: INTEGRATION_DOMAIN } } },
  ...SWITCHES.map((name) => ({ name, default: true, selector: { boolean: {} } })),
];

/** English literals, as the rest of the card (no card i18n yet). */
export const LABELS: Record<keyof CardOptions, string> = {
  title: "Title",
  entry_id: "Controller",
  show_title: "Show title",
  show_history: "Show the last 7 days",
  show_health: "Show health",
};

export const HELPERS: Record<keyof CardOptions, string> = {
  title: `The card's heading; reads "${DEFAULT_TITLE}" when empty. Use Show title to hide it.`,
  entry_id: "The irrigation controller this card reads. Leave it empty to find it by itself, or pin it explicitly.",
  show_title: "Whether the heading is shown. Off, the health chip keeps the header to itself.",
  show_history: "The outcome of each cycle over the last seven days, under today's timeline.",
  show_health:
    "The health chip in the header and the banner listing open anomalies. Users who are not administrators have no other health surface.",
};

/**
 * Refuse what the card cannot render, with one message per fault. Shared
 * by `setConfig` (Lovelace hands over raw YAML) and by the editor's
 * `assertConfig`, so the editor shows the same error the dashboard would.
 * `title` is not validated (it never was): `headingOf` stringifies it.
 */
export function assertCardConfig(config: unknown): asserts config is IrrigationTimelineCardConfig {
  if (config === null || typeof config !== "object") {
    throw new Error(`${CARD_TYPE}: invalid configuration`);
  }
  const candidate = config as Record<string, unknown>;
  if (candidate["entry_id"] !== undefined && typeof candidate["entry_id"] !== "string") {
    throw new Error(`${CARD_TYPE}: invalid configuration (entry_id must be a string)`);
  }
  for (const name of SWITCHES) {
    if (candidate[name] !== undefined && typeof candidate[name] !== "boolean") {
      throw new Error(`${CARD_TYPE}: invalid configuration (${name} must be a boolean)`);
    }
  }
}

function lookup(table: Record<string, string>, name: string): string | undefined {
  // `Object.hasOwn`, not a bare index: a schema named "constructor" would
  // otherwise read a prototype function as its label.
  return Object.hasOwn(table, name) ? table[name] : undefined;
}

/**
 * The form `HaIrrigationTimelineCard.getConfigForm()` hands to
 * `hui-form-editor`. The schema is a deep copy, so a caller mutating what
 * it gets — down to a selector's options — does not rewrite the module's
 * schema for every later editor.
 */
export function configForm(): LovelaceConfigForm {
  return {
    schema: structuredClone(CONFIG_SCHEMA) as HaFormSchema[],
    assertConfig: assertCardConfig,
    // `undefined` for a name this card does not know: HA's own fallback applies.
    computeLabel: (schema) => lookup(LABELS, schema.name),
    computeHelper: (schema) => lookup(HELPERS, schema.name),
  };
}

/**
 * The picker's default: no options at all. The picker spreads it over
 * `{type}`, and the card without `entry_id` resolves the single controller
 * backend-side.
 */
export function stubConfig(): Record<string, never> {
  return {};
}

/**
 * The controller the card asks for: `undefined` — auto-detect, and no
 * `entry_id` on the wire — when the key is absent or blank. The Controller
 * picker writes `""` when cleared; the backend would read `""` as an
 * explicit id and answer `not_found` for a controller that exists.
 */
export function entryIdOf(config: IrrigationTimelineCardConfig | undefined): string | undefined {
  const entryId = config?.entry_id?.trim();
  return entryId === undefined || entryId === "" ? undefined : entryId;
}

/** Whether the heading is shown: only an explicit `false` hides it. */
export function showTitle(config: IrrigationTimelineCardConfig | undefined): boolean {
  return config?.show_title !== false;
}

/** Whether the 7-day strip is shown: only an explicit `false` hides it. */
export function showHistory(config: IrrigationTimelineCardConfig | undefined): boolean {
  return config?.show_history !== false;
}

/** Whether the chip, the banner and the announcer are shown: only an explicit `false` hides them. */
export function showHealth(config: IrrigationTimelineCardConfig | undefined): boolean {
  return config?.show_health !== false;
}

/**
 * The heading's text: the title, or "Irrigation" when it is absent or
 * blank (empty or whitespace). Hiding the heading is `show_title`'s job,
 * never a blank title's — HA's text selector cannot even write one.
 * A YAML `title:` with no value (`null`) is absent too. Stringified so a
 * non-string `title` in raw YAML cannot throw.
 */
export function headingOf(config: IrrigationTimelineCardConfig | undefined): string {
  const title = config?.title;
  return title == null || String(title).trim() === "" ? DEFAULT_TITLE : String(title);
}
