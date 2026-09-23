import type { HomeAssistant, LovelaceCardConfig } from "custom-card-helpers";
import { formatTime } from "custom-card-helpers";
import type { Connection } from "home-assistant-js-websocket";
import { css, html, LitElement, nothing, svg } from "lit";
import type { CSSResultGroup, PropertyValues, TemplateResult } from "lit";
import { state } from "lit/decorators.js";

import type { TimelineRow, TimelineSegment } from "./timeline";
import { buildTimeline } from "./timeline";
import type { CycleKind, Handshake, StateView } from "./types";
import type { UnsubscribeState } from "./ws";
import { asWsError, ERR_NOT_FOUND, ERR_NOT_LOADED, subscribeState } from "./ws";

const CARD_VERSION = "0.1.0";
const CARD_TYPE = "ha-irrigation-timeline-card";

/** How long after a refused subscribe the card tries again. */
export const RETRY_DELAY_MS = 5000;

/**
 * Pixel height of one zone lane. Shared by the HTML label column and the
 * SVG bar column so the two stay aligned: the labels are rows of exactly
 * this height, and the SVG's CSS height is `lanes * LANE_HEIGHT` with a
 * viewBox of the same height, so one viewBox unit is one CSS pixel
 * vertically while the x axis (0..1000) stretches to the column width.
 */
export const LANE_HEIGHT = 28;
const BAR_INSET = 5;
const AXIS_WIDTH = 1000;
/** Rough pixel budget per Lovelace grid row (56 px rows, 8 px gaps). */
const GRID_ROW_PX = 64;

const KIND_LABELS: Record<CycleKind, string> = {
  morning: "Morning",
  evening: "Evening",
};

export interface IrrigationTimelineCardConfig extends LovelaceCardConfig {
  type: string;
  title?: string;
  /** Optional: the controller entry to read. Omitted, the single controller answers. */
  entry_id?: string;
}

/** Sections-view sizing hints (`getGridOptions`), in Lovelace grid units. */
export interface GridOptions {
  rows: number;
  columns: number;
  min_rows: number;
  min_columns: number;
}

/**
 * Today's irrigation plan as a timeline: one row per cycle, one proportional
 * segment per zone, labelled with the zone's name and planned start–end.
 *
 * The card reads the engine's state view over Story 4.1's `state_subscribe`
 * channel and nothing else, and it renders only when a document arrives
 * (or its config or connection state changes): `hass` is a PLAIN accessor,
 * not a reactive property, so the fresh `hass` object Lovelace assigns on
 * every state change in the house schedules no update at all.
 */
export class HaIrrigationTimelineCard extends LitElement {
  @state() private _config?: IrrigationTimelineCardConfig;

  /** The last pushed document — the ONE thing a render is driven by. */
  @state() private _view?: StateView;

  /** A readable connection problem, shown in the card body until it clears. */
  @state() private _error?: string;

  private _handshake?: Handshake;

  private _hass?: HomeAssistant;

  private _unsubscribe?: UnsubscribeState;

  /**
   * Subscription generation. Bumped whenever the current subscription (open
   * or in flight) stops being wanted — a close, a re-point, a lost socket —
   * so an attempt that resolves or rejects afterwards can tell it is stale.
   */
  private _generation = 0;

  private _pending = false;

  /** The connection whose `disconnected`/`ready` events are being followed. */
  private _connection?: Connection;

  private _retry?: ReturnType<typeof setTimeout>;

  /**
   * Set by Lovelace on every state change. Stored, never observed: the
   * first assignment is what lets the subscription open (it is the first
   * moment a connection exists); later ones cost a field write and nothing
   * more. Renders come from `_view`, `_config` and `_error` only.
   */
  public get hass(): HomeAssistant | undefined {
    return this._hass;
  }

  public set hass(value: HomeAssistant | undefined) {
    this._hass = value;
    this._ensureSubscribed();
  }

  /** The stale-bundle handshake as last pushed; stored for Story 4.7, unused here. */
  public get handshake(): Handshake | undefined {
    return this._handshake;
  }

  public setConfig(config: IrrigationTimelineCardConfig): void {
    // Lovelace hands over raw YAML, so the declared type is a promise rather
    // than a guarantee. Throwing is the contract that makes the card editor
    // show an error instead of rendering a blank hole in the dashboard.
    const candidate: unknown = config;
    if (candidate === null || typeof candidate !== "object") {
      throw new Error(`${CARD_TYPE}: invalid configuration`);
    }
    const next = candidate as IrrigationTimelineCardConfig;
    if (next.entry_id !== undefined && typeof next.entry_id !== "string") {
      throw new Error(`${CARD_TYPE}: invalid configuration (entry_id must be a string)`);
    }
    const previous = this._config;
    this._config = next;
    if (previous !== undefined && previous.entry_id !== next.entry_id) {
      // Another controller: the open subscription answers for the old one.
      this._closeSubscription();
      this._view = undefined;
      this._error = undefined;
    }
    this._ensureSubscribed();
  }

  public override connectedCallback(): void {
    super.connectedCallback();
    this._ensureSubscribed();
  }

  public override disconnectedCallback(): void {
    super.disconnectedCallback();
    this._closeSubscription();
    this._unlisten();
  }

  public getCardSize(): number {
    const rows = this._rows();
    if (rows === undefined) {
      return 2;
    }
    // One unit (~50 px) for the header, then per cycle: its header line plus
    // its lanes.
    return (
      1 +
      rows.reduce(
        (total, row) => total + 1 + Math.ceil((Math.max(1, row.segments.length) * LANE_HEIGHT) / 50),
        0,
      )
    );
  }

  public getGridOptions(): GridOptions {
    const rows = this._rows() ?? [];
    const px =
      56 + rows.reduce((total, row) => total + 40 + Math.max(1, row.segments.length) * LANE_HEIGHT, 0);
    return {
      rows: Math.max(2, Math.ceil(px / GRID_ROW_PX)),
      min_rows: 2,
      // Full width by default; below half a section the axis stops reading.
      columns: 12,
      min_columns: 6,
    };
  }

  /**
   * The re-render gate (AD-13). `_view`, `_config` and `_error` are the
   * only reactive fields, compared by reference by Lit itself; this makes
   * the rule explicit and keeps any future reactive field from rendering
   * the card on its own.
   */
  protected override shouldUpdate(changed: PropertyValues): boolean {
    return changed.has("_view") || changed.has("_config") || changed.has("_error");
  }

  protected override render(): TemplateResult | typeof nothing {
    if (!this._config) {
      return nothing;
    }
    return html`
      <ha-card .header=${this._config.title ?? "Irrigation"}>
        <div class="content">${this._renderBody()}</div>
      </ha-card>
    `;
  }

  private _rows(): TimelineRow[] | undefined {
    if (this._view === undefined) {
      return undefined;
    }
    return buildTimeline(this._view, this._hass?.config?.time_zone).rows;
  }

  private _renderBody(): TemplateResult {
    if (this._error !== undefined) {
      return html`<p class="message error" role="alert">${this._error}</p>`;
    }
    const rows = this._rows();
    if (rows === undefined) {
      return html`<p class="message">Connecting to the irrigation controller…</p>`;
    }
    if (rows.length === 0) {
      return html`<p class="message">No cycle is planned today.</p>`;
    }
    return html`${rows.map((row) => this._renderRow(row))}`;
  }

  private _renderRow(row: TimelineRow): TemplateResult {
    return html`
      <section class="cycle" data-kind=${row.kind} data-source=${row.source}>
        <header class="cycle-header">
          <span class="cycle-kind">${KIND_LABELS[row.kind]}</span>
          <span class="cycle-window">${this._window(row.start, row.end)}</span>
        </header>
        ${row.segments.length === 0
          ? html`<p class="empty">No zones are planned for this cycle.</p>`
          : this._renderLanes(row.segments)}
      </section>
    `;
  }

  private _renderLanes(segments: TimelineSegment[]): TemplateResult {
    const height = segments.length * LANE_HEIGHT;
    return html`
      <div class="lanes" style=${`--hic-lanes: ${segments.length}`}>
        <ol class="labels">
          ${segments.map(
            (segment) => html`
              <li class="label">
                <span class="zone">${segment.name}</span>
                <span class="times">${this._window(segment.start, segment.end)}</span>
              </li>
            `,
          )}
        </ol>
        <svg
          class="bars"
          viewBox="0 0 ${AXIS_WIDTH} ${height}"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          ${segments.map(
            (segment, index) => svg`
              <rect
                class="segment"
                x=${segment.x0 * AXIS_WIDTH}
                y=${index * LANE_HEIGHT + BAR_INSET}
                width=${Math.max(0, (segment.x1 - segment.x0) * AXIS_WIDTH)}
                height=${LANE_HEIGHT - 2 * BAR_INSET}
              ></rect>
            `,
          )}
        </svg>
      </div>
    `;
  }

  /** "07:00 – 07:25" in the user's HA locale (falls back to the browser's). */
  private _window(start: string, end: string): string {
    return `${this._time(start)} – ${this._time(end)}`;
  }

  private _time(iso: string): string {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) {
      return "–";
    }
    const locale = this._hass?.locale;
    return locale ? formatTime(date, locale) : date.toLocaleTimeString();
  }

  /** Open the subscription if everything it needs is here and it is not already open. */
  private _ensureSubscribed(): void {
    if (!this.isConnected || this._hass === undefined) {
      return;
    }
    this._listen(this._hass.connection);
    if (
      this._config === undefined ||
      this._unsubscribe !== undefined ||
      this._pending ||
      this._retry !== undefined
    ) {
      return;
    }
    void this._subscribe(this._hass, this._config.entry_id);
  }

  private async _subscribe(hass: HomeAssistant, entryId: string | undefined): Promise<void> {
    const generation = ++this._generation;
    const stale = (): boolean => generation !== this._generation;
    this._pending = true;
    try {
      const unsubscribe = await subscribeState(
        hass,
        (view) => {
          // An event of a released subscription can still be in flight.
          if (!stale()) {
            this._onView(view);
          }
        },
        entryId,
      );
      if (stale()) {
        // Removed from the DOM, re-pointed or disconnected while the command
        // was in flight: this subscription is nobody's; release it.
        void unsubscribe().catch(() => undefined);
      } else {
        this._unsubscribe = unsubscribe;
      }
    } catch (err) {
      // A stale attempt's refusal is not this card's problem any more (and a
      // lost socket rejects every pending command: `ready` re-subscribes).
      if (!stale()) {
        this._error = describeError(asWsError(err));
        this._retry = setTimeout(() => {
          this._retry = undefined;
          this._ensureSubscribed();
        }, RETRY_DELAY_MS);
      }
    } finally {
      this._pending = false;
    }
    // A config change during the await may have asked for another controller.
    this._ensureSubscribed();
  }

  /**
   * Follow the connection's lifecycle. The library's own re-subscribe after
   * a reconnect has no error path: a refusal during HA startup (the entry
   * not loaded yet, or this integration's commands not registered yet —
   * the frontend reconnects before that on every restart) would leave a
   * dead subscription and a stale document forever. So the subscription is
   * opened with `resubscribe: false`, dropped on `disconnected` and reopened
   * on `ready` through the same path a first subscribe takes, refusal,
   * message and 5 s retry included.
   */
  private _listen(connection: Connection): void {
    if (this._connection === connection) {
      return;
    }
    this._unlisten();
    this._connection = connection;
    connection.addEventListener("disconnected", this._onDisconnected);
    connection.addEventListener("ready", this._onReady);
  }

  private _unlisten(): void {
    const connection = this._connection;
    if (connection === undefined) {
      return;
    }
    connection.removeEventListener("disconnected", this._onDisconnected);
    connection.removeEventListener("ready", this._onReady);
    this._connection = undefined;
  }

  private readonly _onDisconnected = (): void => {
    // The server side went with the socket: nothing to send, just forget the
    // handle, and let an in-flight attempt know its answer is moot.
    this._unsubscribe = undefined;
    this._generation += 1;
  };

  private readonly _onReady = (): void => {
    this._ensureSubscribed();
  };

  private _onView(view: StateView): void {
    this._handshake = { schema_version: view.schema_version, version: view.version };
    this._view = view;
    if (this._error !== undefined) {
      this._error = undefined;
    }
  }

  private _closeSubscription(): void {
    if (this._retry !== undefined) {
      clearTimeout(this._retry);
      this._retry = undefined;
    }
    // Invalidates an in-flight subscribe: `_subscribe` releases its result.
    this._generation += 1;
    const unsubscribe = this._unsubscribe;
    this._unsubscribe = undefined;
    if (unsubscribe !== undefined) {
      // A closing socket may reject; there is nothing left to do about it.
      void unsubscribe().catch(() => undefined);
    }
  }

  static override get styles(): CSSResultGroup {
    return css`
      :host {
        display: block;
        --hic-lane-height: ${LANE_HEIGHT}px;
      }
      .content {
        padding: 0 16px 16px;
        color: var(--hic-text-color, var(--primary-text-color));
      }
      .message {
        margin: 0;
        padding: 8px 0;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
      }
      .message.error {
        color: var(--hic-error-color, var(--error-color, #db4437));
      }
      .cycle + .cycle {
        margin-top: 16px;
      }
      .cycle-header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 6px;
      }
      .cycle-kind {
        font-weight: 500;
      }
      .cycle-window,
      .times {
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
        font-variant-numeric: tabular-nums;
      }
      .cycle-window {
        font-size: 0.9em;
      }
      .lanes {
        display: grid;
        grid-template-columns: fit-content(55%) minmax(0, 1fr);
        column-gap: 12px;
        align-items: start;
      }
      .labels {
        list-style: none;
        margin: 0;
        padding: 0;
        min-width: 0;
      }
      .label {
        display: flex;
        gap: 8px;
        height: var(--hic-lane-height);
        line-height: var(--hic-lane-height);
        white-space: nowrap;
        font-size: 0.9em;
      }
      .label .zone {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .label .times {
        flex: none;
      }
      .bars {
        display: block;
        width: 100%;
        height: calc(var(--hic-lanes, 1) * var(--hic-lane-height));
        border-radius: 4px;
        background: var(--hic-track-color, var(--divider-color, rgba(0, 0, 0, 0.12)));
      }
      .segment {
        fill: var(--hic-segment-color, var(--primary-color, #03a9f4));
      }
      .empty {
        margin: 0;
        font-size: 0.9em;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
      }
    `;
  }
}

/** The card body's wording for a refused subscribe, by backend error code. */
function describeError(error: { code: string; message: string }): string {
  switch (error.code) {
    case ERR_NOT_FOUND:
      return "No irrigation controller was found. Set up the HA Irrigation Controller helper, or pass its entry_id in the card configuration.";
    case ERR_NOT_LOADED:
      return "The irrigation controller is loading; the card will retry shortly.";
    default:
      return `Cannot read the irrigation controller (${error.code}${error.message ? `: ${error.message}` : ""}); retrying.`;
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
    description:
      "Today's irrigation plan: each cycle's zones as a proportional timeline with planned times.",
  });
}

console.info(
  `%c ${CARD_TYPE.toUpperCase()} %c v${CARD_VERSION}`,
  "color: white; background: #2e7d32; font-weight: 700;",
  "",
);
