//! Tauri backend for the RXD/LTC liquidity bot control panel.
//!
//! Everything that touches secrets, processes or the network lives here; the
//! webview only calls these commands. The bot's control token and the KDF RPC
//! password never reach the frontend.
//!
//! * `settings_*`  – paths and URLs, persisted in the app config dir
//! * `secret_*`    – OS keychain (Windows Credential Manager / macOS Keychain / Linux keyutils)
//! * `bot_*`       – spawn/stop the Python bot, proxy `/status`, `/events`, `/control/*`
//! * `kdf_*`       – spawn/stop `kdf.exe`, allow-listed RPC proxy, MM2.json editing
//! * `config_*`    – read/write/validate the bot's YAML config
//! * `logs_get`    – ring buffers of captured stdout/stderr (also streamed as events)

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

mod setup;

use rand::{distributions::Alphanumeric, Rng};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager, State};

const LIVE_PHRASE: &str = "I_UNDERSTAND_THIS_TRADES_REAL_FUNDS";
const KEYRING_SERVICE: &str = "rxdltc-mm";
const LOG_CAPACITY: usize = 3000;
/// Legacy-style RPCs the UI may call.
const KDF_RPC_ALLOWLIST: &[&str] = &[
    "version",
    "get_enabled_coins",
    "my_balance",
    "my_orders",
    "orderbook",
    "my_recent_swaps",
    "active_swaps",
    "cancel_order",
    "cancel_all_orders",
    "electrum",
    "stop",
];
/// mmrpc 2.0 RPCs the UI may call.
const KDF_RPC_V2_ALLOWLIST: &[&str] = &["get_enabled_coins", "get_wallet_names", "get_mnemonic", "orderbook", "my_recent_swaps"];

// ------------------------------------------------------------------ settings

#[derive(Clone, Serialize, Deserialize)]
pub struct Settings {
    /// Command used to run the bot, e.g. `C:\development\mm-bot\.venv\Scripts\rxdltc-mm.exe`.
    pub bot_command: String,
    /// The bot's YAML config file.
    pub config_path: String,
    /// Directory for the bot's SQLite state (MM_BOT_DATA_DIR). Empty = bot default.
    pub data_dir: String,
    /// Base URL of the bot's status/control server (metrics.bind/port in the config).
    pub status_url: String,
    /// KDF RPC URL (KDF_RPC_URL for the bot and for the UI's own RPC calls).
    pub kdf_rpc_url: String,
    /// Folder containing kdf.exe, MM2.json and the coins file.
    pub kdf_dir: String,
    /// kdf executable name inside kdf_dir.
    pub kdf_exe: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            bot_command: "rxdltc-mm".into(),
            config_path: "config.yaml".into(),
            data_dir: String::new(),
            status_url: "http://127.0.0.1:9109".into(),
            kdf_rpc_url: "http://127.0.0.1:7783".into(),
            kdf_dir: String::new(),
            kdf_exe: if cfg!(windows) { "kdf.exe".into() } else { "kdf".into() },
        }
    }
}

fn settings_file(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_config_dir().map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir.join("settings.json"))
}

/// Relative paths in settings are resolved against the workspace, never against the
/// process working directory (which is `src-tauri` under `tauri dev`).
fn normalize_settings(app: &AppHandle, mut s: Settings) -> Settings {
    let ws = setup::workspace_dir(app).unwrap_or_else(|_| PathBuf::from("."));
    let fix = |v: &mut String| {
        let p = Path::new(v.as_str());
        if !v.is_empty() && !p.is_absolute() && v.contains(['/', '\\', '.']) && !v.starts_with("http") {
            *v = ws.join(p).to_string_lossy().to_string();
        }
    };
    fix(&mut s.config_path);
    fix(&mut s.data_dir);
    fix(&mut s.kdf_dir);
    if s.bot_command.contains(['/', '\\']) {
        fix(&mut s.bot_command);
    }
    s
}

fn load_settings(app: &AppHandle) -> Settings {
    let s = settings_file(app)
        .ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| setup::default_settings(app));
    normalize_settings(app, s)
}

fn save_settings(app: &AppHandle, state: &AppState, settings: Settings) -> Result<(), String> {
    let settings = normalize_settings(app, settings);
    let path = settings_file(app)?;
    std::fs::write(&path, serde_json::to_string_pretty(&settings).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    *state.settings.lock().unwrap() = settings;
    Ok(())
}

// ------------------------------------------------------------------ process

struct ManagedProcess {
    child: Child,
    started: Instant,
}

#[derive(Default)]
struct LogBuffers {
    bot: VecDeque<String>,
    kdf: VecDeque<String>,
}

pub struct AppState {
    settings: Mutex<Settings>,
    bot: Mutex<Option<ManagedProcess>>,
    kdf: Mutex<Option<ManagedProcess>>,
    control_token: Mutex<Option<String>>,
    logs: Arc<Mutex<LogBuffers>>,
    pub(crate) http: reqwest::Client,
}

#[derive(Serialize)]
pub struct ProcessInfo {
    running: bool,
    pid: Option<u32>,
    uptime_seconds: Option<u64>,
    exit_code: Option<i32>,
}

fn process_info(slot: &Mutex<Option<ManagedProcess>>) -> ProcessInfo {
    let mut guard = slot.lock().unwrap();
    match guard.as_mut() {
        None => ProcessInfo { running: false, pid: None, uptime_seconds: None, exit_code: None },
        Some(p) => match p.child.try_wait() {
            Ok(Some(status)) => {
                let code = status.code();
                *guard = None;
                ProcessInfo { running: false, pid: None, uptime_seconds: None, exit_code: code }
            }
            _ => ProcessInfo {
                running: true,
                pid: Some(p.child.id()),
                uptime_seconds: Some(p.started.elapsed().as_secs()),
                exit_code: None,
            },
        },
    }
}

fn spawn_logged(
    app: &AppHandle,
    logs: Arc<Mutex<LogBuffers>>,
    which: &'static str,
    mut cmd: Command,
) -> Result<ManagedProcess, String> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("failed to start {which}: {e}"))?;
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    for (stream, tag) in [(stdout.map(|s| Box::new(s) as Box<dyn std::io::Read + Send>), "out"),
                          (stderr.map(|s| Box::new(s) as Box<dyn std::io::Read + Send>), "err")] {
        let Some(stream) = stream else { continue };
        let app = app.clone();
        let logs = logs.clone();
        std::thread::spawn(move || {
            let reader = BufReader::new(stream);
            for line in reader.lines().map_while(Result::ok) {
                let line = if tag == "err" { format!("[stderr] {line}") } else { line };
                {
                    let mut l = logs.lock().unwrap();
                    let buf = if which == "bot" { &mut l.bot } else { &mut l.kdf };
                    if buf.len() >= LOG_CAPACITY {
                        buf.pop_front();
                    }
                    buf.push_back(line.clone());
                }
                let _ = app.emit(&format!("{which}-log"), line);
            }
        });
    }
    let pid = child.id();
    // exit watcher
    {
        let app = app.clone();
        std::thread::spawn(move || {
            // Poll for exit without holding the state mutex; the process_info() call reaps it.
            loop {
                std::thread::sleep(Duration::from_millis(500));
                let state = app.state::<AppState>();
                let slot = if which == "bot" { &state.bot } else { &state.kdf };
                let info = process_info(slot);
                if !info.running {
                    let _ = app.emit(&format!("{which}-exit"), json!({"pid": pid, "code": info.exit_code}));
                    break;
                }
            }
        });
    }
    Ok(ManagedProcess { child, started: Instant::now() })
}

fn wait_exit(slot: &Mutex<Option<ManagedProcess>>, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if !process_info(slot).running {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

fn kill(slot: &Mutex<Option<ManagedProcess>>) {
    if let Some(p) = slot.lock().unwrap().as_mut() {
        let _ = p.child.kill();
        let _ = p.child.wait();
    }
    *slot.lock().unwrap() = None;
}

// ------------------------------------------------------------------ helpers

fn resolve(base: &str, path: &str) -> PathBuf {
    let p = Path::new(path);
    if p.is_absolute() || base.is_empty() {
        p.to_path_buf()
    } else {
        Path::new(base).join(p)
    }
}

fn config_dir_of(settings: &Settings) -> String {
    Path::new(&settings.config_path)
        .parent()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_default()
}

pub(crate) fn secret(key: &str) -> Result<Option<String>, String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, key).map_err(|e| e.to_string())?;
    match entry.get_password() {
        Ok(v) => Ok(Some(v)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

pub(crate) fn secret_store(key: &str, value: &str) -> Result<(), String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, key).map_err(|e| e.to_string())?;
    if value.is_empty() {
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(e.to_string()),
        }
    } else {
        entry.set_password(value).map_err(|e| e.to_string())
    }
}

// ------------------------------------------------------------------ commands: settings & secrets

#[tauri::command]
fn settings_get(state: State<AppState>) -> Settings {
    state.settings.lock().unwrap().clone()
}

#[tauri::command]
fn settings_set(app: AppHandle, state: State<AppState>, settings: Settings) -> Result<(), String> {
    save_settings(&app, &state, settings)
}

#[tauri::command]
fn secret_status(key: String) -> Result<bool, String> {
    Ok(secret(&key)?.is_some())
}

#[tauri::command]
fn secret_set(key: String, value: String) -> Result<(), String> {
    secret_store(&key, &value)
}

// ------------------------------------------------------------------ commands: setup wizard

#[tauri::command]
fn setup_status(app: AppHandle, state: State<AppState>) -> Result<setup::SetupStatus, String> {
    let settings = state.settings.lock().unwrap().clone();
    setup::status(&app, &settings)
}

#[tauri::command]
fn setup_init_workspace(app: AppHandle, state: State<AppState>) -> Result<Settings, String> {
    let settings = setup::init_workspace(&app)?;
    save_settings(&app, &state, settings.clone())?;
    Ok(settings)
}

#[tauri::command]
async fn setup_download_kdf(app: AppHandle, state: State<'_, AppState>) -> Result<String, String> {
    let dir = PathBuf::from(state.settings.lock().unwrap().kdf_dir.clone());
    setup::download_kdf(&app, &state, &dir).await
}

#[tauri::command]
async fn setup_download_coins(app: AppHandle, state: State<'_, AppState>) -> Result<String, String> {
    let dir = PathBuf::from(state.settings.lock().unwrap().kdf_dir.clone());
    setup::download_coins(&app, &state, &dir).await
}

#[tauri::command]
async fn setup_write_mm2(state: State<'_, AppState>, mode: String, passphrase: Option<String>) -> Result<setup::Mm2Summary, String> {
    let dir = PathBuf::from(state.settings.lock().unwrap().kdf_dir.clone());
    setup::write_mm2(&state, &dir, &mode, passphrase).await
}

#[tauri::command]
fn setup_finalize_wallet(state: State<AppState>) -> Result<bool, String> {
    let dir = PathBuf::from(state.settings.lock().unwrap().kdf_dir.clone());
    setup::finalize_wallet(&dir)
}

#[tauri::command]
async fn setup_latest_release(state: State<'_, AppState>) -> Result<Value, String> {
    setup::latest_release(&state).await
}

/// Reveal the wallet seed for backup. The wallet password never leaves the backend.
#[tauri::command]
async fn wallet_reveal_mnemonic(state: State<'_, AppState>) -> Result<String, String> {
    let password = secret("WALLET_PASSWORD")?.ok_or("no wallet password stored: the wallet was not created by this app")?;
    let v = kdf_rpc_inner(&state, "get_mnemonic", json!({ "format": "plaintext", "password": password }), true).await?;
    v.get("result").and_then(|r| r.get("mnemonic")).or_else(|| v.get("mnemonic"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| format!("unexpected get_mnemonic response: {v}"))
}

// ------------------------------------------------------------------ commands: bot process

#[tauri::command]
fn bot_start(app: AppHandle, state: State<AppState>, live_confirmation: Option<String>) -> Result<ProcessInfo, String> {
    if process_info(&state.bot).running {
        return Err("bot is already running".into());
    }
    let settings = state.settings.lock().unwrap().clone();
    let password = secret("KDF_RPC_PASSWORD")?.ok_or("KDF RPC password is not stored yet (Settings tab)")?;
    let token: String = rand::thread_rng().sample_iter(&Alphanumeric).take(40).map(char::from).collect();

    let mut cmd = Command::new(&settings.bot_command);
    cmd.arg("--config").arg(&settings.config_path).arg("--env-file").arg("");
    let cwd = config_dir_of(&settings);
    if !cwd.is_empty() {
        cmd.current_dir(&cwd);
    } else if let Ok(ws) = setup::workspace_dir(&app) {
        cmd.current_dir(ws);
    }
    cmd.env("KDF_RPC_PASSWORD", &password)
        .env("KDF_RPC_URL", &settings.kdf_rpc_url)
        .env("MM_BOT_CONTROL_TOKEN", &token)
        .env("PYTHONUNBUFFERED", "1")
        .env_remove("MM_BOT_CONFIRM_LIVE");
    if !settings.data_dir.is_empty() {
        cmd.env("MM_BOT_DATA_DIR", &settings.data_dir);
    }
    if live_confirmation.as_deref() == Some(LIVE_PHRASE) {
        cmd.env("MM_BOT_CONFIRM_LIVE", LIVE_PHRASE);
    }
    let proc = spawn_logged(&app, state.logs.clone(), "bot", cmd)?;
    *state.control_token.lock().unwrap() = Some(token);
    *state.bot.lock().unwrap() = Some(proc);
    Ok(process_info(&state.bot))
}

/// Wait for a managed process to exit without blocking the async runtime.
async fn wait_exit_async(app: AppHandle, which: &'static str, timeout: Duration) -> bool {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        let slot = if which == "bot" { &state.bot } else { &state.kdf };
        wait_exit(slot, timeout)
    })
    .await
    .unwrap_or(false)
}

#[tauri::command]
async fn bot_stop(app: AppHandle, state: State<'_, AppState>) -> Result<String, String> {
    if !process_info(&state.bot).running {
        return Ok("bot is not running".into());
    }
    // graceful: /control/stop lets the bot cancel its orders; hard kill only as a last resort
    let graceful = bot_control_inner(&state, "stop", None).await.is_ok();
    let exited = wait_exit_async(app, "bot", Duration::from_secs(if graceful { 60 } else { 5 })).await;
    if !exited {
        kill(&state.bot);
        return Ok("bot did not exit in time and was killed (orders may still be open in KDF)".into());
    }
    Ok(if graceful { "bot stopped gracefully".into() } else { "bot exited".into() })
}

#[tauri::command]
fn bot_process(state: State<AppState>) -> ProcessInfo {
    process_info(&state.bot)
}

async fn bot_control_inner(state: &AppState, action: &str, reason: Option<String>) -> Result<Value, String> {
    let (url, token) = {
        let s = state.settings.lock().unwrap();
        (format!("{}/control/{action}", s.status_url.trim_end_matches('/')),
         state.control_token.lock().unwrap().clone())
    };
    let token = token.ok_or("no control token: the bot was not started from this app")?;
    let resp = state
        .http
        .post(url)
        .bearer_auth(token)
        .json(&json!({ "reason": reason.unwrap_or_else(|| "desktop UI".into()) }))
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let body: Value = resp.json().await.unwrap_or(json!({}));
    if !status.is_success() {
        return Err(body.get("error").and_then(Value::as_str).unwrap_or("control request failed").to_string());
    }
    Ok(body)
}

#[tauri::command]
async fn bot_control(state: State<'_, AppState>, action: String, reason: Option<String>) -> Result<Value, String> {
    if !matches!(action.as_str(), "pause" | "resume" | "cancel_all" | "stop" | "clear_events") {
        return Err(format!("unknown action {action}"));
    }
    bot_control_inner(&state, &action, reason).await
}

#[tauri::command]
async fn bot_status(state: State<'_, AppState>) -> Result<Value, String> {
    let url = format!("{}/status", state.settings.lock().unwrap().status_url.trim_end_matches('/'));
    let process = process_info(&state.bot);
    let fetched = state.http.get(&url).timeout(Duration::from_secs(5)).send().await;
    let mut out = match fetched {
        Ok(resp) if resp.status().is_success() => {
            let mut v: Value = resp.json().await.map_err(|e| e.to_string())?;
            v["reachable"] = json!(true);
            v
        }
        Ok(resp) => json!({ "reachable": false, "error": format!("HTTP {}", resp.status()) }),
        Err(e) => json!({ "reachable": false, "error": e.to_string() }),
    };
    out["process"] = serde_json::to_value(process).map_err(|e| e.to_string())?;
    Ok(out)
}

#[tauri::command]
async fn bot_events(state: State<'_, AppState>, limit: Option<u32>) -> Result<Value, String> {
    let url = format!("{}/events?limit={}", state.settings.lock().unwrap().status_url.trim_end_matches('/'), limit.unwrap_or(100));
    let resp = state.http.get(&url).timeout(Duration::from_secs(5)).send().await.map_err(|e| e.to_string())?;
    resp.json().await.map_err(|e| e.to_string())
}

#[tauri::command]
fn bot_check_config(state: State<AppState>) -> Result<Value, String> {
    let settings = state.settings.lock().unwrap().clone();
    let mut cmd = Command::new(&settings.bot_command);
    cmd.arg("--check-config").arg("--config").arg(&settings.config_path).arg("--env-file").arg("");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    let out = cmd.output().map_err(|e| format!("cannot run {}: {e}", settings.bot_command))?;
    Ok(json!({
        "ok": out.status.success(),
        "output": format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr)),
    }))
}

// ------------------------------------------------------------------ commands: config files

#[tauri::command]
fn config_read(state: State<AppState>) -> Result<String, String> {
    let path = state.settings.lock().unwrap().config_path.clone();
    std::fs::read_to_string(&path).map_err(|e| format!("cannot read {path}: {e}"))
}

#[tauri::command]
fn config_write(state: State<AppState>, text: String) -> Result<(), String> {
    let path = state.settings.lock().unwrap().config_path.clone();
    if Path::new(&path).exists() {
        let _ = std::fs::copy(&path, format!("{path}.bak"));
    }
    std::fs::write(&path, text).map_err(|e| format!("cannot write {path}: {e}"))
}

#[tauri::command]
fn mm2_read(state: State<AppState>) -> Result<String, String> {
    let s = state.settings.lock().unwrap().clone();
    if s.kdf_dir.is_empty() {
        return Err("KDF folder is not set (Settings tab)".into());
    }
    let path = resolve(&s.kdf_dir, "MM2.json");
    std::fs::read_to_string(&path).map_err(|e| format!("cannot read {}: {e}", path.display()))
}

#[tauri::command]
fn mm2_write(state: State<AppState>, text: String) -> Result<(), String> {
    let s = state.settings.lock().unwrap().clone();
    if s.kdf_dir.is_empty() {
        return Err("KDF folder is not set (Settings tab)".into());
    }
    serde_json::from_str::<Value>(&text).map_err(|e| format!("MM2.json is not valid JSON: {e}"))?;
    let path = resolve(&s.kdf_dir, "MM2.json");
    if path.exists() {
        let _ = std::fs::copy(&path, path.with_extension("json.bak"));
    }
    std::fs::write(&path, text).map_err(|e| format!("cannot write {}: {e}", path.display()))
}

#[tauri::command]
fn kdf_files(state: State<AppState>) -> Result<Value, String> {
    let s = state.settings.lock().unwrap().clone();
    if s.kdf_dir.is_empty() {
        return Ok(json!({ "configured": false }));
    }
    let dir = Path::new(&s.kdf_dir);
    Ok(json!({
        "configured": true,
        "dir": s.kdf_dir,
        "exe": dir.join(&s.kdf_exe).exists(),
        "mm2_json": dir.join("MM2.json").exists(),
        "coins": dir.join("coins").exists(),
    }))
}

// ------------------------------------------------------------------ commands: KDF process & RPC

#[tauri::command]
fn kdf_start(app: AppHandle, state: State<AppState>) -> Result<ProcessInfo, String> {
    if process_info(&state.kdf).running {
        return Err("KDF is already running".into());
    }
    let s = state.settings.lock().unwrap().clone();
    if s.kdf_dir.is_empty() {
        return Err("KDF folder is not set (Settings tab)".into());
    }
    let exe = resolve(&s.kdf_dir, &s.kdf_exe);
    if !exe.exists() {
        return Err(format!("{} not found", exe.display()));
    }
    let mut cmd = Command::new(exe);
    cmd.current_dir(&s.kdf_dir);
    let proc = spawn_logged(&app, state.logs.clone(), "kdf", cmd)?;
    *state.kdf.lock().unwrap() = Some(proc);
    Ok(process_info(&state.kdf))
}

async fn kdf_rpc_inner(state: &AppState, method: &str, params: Value, v2: bool) -> Result<Value, String> {
    let allowed = if v2 { KDF_RPC_V2_ALLOWLIST } else { KDF_RPC_ALLOWLIST };
    if !allowed.contains(&method) {
        return Err(format!("RPC method {method} is not allowed from the UI"));
    }
    let password = secret("KDF_RPC_PASSWORD")?.ok_or("KDF RPC password is not stored yet (Settings tab)")?;
    let url = state.settings.lock().unwrap().kdf_rpc_url.clone();
    let body = if v2 {
        json!({ "userpass": password, "mmrpc": "2.0", "method": method, "params": params, "id": 1 })
    } else {
        let mut b = json!({ "userpass": password, "method": method });
        if let Value::Object(map) = params {
            for (k, v) in map {
                b[k] = v;
            }
        }
        b
    };
    let resp = state.http.post(&url).json(&body).timeout(Duration::from_secs(60)).send().await.map_err(|e| e.to_string())?;
    let status = resp.status();
    let text = resp.text().await.map_err(|e| e.to_string())?;
    let value: Value = serde_json::from_str(&text).unwrap_or(json!({ "raw": text }));
    if !status.is_success() || (v2 && value.get("error").is_some()) {
        return Err(value.get("error").and_then(Value::as_str).map(str::to_string).unwrap_or(format!("HTTP {status}")));
    }
    Ok(value)
}

#[tauri::command]
async fn kdf_rpc(state: State<'_, AppState>, method: String, params: Option<Value>) -> Result<Value, String> {
    kdf_rpc_inner(&state, &method, params.unwrap_or(json!({})), false).await
}

#[tauri::command]
async fn kdf_rpc_v2(state: State<'_, AppState>, method: String, params: Option<Value>) -> Result<Value, String> {
    kdf_rpc_inner(&state, &method, params.unwrap_or(json!({})), true).await
}

#[tauri::command]
async fn kdf_stop(app: AppHandle, state: State<'_, AppState>) -> Result<String, String> {
    let running = process_info(&state.kdf).running;
    // `stop` is KDF's own graceful shutdown RPC; it works for a node we did not spawn as well.
    let graceful = kdf_rpc_inner(&state, "stop", json!({}), false).await.is_ok();
    if !running {
        return Ok(if graceful { "stop sent to the external KDF node".into() } else { "KDF is not running".into() });
    }
    if !wait_exit_async(app, "kdf", Duration::from_secs(if graceful { 30 } else { 3 })).await {
        kill(&state.kdf);
        return Ok("KDF did not exit in time and was killed".into());
    }
    Ok("KDF stopped".into())
}

#[tauri::command]
fn kdf_process(state: State<AppState>) -> ProcessInfo {
    process_info(&state.kdf)
}

// ------------------------------------------------------------------ commands: logs

#[tauri::command]
fn logs_get(state: State<AppState>, which: String) -> Vec<String> {
    let l = state.logs.lock().unwrap();
    let buf = if which == "kdf" { &l.kdf } else { &l.bot };
    buf.iter().cloned().collect()
}

#[tauri::command]
fn logs_clear(state: State<AppState>, which: String) {
    let mut l = state.logs.lock().unwrap();
    if which == "kdf" { l.kdf.clear() } else { l.bot.clear() }
}

#[tauri::command]
fn live_phrase() -> &'static str {
    LIVE_PHRASE
}

#[tauri::command]
fn app_version(app: AppHandle) -> String {
    app.package_info().version.to_string()
}

// ------------------------------------------------------------------ app

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let settings = load_settings(app.handle());
            app.manage(AppState {
                settings: Mutex::new(settings),
                bot: Mutex::new(None),
                kdf: Mutex::new(None),
                control_token: Mutex::new(None),
                logs: Arc::new(Mutex::new(LogBuffers::default())),
                http: reqwest::Client::new(),
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window stops the bot gracefully (orders cancelled) and then the KDF
            // node we started (KDF `stop` RPC), so nothing is left running or hard-killed.
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                let state = window.state::<AppState>();
                let (status_url, rpc_url, token) = {
                    let s = state.settings.lock().unwrap();
                    (s.status_url.trim_end_matches('/').to_string(), s.kdf_rpc_url.clone(),
                     state.control_token.lock().unwrap().clone())
                };
                if process_info(&state.bot).running {
                    if let Some(token) = token {
                        // reqwest::blocking must not run on the async runtime thread: use a helper thread.
                        let url = format!("{status_url}/control/stop");
                        let _ = std::thread::spawn(move || {
                            reqwest::blocking::Client::new()
                                .post(url)
                                .bearer_auth(token)
                                .timeout(Duration::from_secs(5))
                                .send()
                                .map(|_| ())
                                .map_err(|e| e.to_string())
                        })
                        .join();
                        wait_exit(&state.bot, Duration::from_secs(45));
                    }
                    kill(&state.bot);
                }
                if process_info(&state.kdf).running {
                    if let Ok(Some(password)) = secret("KDF_RPC_PASSWORD") {
                        let _ = std::thread::spawn(move || {
                            reqwest::blocking::Client::new()
                                .post(rpc_url)
                                .json(&json!({ "userpass": password, "method": "stop" }))
                                .timeout(Duration::from_secs(5))
                                .send()
                                .map(|_| ())
                                .map_err(|e| e.to_string())
                        })
                        .join();
                        wait_exit(&state.kdf, Duration::from_secs(30));
                    }
                    kill(&state.kdf);
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            settings_get,
            settings_set,
            secret_status,
            secret_set,
            bot_start,
            bot_stop,
            bot_process,
            bot_control,
            bot_status,
            bot_events,
            bot_check_config,
            config_read,
            config_write,
            mm2_read,
            mm2_write,
            kdf_files,
            kdf_start,
            kdf_stop,
            kdf_process,
            kdf_rpc,
            kdf_rpc_v2,
            logs_get,
            logs_clear,
            live_phrase,
            app_version,
            setup_status,
            setup_init_workspace,
            setup_download_kdf,
            setup_download_coins,
            setup_write_mm2,
            setup_finalize_wallet,
            setup_latest_release,
            wallet_reveal_mnemonic,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
