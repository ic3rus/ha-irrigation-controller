/**
 * The stale-bundle handshake — pure, DOM-free, hass-free (Story 4.7).
 *
 * Every pushed document carries the integration's `version` and the
 * state-view `schema_version` (`engine/view.py`). After an update a browser
 * or the companion app may keep serving the previous card bundle; comparing
 * those two keys with the constants this bundle was built with is how the
 * card notices, on the first push, and says so. The hint is display-only:
 * the document still renders — an older bundle usually reads a newer
 * document correctly, since additions do not bump the schema.
 */

import { CARD_VERSION } from "./editor";
import type { Handshake } from "./types";

/**
 * The state-view schema this bundle was built against: the mirror of
 * `engine/view.py` `STATE_SCHEMA_VERSION`, pinned to it by
 * `tests/test_manifest.py`.
 */
export const STATE_SCHEMA_VERSION = 1;

/** Whether the pushed document came from a different release or schema than this bundle. */
export function isStale(handshake: Handshake): boolean {
  return handshake.version !== CARD_VERSION || handshake.schema_version !== STATE_SCHEMA_VERSION;
}

const SEMVER = /^(\d+)\.(\d+)\.(\d+)/;

/**
 * Whether this bundle is the newer side: HACS has updated the files but
 * Home Assistant still runs the previous integration, and a hard refresh has
 * already served the new bundle. Reloading never clears that case; a restart
 * does. Compares `major.minor.patch` in order; when the numbers are equal or
 * either side does not parse, the schema numbers decide.
 */
function cardIsNewer(handshake: Handshake): boolean {
  const card = SEMVER.exec(CARD_VERSION);
  const integration = SEMVER.exec(handshake.version);
  if (card !== null && integration !== null) {
    for (let i = 1; i <= 3; i += 1) {
      const diff = Number(card[i]) - Number(integration[i]);
      if (diff !== 0) {
        return diff > 0;
      }
    }
  }
  return STATE_SCHEMA_VERSION > handshake.schema_version;
}

/**
 * The hint's sentence, naming both sides. The schema numbers are named only
 * when they differ — a version mismatch alone is the ordinary case. The fix
 * depends on which side is newer: an older card is fixed by a reload (or the
 * companion app's frontend-cache reset), a newer card by restarting Home
 * Assistant so the updated integration runs.
 */
export function staleHint(handshake: Handshake): string {
  const schemaDiffers = handshake.schema_version !== STATE_SCHEMA_VERSION;
  const card = schemaDiffers ? `${CARD_VERSION}, schema ${STATE_SCHEMA_VERSION}` : CARD_VERSION;
  const integration = schemaDiffers
    ? `${handshake.version}, schema ${handshake.schema_version}`
    : handshake.version;
  const fix = cardIsNewer(handshake)
    ? "Restart Home Assistant to finish the update."
    : "Reload the page — in the companion app, reset the frontend cache.";
  return `This card (${card}) does not match the installed integration (${integration}). ${fix}`;
}
