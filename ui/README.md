# Desktop control panel (Tauri)

A small desktop app that runs and supervises the RXD/LTC liquidity bot and the
KDF node on the same machine. It is a thin shell over the bot's own
localhost API: the Python bot remains the only thing that trades.

```
┌──────────────── Tauri window (WebView2) ────────────────┐
│ React UI: Dashboard · Config · KDF node · Logs · Settings│
│            │ invoke()                                    │
│ Rust backend (src-tauri/src/lib.rs)                      │
│   • spawns  rxdltc-mm  and  kdf.exe  (stdout -> Logs)    │
│   • GET /status /events, POST /control/* (bearer token)  │
│   • allow-listed KDF RPC (version, balances, orders …)   │
│   • config.yaml / MM2.json read & write, --check-config  │
│   • RPC password in the OS keychain                      │
└──────────────────────────────────────────────────────────┘
```

## What each tab does

| tab | content |
|-----|---------|
| Setup | first-run wizard: creates the workspace (`%LOCALAPPDATA%\com.gleec.rxdltc-mm`) with `config.yaml` from the shipped defaults; downloads the **pinned** GLEEC KDF release and verifies its SHA-256 before extracting; downloads the GLEEC coins file at a pinned commit and checks RXD / LTC-segwit exist; writes `MM2.json` (netid 6133, GLEEC seed nodes, generated RPC and wallet passwords stored in the keychain); creates a new wallet (KDF generates and encrypts the seed) or imports one (plaintext removed after the first start); reveals the seed once for a paper backup with a three-word check; activates the coins and shows deposit addresses with QR codes |
| Dashboard | state badge, fair / bid / ask in both units, inventory share and skew, open orders, foreign order book around the fair line, last plan, provider health, statistics, recent events |
| Config | form over the important `config.yaml` keys (comments preserved) plus a raw YAML view; **Save & validate** writes a `.bak` and runs the bot's `--check-config` |
| KDF node | start/stop `kdf.exe`, version, enabled coins with balances and addresses, maker orders on the node (cancel one / all), `MM2.json` editor with masked secrets |
| Logs | live bot and KDF output with filter and follow |
| Settings | paths (bot command, config, data dir, KDF folder), URLs, and the KDF RPC password which is stored in the OS keychain and never written to a file |

Top bar buttons: **Start bot** (asks for the confirmation phrase when the
config has `dry_run: false`, with a "start small" preset that switches to fixed
20,000 RXD / 0.01 LTC orders), **Pause & cancel**, **Resume**, **Stop bot**.
A health checklist (setup, node, bot, funds, price sources, quoting, single
instance) sits above every tab; each chip jumps to the tab that fixes it.

## Safety model

* The bot is started with a random per-launch control token
  (`MM_BOT_CONTROL_TOKEN`); only this app instance can pause/resume/stop it.
* The RPC password is fetched from the keychain at launch and passed to the
  bot as an environment variable. The webview never sees it or the token.
* Live trading requires both `dry_run: false` in the config **and** typing
  `I_UNDERSTAND_THIS_TRADES_REAL_FUNDS` into the dialog; the phrase is passed
  to the bot as `MM_BOT_CONFIRM_LIVE` only for that launch.
* **Stop bot** and closing the window call the bot's `/control/stop`, so the
  bot cancels its orders (per `shutdown.cancel_orders_on_exit`) before the
  process ends. A hard kill is used only if it does not exit in time.
* **Stop node** calls KDF's own `stop` RPC (graceful) and works for a node the
  app did not start.
* KDF RPC calls from the UI are limited to an allow-list (version, balances,
  orders, order book, swaps, cancel, stop).

## Prerequisites

End users need nothing but the installer: the bot is bundled as a sidecar
executable and the wizard fetches KDF and the coins file. Developers need
Node.js 20+, Rust 1.77+ (`cargo`), WebView2 (ships with Windows 11) and the
bot's venv with `pyinstaller` to build the sidecar:

```powershell
.\.venv\Scripts\python uiuild-sidecar.py     # -> ui/src-tauri/binaries/rxdltc-mm-<triple>.exe
```

The sidecar must exist before `cargo build` / `npm run tauri build`, because
`bundle.externalBin` in `tauri.conf.json` references it.

## Pinned downloads

| item | pinned to | verification |
|------|-----------|--------------|
| KDF Windows | `GLEECBTC/komodo-defi-framework` `v3.0.0-beta` `kdf_d56a7bc-win-x86-64.zip` | SHA-256 `4839a22f…91b2` |
| KDF Linux | same release, `kdf_d56a7bc-linux-x86-64.zip` | SHA-256 `20016e48…d501` |
| coins file, seed nodes | `GLEECBTC/coins` commit `846c222a` | RXD, LTC, LTC-segwit entries must exist |

Bump these in `src-tauri/src/setup.rs` when releasing a new app version.
"Check for KDF updates" on the KDF tab only reports a newer tag; it never
installs it.

## Code signing

CI produces unsigned bundles unless signing secrets are present, so the
workflow is safe to run with none, some, or all of them configured.

**macOS** (fully automated once the secrets exist). Add these repository
secrets; the two macOS jobs pick them up and Tauri signs and notarises:

| secret | contents |
|--------|----------|
| `APPLE_CERTIFICATE` | the **Developer ID Application** certificate exported from Keychain Access as `.p12`, base64 encoded (`base64 -i cert.p12 \| pbcopy`) |
| `APPLE_CERTIFICATE_PASSWORD` | the password used for that export |
| `APPLE_SIGNING_IDENTITY` | e.g. `Developer ID Application: Your Name (TEAMID)`, from `security find-identity -v -p codesigning` |
| `APPLE_API_ISSUER` | App Store Connect issuer UUID |
| `APPLE_API_KEY` | App Store Connect key ID, e.g. `ABC123DEFG` |
| `APPLE_API_KEY_CONTENT` | the contents of the downloaded `AuthKey_*.p8`; the workflow writes it to disk and sets `APPLE_API_KEY_PATH` |

Signing only engages when `APPLE_CERTIFICATE` is set, so an identity on its own
cannot break the build. Notarisation is skipped if `APPLE_API_KEY_CONTENT` is
absent, leaving a signed but un-notarised bundle.

**Windows** is not wired up. The certificate must live on certified hardware,
so a cloud HSM such as Certum SimplySign needs either a headless PKCS#11 path
driven through Tauri's `bundle.windows.signCommand`, a self-hosted runner with
the signing client logged in, or a manual signing pass over the released
`.exe` and `.msi`. The PyInstaller sidecar (`rxdltc-mm.exe`) is worth signing
too, since one-file Python builds often trip antivirus heuristics.

## Run in development

```powershell
cd ui
npm install
npm run tauri dev
```

Then open **Settings**: set the bot command to
`C:\development\mm-bot\.venv\Scripts\rxdltc-mm.exe`, the config to
`C:\development\mm-bot\config.yaml`, the KDF folder, and store the RPC
password. **KDF node → Start kdf.exe**, wait for the version to show, then
**Start bot**.

## Build an installer

```powershell
cd ui
npm run tauri build
```

Build the sidecar first (see Prerequisites). The bundle lands in
`ui/src-tauri/target/release/bundle/` (NSIS and MSI on Windows) and contains
the app plus the bot executable; KDF and the coins file are fetched by the
wizard on first run.

## Layout

```
ui/
  package.json, vite.config.ts, tsconfig.json, index.html
  src/
    api.ts            invoke() wrappers, types, formatting helpers
    App.tsx           top bar, tabs, start/stop/pause/resume, live dialog, health checklist
    panels/           SetupWizard, HealthChecklist, Dashboard, ConfigEditor, KdfPanel, LogView, SettingsPanel, LiveDialog
    styles.css
  src-tauri/
    Cargo.toml, tauri.conf.json, build.rs, capabilities/default.json, icons/, binaries/ (sidecar, built)
    src/lib.rs        process management, status/control proxy, KDF RPC allow-list, config/MM2 editing
    src/setup.rs      wizard backend: workspace, pinned KDF/coins download + SHA-256, MM2.json, passwords
    src/main.rs
  build-sidecar.py    PyInstaller build of the bot into src-tauri/binaries/
  make_icon.py        regenerates app-icon.png -> `npx tauri icon app-icon.png`
```
