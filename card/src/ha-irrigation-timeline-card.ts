import type { HomeAssistant, LovelaceCardConfig } from "custom-card-helpers";
import { formatTime } from "custom-card-helpers";
import type { Connection } from "home-assistant-js-websocket";
import { css, html, LitElement, nothing, svg } from "lit";
import type { CSSResultGroup, PropertyValues, TemplateResult } from "lit";
import { state } from "lit/decorators.js";

import { estimateOffset, serverNow, tickPeriodFor, TRANSITION_MS } from "./clock";
import type { RowStatus, TimelineRow } from "./timeline";
import { buildTimeline, cursorAt, progressOf } from "./timeline";
import type { CycleKind, Handshake, StateView } from "./types";
import type { UnsubscribeState } from "./ws";
import { asWsError, ERR_NOT_FOUND, ERR_NOT_LOADED, fetchState, subscribeState } from "./ws";

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

/**
 * The word a row's header carries once its run has reached a terminal
 * state. A planned, pending or running row says nothing: the fill and the
 * cursor already do. Only three terminal statuses can reach `runs.current`
 * or `runs.last`: `waived` and `missed` runs are terminal at birth — filed
 * to history and dropped in one step (`engine/runs.py`) — so the timeline
 * never sees them; they are Story 4.4's history strip's business.
 */
const TERMINAL_LABELS: Partial<Record<RowStatus, string>> = {
  completed: "Completed",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
};

/** The row statuses "now" still means something for: the cursor is drawn on these only. */
const LIVE_STATUSES: ReadonlySet<RowStatus> = new Set<RowStatus>(["planned", "pending", "running"]);

/** `cursorAt`, gated by the row's status: a finished row has no "now". */
function liveCursor(row: TimelineRow, nowMs: number): number | undefined {
  return LIVE_STATUSES.has(row.status) ? cursorAt(row, nowMs) : undefined;
}

/** The geometry memo: rows are rebuilt only when the document or the zone changes. */
interface TimelineMemo {
  view: StateView;
  timeZone: string | undefined;
  rows: TimelineRow[];
}

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
 * segment per zone, labelled with the zone's name and planned start–end,
 * coloured by its engine status, with a live fill on the running zone and
 * a now-cursor across the row (Story 4.3).
 *
 * The card reads the engine's state view over Story 4.1's `state_subscribe`
 * channel and nothing else, and it renders only when a document arrives
 * (or its config or connection state changes): `hass` is a PLAIN accessor,
 * not a reactive property, so the fresh `hass` object Lovelace assigns on
 * every state change in the house schedules no update at all.
 *
 * The one addition to that gate is the clock: a `setInterval` (1 s while a
 * run is running, 60 s otherwise) sets `_nowMs` to the server-aligned wall
 * clock, and `shouldUpdate` lets that through only when a cursor is
 * visible somewhere. Ticks never accumulate — a throttled tab lands at the
 * wall-clock position on its next tick, and a tab turning visible ticks at
 * once. Geometry is memoised on the document reference, so a tick
 * re-evaluates the cursor and fill bindings and nothing else.
 */
export class HaIrrigationTimelineCard extends LitElement {
  @state() private _config?: IrrigationTimelineCardConfig;

  /** The last pushed document — the ONE thing a render is driven by. */
  @state() private _view?: StateView;

  /** A readable connection problem, shown in the card body until it clears. */
  @state() private _error?: string;

  /**
   * The server-aligned "now" of the last tick or push, in epoch ms. The
   * only reactive field a tick touches; `shouldUpdate` drops it when no
   * row has a cursor to move.
   */
  @state() private _nowMs?: number;

  /** `generated_at − Date.now()` at the last push whose stamp parsed. */
  private _offsetMs?: number;

  private _ticker?: ReturnType<typeof setInterval>;

  /** The period `_ticker` was armed with; the ticker is replaced when it changes. */
  private _tickerPeriod?: number;

  private _memo?: TimelineMemo;

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

  /**
   * The client/server clock offset in ms (server minus client) the card is
   * correcting with, 0 until a document's `generated_at` parsed. Read-only,
   * applied silently; exposed for tests and later stories.
   */
  public get clockOffsetMs(): number {
    return this._offsetMs ?? 0;
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
      this._syncTicker();
    }
    this._ensureSubscribed();
  }

  public override connectedCallback(): void {
    super.connectedCallback();
    document.addEventListener("visibilitychange", this._onVisibilityChange);
    if (this._view !== undefined) {
      // Re-attached with a document: the cursor is as stale as the detached
      // spell; catch up now rather than at the next interval.
      this._tick();
    }
    this._syncTicker();
    this._ensureSubscribed();
  }

  public override disconnectedCallback(): void {
    super.disconnectedCallback();
    document.removeEventListener("visibilitychange", this._onVisibilityChange);
    this._stopTicker();
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
   * The re-render gate (AD-13). `_view`, `_config` and `_error` render on
   * their own, compared by reference by Lit itself. A tick (`_nowMs` alone)
   * renders only when it moves something visible: a cursor inside some row
   * before the tick or after it — the "after" for the ordinary case, the
   * "before" so the render that takes a cursor off the end of its row still
   * happens. An idle tick with the clock outside every row is dropped here,
   * and nothing else may render the card.
   */
  protected override shouldUpdate(changed: PropertyValues): boolean {
    if (changed.has("_view") || changed.has("_config") || changed.has("_error")) {
      return true;
    }
    if (!changed.has("_nowMs")) {
      return false;
    }
    const before = changed.get("_nowMs") as number | undefined;
    const rows = this._rows() ?? [];
    return rows.some(
      (row) =>
        this._cursor(row) !== undefined ||
        (before !== undefined && liveCursor(row, before) !== undefined),
    );
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

  /**
   * The rows of the current document, memoised on the document reference
   * and the time zone: a tick never re-runs the geometry.
   */
  private _rows(): TimelineRow[] | undefined {
    const view = this._view;
    if (view === undefined) {
      return undefined;
    }
    const timeZone = this._hass?.config?.time_zone;
    if (this._memo?.view !== view || this._memo.timeZone !== timeZone) {
      this._memo = { view, timeZone, rows: buildTimeline(view, timeZone).rows };
    }
    return this._memo.rows;
  }

  /**
   * Where the server-aligned clock sits on `row`, or nothing to draw: no
   * clock yet, outside the row, or a row whose run is over (a cycle
   * cancelled half-way would otherwise keep a moving cursor, and 60 s
   * renders, until its planned end).
   */
  private _cursor(row: TimelineRow): number | undefined {
    return this._nowMs === undefined ? undefined : liveCursor(row, this._nowMs);
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
    const terminal = TERMINAL_LABELS[row.status];
    return html`
      <section class="cycle" data-kind=${row.kind} data-source=${row.source} data-status=${row.status}>
        <header class="cycle-header">
          <span class="cycle-kind">${KIND_LABELS[row.kind]}</span>
          ${terminal === undefined ? nothing : html`<span class="cycle-status">${terminal}</span>`}
          <span class="cycle-window">${this._window(row.start, row.end)}</span>
        </header>
        ${row.segments.length === 0
          ? html`<p class="empty">No zones are planned for this cycle.</p>`
          : this._renderLanes(row)}
      </section>
    `;
  }

  /**
   * The lanes of one row. Per segment: the status-coloured base rect and,
   * for a running zone, a `progress` overlay scaled to the cursor (a CSS
   * `transform` so the 1.1 s transition carries it between ticks); one
   * `cursor` line across the row while the clock is inside it.
   */
  private _renderLanes(row: TimelineRow): TemplateResult {
    const { segments } = row;
    const height = segments.length * LANE_HEIGHT;
    const cursor = this._cursor(row);
    return html`
      <div class="lanes" style=${`--hic-lanes: ${segments.length}`}>
        <ol class="labels">
          ${segments.map(
            (segment) => html`
              <li class="label" data-status=${segment.status}>
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
          ${segments.map((segment, index) => {
            const x = segment.x0 * AXIS_WIDTH;
            const y = index * LANE_HEIGHT + BAR_INSET;
            const width = Math.max(0, (segment.x1 - segment.x0) * AXIS_WIDTH);
            const barHeight = LANE_HEIGHT - 2 * BAR_INSET;
            return svg`
              <rect
                class="segment"
                data-zone=${segment.zoneId}
                data-status=${segment.status}
                x=${x}
                y=${y}
                width=${width}
                height=${barHeight}
              ></rect>
              ${
                segment.status === "running"
                  ? svg`
                    <rect
                      class="progress"
                      data-zone=${segment.zoneId}
                      x=${x}
                      y=${y}
                      width=${width}
                      height=${barHeight}
                      style=${`transform: scaleX(${progressOf(segment, cursor)})`}
                    ></rect>
                  `
                  : nothing
              }
            `;
          })}
          ${cursor === undefined
            ? nothing
            : svg`
              <line
                class="cursor"
                x1=${cursor * AXIS_WIDTH}
                x2=${cursor * AXIS_WIDTH}
                y1="0"
                y2=${height}
              ></line>
            `}
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
    // Re-estimated on every push: the newest estimate wins; an unparsable
    // stamp keeps the previous one.
    const offset = estimateOffset(view.generated_at, Date.now());
    if (offset !== undefined) {
      this._offsetMs = offset;
    }
    this._nowMs = serverNow(this._offsetMs);
    this._view = view;
    if (this._error !== undefined) {
      this._error = undefined;
    }
    this._syncTicker();
  }

  // ------------------------------------------------------------------ clock

  /**
   * Keep exactly one interval running while a document is here and the
   * element is connected, at the period the document calls for; replace it
   * when the period changes, stop it otherwise.
   */
  private _syncTicker(): void {
    const period = this.isConnected && this._view !== undefined ? tickPeriodFor(this._view) : undefined;
    if (period === this._tickerPeriod) {
      return;
    }
    this._stopTicker();
    if (period !== undefined) {
      this._tickerPeriod = period;
      this._ticker = setInterval(this._tick, period);
    }
  }

  private _stopTicker(): void {
    if (this._ticker !== undefined) {
      clearInterval(this._ticker);
    }
    this._ticker = undefined;
    this._tickerPeriod = undefined;
  }

  /** One tick: read the wall clock; never add the period to the last value. */
  private readonly _tick = (): void => {
    this._nowMs = serverNow(this._offsetMs);
  };

  private readonly _onVisibilityChange = (): void => {
    if (document.visibilityState !== "visible" || this._view === undefined) {
      return;
    }
    // A throttled tab has missed ticks; the first visible moment catches up
    // to the wall clock at once instead of waiting for the next period.
    this._tick();
    // The offset estimate may be wrong too: a push queued while the tab was
    // frozen was stamped when sent but received minutes later (a sleeping
    // laptop steps its clock the same way), and that delay was booked as
    // clock offset. One fresh round-trip re-estimates it from a receipt
    // time that is honest again.
    this._refetch();
  };

  /**
   * Fetch one document over the open subscription's connection and feed it
   * through `_onView`. A refusal is ignored (the subscription's own retry
   * path owns errors); a result landing after a close, a re-point or a lost
   * socket is dropped by the generation counter, like a stale subscribe.
   */
  private _refetch(): void {
    const hass = this._hass;
    if (hass === undefined || this._unsubscribe === undefined) {
      return;
    }
    const generation = this._generation;
    fetchState(hass, this._config?.entry_id)
      .then((view) => {
        if (generation === this._generation && this._unsubscribe !== undefined) {
          this._onView(view);
        }
      })
      .catch(() => undefined);
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
      .cycle-status {
        flex: 1;
        font-size: 0.85em;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
      }
      .cycle[data-status="completed"] .cycle-status {
        color: var(--hic-completed-color, var(--success-color, #43a047));
      }
      .cycle[data-status="cancelled"] .cycle-status,
      .cycle[data-status="interrupted"] .cycle-status {
        color: var(--hic-failed-color, var(--error-color, #db4437));
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
      /* Inside a run, a zone not yet reached is a promise, not a fact. */
      .segment[data-status="pending"] {
        opacity: 0.4;
      }
      .label[data-status="skipped"] .zone {
        text-decoration: line-through;
        color: var(--hic-secondary-text-color, var(--secondary-text-color));
      }
      /* The running zone's base is a faint track; the overlay is the fill. */
      .segment[data-status="running"] {
        fill: var(--hic-running-color, var(--hic-segment-color, var(--primary-color, #03a9f4)));
        opacity: 0.3;
      }
      .segment[data-status="completed"] {
        fill: var(--hic-completed-color, var(--success-color, #43a047));
      }
      .segment[data-status="failed"] {
        fill: var(--hic-failed-color, var(--error-color, #db4437));
      }
      .segment[data-status="skipped"] {
        fill: var(--hic-skipped-color, var(--disabled-text-color, #bdbdbd));
      }
      .progress {
        fill: var(--hic-running-color, var(--hic-segment-color, var(--primary-color, #03a9f4)));
        transform-box: fill-box;
        transform-origin: left;
        /* Slightly longer than the tick so the fill glides instead of stepping. */
        transition: transform ${TRANSITION_MS}ms linear;
      }
      .cursor {
        stroke: var(--hic-cursor-color, var(--primary-text-color, #212121));
        stroke-width: 2px;
        /* preserveAspectRatio="none" would stretch the stroke with the axis. */
        vector-effect: non-scaling-stroke;
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
      "Today's irrigation plan: each cycle's zones as a proportional timeline with planned times, live progress and outcomes.",
  });
}

console.info(
  `%c ${CARD_TYPE.toUpperCase()} %c v${CARD_VERSION}`,
  "color: white; background: #2e7d32; font-weight: 700;",
  "",
);
