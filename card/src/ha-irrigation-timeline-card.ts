import type { HomeAssistant, LovelaceCardConfig } from "custom-card-helpers";
import { css, html, LitElement, nothing } from "lit";
import type { CSSResultGroup, TemplateResult } from "lit";
import { customElement, state } from "lit/decorators.js";

const CARD_VERSION = "0.1.0";

export interface IrrigationTimelineCardConfig extends LovelaceCardConfig {
  type: string;
  title?: string;
}

/**
 * Placeholder timeline card. It registers the element, renders inside
 * <ha-card>, and proves the build/bundle pipeline — the real timeline UI
 * arrives with Epic 4.
 */
@customElement("ha-irrigation-timeline-card")
export class HaIrrigationTimelineCard extends LitElement {
  @state() private _config?: IrrigationTimelineCardConfig;

  public hass?: HomeAssistant;

  public setConfig(config: IrrigationTimelineCardConfig): void {
    this._config = config;
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

window.customCards = window.customCards ?? [];
window.customCards.push({
  type: "ha-irrigation-timeline-card",
  name: "HA Irrigation Timeline Card",
  description: "Timeline of today's irrigation plan (skeleton).",
});

// eslint-disable-next-line no-console
console.info(`%c HA-IRRIGATION-TIMELINE-CARD %c v${CARD_VERSION}`, "color: white; background: #2e7d32; font-weight: 700;", "");
