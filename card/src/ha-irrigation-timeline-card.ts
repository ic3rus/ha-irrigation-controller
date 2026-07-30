import type { HomeAssistant, LovelaceCardConfig } from "custom-card-helpers";
import { css, html, LitElement, nothing } from "lit";
import type { CSSResultGroup, TemplateResult } from "lit";
import { property, state } from "lit/decorators.js";

const CARD_VERSION = "0.1.0";
const CARD_TYPE = "ha-irrigation-timeline-card";

export interface IrrigationTimelineCardConfig extends LovelaceCardConfig {
  type: string;
  title?: string;
}

/**
 * Placeholder timeline card. It registers the element, renders inside
 * <ha-card>, and proves the build/bundle pipeline — the real timeline UI
 * arrives with Epic 4.
 */
export class HaIrrigationTimelineCard extends LitElement {
  @state() private _config?: IrrigationTimelineCardConfig;

  /** Set by Lovelace on every state change; must be reactive to re-render. */
  @property({ attribute: false }) public hass?: HomeAssistant;

  public setConfig(config: IrrigationTimelineCardConfig): void {
    // Lovelace hands over raw YAML, so the declared type is a promise rather
    // than a guarantee. Throwing is the contract that makes the card editor
    // show an error instead of rendering a blank hole in the dashboard.
    const candidate: unknown = config;
    if (candidate === null || typeof candidate !== "object") {
      throw new Error(`${CARD_TYPE}: invalid configuration`);
    }
    this._config = candidate as IrrigationTimelineCardConfig;
  }

  public getCardSize(): number {
    return 2;
  }

  protected override render(): TemplateResult | typeof nothing {
    if (!this._config) {
      return nothing;
    }
    return html`
      <ha-card .header=${this._config.title ?? "Irrigation"}>
        <div class="content">
          HA Irrigation Controller — timeline card skeleton (v${CARD_VERSION}).
        </div>
      </ha-card>
    `;
  }

  static override get styles(): CSSResultGroup {
    return css`
      .content {
        padding: 16px;
        color: var(--secondary-text-color);
      }
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "ha-irrigation-timeline-card": HaIrrigationTimelineCard;
  }
  interface Window {
    customCards?: Array<Record<string, unknown>>;
  }
}

// Guarded instead of using @customElement: cache-busting `?v=` query strings are
// the standard dev-loop workaround, and they make the browser evaluate this
// module twice. An unguarded customElements.define throws NotSupportedError on
// the second pass and takes the whole card down with it.
if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, HaIrrigationTimelineCard);
}

window.customCards = window.customCards ?? [];
if (!window.customCards.some((card) => card["type"] === CARD_TYPE)) {
  window.customCards.push({
    type: CARD_TYPE,
    name: "HA Irrigation Timeline Card",
    description: "Timeline of today's irrigation plan (skeleton).",
  });
}

console.info(
  `%c ${CARD_TYPE.toUpperCase()} %c v${CARD_VERSION}`,
  "color: white; background: #2e7d32; font-weight: 700;",
  "",
);
