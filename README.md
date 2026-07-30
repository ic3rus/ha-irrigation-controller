# HA Irrigation Controller

[![Validate](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/validate.yml/badge.svg)](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/validate.yml)
[![Test](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/test.yml/badge.svg)](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/test.yml)
[![Lint](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/lint.yml/badge.svg)](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/lint.yml)
[![Card](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/card.yml/badge.svg)](https://github.com/ic3rus/ha-irrigation-controller/actions/workflows/card.yml)

A Home Assistant custom integration for deterministic irrigation scheduling — a
hass-free sequencer engine driving your valves, plus a bundled Lovelace timeline
card (`ha-irrigation-timeline-card`) delivered in the same install.

**Requires Home Assistant 2026.7.0 or newer** (setup fails with a clear error on
older versions).

## Installation (HACS custom repository)

1. In HACS, open **⋮ → Custom repositories**.
2. Add `https://github.com/ic3rus/ha-irrigation-controller` with category
   **Integration**.
3. Install **HA Irrigation Controller**, then restart Home Assistant.
4. Go to **Settings → Devices & services → Helpers** and choose
   **Create helper → HA Irrigation Controller**.

> This integration declares `integration_type: helper`, so Home Assistant lists
> it under **Helpers** — it does *not* appear in the "Add integration" dialog.

Only one controller can be configured (`single_config_entry`).

The built card ships inside `custom_components/`, so a HACS install delivers it
in the same payload — no separate card install or download. Serving it to
Lovelace (static path + resource registration) arrives with Story 4.2, so the
card is not yet selectable in the dashboard editor.

## Development

The repo ships a devcontainer (Python 3.14 + Node 22). Open it in VS Code and
the `scripts/setup` post-create hook installs everything. Then:

```bash
scripts/develop
```

This starts a local Home Assistant instance (http://localhost:8123) with the
integration loaded **and** a Rollup watch that rebuilds the card bundle to
`custom_components/ha_irrigation_controller/frontend/` on change.

> **Note:** reloading the config entry does *not* pick up Python code changes —
> restart `scripts/develop` (full HA restart) after editing integration code.
> Card changes only need a browser hard-refresh.

Other loops:

```bash
scripts/lint                 # Ruff format + lint (autofix), then mypy
python3 -m pytest tests/     # backend + engine test suites
python3 -m pytest tests/engine  # engine only — must pass with HA uninstalled (AD-1)
cd card && npm run typecheck # tsc --noEmit (vitest does NOT type-check)
cd card && npm test          # card tests (vitest, headless Chromium)
cd card && npm run build     # rebuild the committed card bundle
```

> **Running hassfest locally:** exclude `card/node_modules` from the mount.
> `@vitest/browser` ships a Vite build manifest at
> `node_modules/@vitest/browser/dist/client/.vite/manifest.json`, which hassfest
> mistakes for a second integration and then crashes on. CI is unaffected — the
> hassfest job checks out the repo without installing card dependencies.

### Layout

```text
custom_components/ha_irrigation_controller/
  engine/     # hass-free scheduling core (imports NOTHING from homeassistant.*)
  adapters/   # HA <-> engine adapters
  entities/   # entity platforms
  frontend/   # BUILT card bundle (committed; shipped by HACS)
card/         # card TypeScript source (Lit 3 + Rollup + vitest)
tests/        # pytest-homeassistant-custom-component harness + engine tests
```

The built card bundle is committed on purpose: HACS installs
`custom_components/` as-is, so the card must live inside it (single-repo,
single-install design).

## CI

Every pull request (and every push to `main`) runs hassfest, HACS validation,
Ruff (format + lint), mypy, `tsc`, pytest against HA **stable and beta**, the
engine suite in an environment with **no Home Assistant installed** (the AD-1
gate), and the card build + vitest in headless Chromium. A nightly cron re-runs
all four workflows to catch Home Assistant's monthly breakage early;
`filterwarnings = ["error"]` means a `DeprecationWarning` fails the build while
the removal is still months away.

The HA beta leg is `continue-on-error`: it is an early-warning signal, not a
merge gate. Dependency bumps arrive as grouped dependabot PRs — note that
dependabot cannot bump `hassfest@master` or `hacs/action@main`, since a mutable
ref is not a version.

## License

MIT
