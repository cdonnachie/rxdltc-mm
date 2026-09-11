//! First-run setup: workspace, pinned KDF download, coins file, MM2.json and
//! config generation. Everything is pinned to versions and hashes that ship
//! with this app so the wizard never trusts "latest" blindly.

use std::io::Read;
use std::path::{Path, PathBuf};

use rand::seq::SliceRandom;
use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager};

use crate::{secret, secret_store, AppState, Settings};

/// The bot config shipped with the repo; written to the workspace on first run.
pub const CONFIG_EXAMPLE: &str = include_str!("../../../config.example.yaml");

pub const KDF_RELEASE_TAG: &str = "v3.0.0-beta";
pub const KDF_RELEASE_COMMIT: &str = "d56a7bc";
pub const KDF_REPO: &str = "GLEECBTC/komodo-defi-framework";

pub struct KdfAsset {
    pub file: &'static str,
    pub sha256: &'static str,
    pub inner: &'static str, // file name inside the zip
}

/// Pinned per-platform release assets (hashes computed from the published zips on 2026-09-11).
pub fn kdf_asset() -> Option<KdfAsset> {
    if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        Some(KdfAsset {
            file: "kdf_d56a7bc-win-x86-64.zip",
            sha256: "4839a22f8722e5ab9b249c2e0d602659e74c45dfcac7b0345729aaf4738591b2",
            inner: "kdf.exe",
        })
    } else if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
        Some(KdfAsset {
            file: "kdf_d56a7bc-linux-x86-64.zip",
            sha256: "20016e48158cc3926febb37bb2c51bce713eb46f53b0be6790ab132ceec2d501",
            inner: "kdf",
        })
    } else {
        None // v3.0.0-beta has no macOS asset; the user points the app at their own binary
    }
}

pub const COINS_COMMIT: &str = "846c222a9d1b4559d2f227e4fd318a69b3656d87";
pub const GLEEC_NETID: u64 = 6133;
/// Fallback if seed-nodes.json cannot be fetched (netid 6133 entries as of 2026-09-11).
pub const SEEDNODES_FALLBACK: &[&str] = &[
    "staking1.gleec.com",
    "staking2.gleec.com",
    "kdfseed1.decker.im",
    "seed01.kmdefi.net",
    "seed03.kmdefi.net",
];
pub const WALLET_NAME: &str = "rxdltc-mm";

fn coins_url() -> String {
    format!("https://raw.githubusercontent.com/GLEECBTC/coins/{COINS_COMMIT}/coins")
}

fn seednodes_url() -> String {
    format!("https://raw.githubusercontent.com/GLEECBTC/coins/{COINS_COMMIT}/seed-nodes.json")
}

// ------------------------------------------------------------------ workspace & defaults

pub fn workspace_dir(app: &AppHandle) -> Result<PathBuf, String> {
    app.path().app_local_data_dir().map_err(|e| e.to_string())
}

pub fn kdf_exe_name() -> &'static str {
    if cfg!(windows) { "kdf.exe" } else { "kdf" }
}

/// The bundled bot executable, if this build ships one (Tauri places sidecars next to the app exe).
pub fn sidecar_path() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    let name = if cfg!(windows) { "rxdltc-mm.exe" } else { "rxdltc-mm" };
    let p = dir.join(name);
    p.exists().then_some(p)
}

pub fn default_settings(app: &AppHandle) -> Settings {
    let ws = workspace_dir(app).unwrap_or_else(|_| PathBuf::from("."));
    Settings {
        bot_command: sidecar_path().map(|p| p.to_string_lossy().to_string()).unwrap_or_else(|| "rxdltc-mm".into()),
        config_path: ws.join("config.yaml").to_string_lossy().to_string(),
        data_dir: ws.join("data").to_string_lossy().to_string(),
        status_url: "http://127.0.0.1:9109".into(),
        kdf_rpc_url: "http://127.0.0.1:7783".into(),
        kdf_dir: ws.join("kdf").to_string_lossy().to_string(),
        kdf_exe: kdf_exe_name().into(),
    }
}

// ------------------------------------------------------------------ status

#[derive(Serialize)]
pub struct SetupStatus {
    pub workspace: String,
    pub config_exists: bool,
    pub bot_command: String,
    pub bot_command_exists: bool,
    pub bundled_bot: bool,
    pub kdf_dir: String,
    pub kdf_exe_exists: bool,
    pub kdf_download_available: bool,
    pub kdf_release: String,
    pub coins_exists: bool,
    pub mm2_exists: bool,
    pub mm2_wallet_mode: Option<String>, // "encrypted" | "plaintext" | null
    pub mm2_has_plaintext_passphrase: bool,
    pub rpc_password_stored: bool,
    pub wallet_password_stored: bool,
    pub complete: bool,
}

fn command_exists(cmd: &str) -> bool {
    let p = Path::new(cmd);
    if p.is_absolute() || cmd.contains(['/', '\\']) {
        return p.exists();
    }
    // bare command: look it up on PATH
    let exts: Vec<String> = if cfg!(windows) { vec!["".into(), ".exe".into(), ".cmd".into()] } else { vec!["".into()] };
    std::env::var_os("PATH")
        .map(|paths| {
            std::env::split_paths(&paths).any(|dir| exts.iter().any(|e| dir.join(format!("{cmd}{e}")).exists()))
        })
        .unwrap_or(false)
}

pub fn status(app: &AppHandle, settings: &Settings) -> Result<SetupStatus, String> {
    let ws = workspace_dir(app)?;
    let kdf_dir = PathBuf::from(&settings.kdf_dir);
    let mm2_path = kdf_dir.join("MM2.json");
    let mm2: Option<Value> = std::fs::read_to_string(&mm2_path).ok().and_then(|s| serde_json::from_str(&s).ok());
    let has_pass = mm2.as_ref().map(|m| m.get("passphrase").is_some()).unwrap_or(false);
    let has_wallet = mm2.as_ref().map(|m| m.get("wallet_name").is_some() && m.get("wallet_password").is_some()).unwrap_or(false);
    let bot_exists = command_exists(&settings.bot_command);
    let kdf_exists = kdf_dir.join(&settings.kdf_exe).exists();
    let coins_exists = kdf_dir.join("coins").exists();
    let config_exists = Path::new(&settings.config_path).exists();
    let rpc_pw = secret("KDF_RPC_PASSWORD").ok().flatten().is_some();
    let wallet_pw = secret("WALLET_PASSWORD").ok().flatten().is_some();
    Ok(SetupStatus {
        workspace: ws.to_string_lossy().to_string(),
        config_exists,
        bot_command: settings.bot_command.clone(),
        bot_command_exists: bot_exists,
        bundled_bot: sidecar_path().is_some(),
        kdf_dir: settings.kdf_dir.clone(),
        kdf_exe_exists: kdf_exists,
        kdf_download_available: kdf_asset().is_some(),
        kdf_release: format!("{KDF_RELEASE_TAG} ({KDF_RELEASE_COMMIT})"),
        coins_exists,
        mm2_exists: mm2.is_some(),
        mm2_wallet_mode: mm2.as_ref().map(|_| if has_wallet { "encrypted".into() } else { "plaintext".into() }),
        mm2_has_plaintext_passphrase: has_pass,
        rpc_password_stored: rpc_pw,
        wallet_password_stored: wallet_pw,
        complete: config_exists && bot_exists && kdf_exists && coins_exists && mm2.is_some() && rpc_pw,
    })
}

// ------------------------------------------------------------------ steps

fn progress(app: &AppHandle, step: &str, message: impl Into<String>) {
    let _ = app.emit("setup-progress", json!({ "step": step, "message": message.into() }));
}

/// Create the workspace folders and a config.yaml from the embedded example (never overwrites).
pub fn init_workspace(app: &AppHandle) -> Result<Settings, String> {
    let ws = workspace_dir(app)?;
    for sub in ["kdf", "data"] {
        std::fs::create_dir_all(ws.join(sub)).map_err(|e| e.to_string())?;
    }
    let cfg = ws.join("config.yaml");
    if !cfg.exists() {
        std::fs::write(&cfg, CONFIG_EXAMPLE).map_err(|e| e.to_string())?;
    }
    Ok(default_settings(app))
}

pub async fn download_kdf(app: &AppHandle, state: &AppState, kdf_dir: &Path) -> Result<String, String> {
    let asset = kdf_asset().ok_or("no pinned KDF build for this platform; place the binary manually")?;
    let url = format!("https://github.com/{KDF_REPO}/releases/download/{KDF_RELEASE_TAG}/{}", asset.file);
    progress(app, "kdf", format!("downloading {} …", asset.file));
    let bytes = state
        .http
        .get(&url)
        .timeout(std::time::Duration::from_secs(600))
        .send()
        .await
        .map_err(|e| e.to_string())?
        .error_for_status()
        .map_err(|e| e.to_string())?
        .bytes()
        .await
        .map_err(|e| e.to_string())?;
    progress(app, "kdf", format!("verifying SHA-256 of {} bytes …", bytes.len()));
    let digest = hex::encode(Sha256::digest(&bytes));
    if digest != asset.sha256 {
        return Err(format!("checksum mismatch for {}: got {digest}, expected {}. Download refused.", asset.file, asset.sha256));
    }
    progress(app, "kdf", "extracting …");
    let cursor = std::io::Cursor::new(bytes.to_vec());
    let mut archive = zip::ZipArchive::new(cursor).map_err(|e| e.to_string())?;
    let mut entry = archive.by_name(asset.inner).map_err(|e| format!("{} not found in zip: {e}", asset.inner))?;
    let mut data = Vec::with_capacity(entry.size() as usize);
    entry.read_to_end(&mut data).map_err(|e| e.to_string())?;
    std::fs::create_dir_all(kdf_dir).map_err(|e| e.to_string())?;
    let target = kdf_dir.join(kdf_exe_name());
    std::fs::write(&target, &data).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o755)).map_err(|e| e.to_string())?;
    }
    progress(app, "kdf", "done");
    Ok(format!("{KDF_RELEASE_TAG} ({KDF_RELEASE_COMMIT}) verified sha256 {}", &asset.sha256[..16]))
}

pub async fn download_coins(app: &AppHandle, state: &AppState, kdf_dir: &Path) -> Result<String, String> {
    progress(app, "coins", "downloading coins file …");
    let text = state.http.get(coins_url()).send().await.map_err(|e| e.to_string())?
        .error_for_status().map_err(|e| e.to_string())?
        .text().await.map_err(|e| e.to_string())?;
    let parsed: Value = serde_json::from_str(&text).map_err(|e| format!("coins file is not valid JSON: {e}"))?;
    let coins = parsed.as_array().ok_or("coins file is not a JSON array")?;
    let has = |t: &str| coins.iter().any(|c| c.get("coin").and_then(Value::as_str) == Some(t));
    for needed in ["RXD", "LTC", "LTC-segwit"] {
        if !has(needed) {
            return Err(format!("coins file does not contain {needed}"));
        }
    }
    std::fs::create_dir_all(kdf_dir).map_err(|e| e.to_string())?;
    std::fs::write(kdf_dir.join("coins"), text).map_err(|e| e.to_string())?;
    progress(app, "coins", "done");
    Ok(format!("{} coins, commit {}", coins.len(), &COINS_COMMIT[..8]))
}

async fn seednodes(state: &AppState) -> Vec<String> {
    let fetched: Option<Vec<String>> = async {
        let v: Value = state.http.get(seednodes_url()).send().await.ok()?.json().await.ok()?;
        let hosts: Vec<String> = v.as_array()?.iter()
            .filter(|s| s.get("netid").and_then(Value::as_u64) == Some(GLEEC_NETID))
            .filter_map(|s| s.get("host").and_then(Value::as_str).map(str::to_string))
            .collect();
        (!hosts.is_empty()).then_some(hosts)
    }.await;
    fetched.unwrap_or_else(|| SEEDNODES_FALLBACK.iter().map(|s| s.to_string()).collect())
}

/// Random password satisfying KDF's policy: 8+ chars, upper, lower, digit, special, no char
/// three times in a row, does not contain "password".
pub fn generate_password(len: usize) -> String {
    const UPPER: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZ";
    const LOWER: &[u8] = b"abcdefghijkmnopqrstuvwxyz";
    const DIGIT: &[u8] = b"23456789";
    const SPECIAL: &[u8] = b"!#$%&*+-=?@^_";
    let mut rng = rand::thread_rng();
    loop {
        let mut chars: Vec<u8> = vec![
            *UPPER.choose(&mut rng).unwrap(),
            *LOWER.choose(&mut rng).unwrap(),
            *DIGIT.choose(&mut rng).unwrap(),
            *SPECIAL.choose(&mut rng).unwrap(),
        ];
        let all: Vec<u8> = [UPPER, LOWER, DIGIT, SPECIAL].concat();
        while chars.len() < len.max(12) {
            chars.push(*all.choose(&mut rng).unwrap());
        }
        chars.shuffle(&mut rng);
        let s = String::from_utf8(chars).unwrap();
        let triple = s.as_bytes().windows(3).any(|w| w[0] == w[1] && w[1] == w[2]);
        if !triple && !s.to_lowercase().contains("password") {
            return s;
        }
    }
}

#[derive(Serialize)]
pub struct Mm2Summary {
    pub path: String,
    pub netid: u64,
    pub seednodes: Vec<String>,
    pub wallet_mode: String,
    pub rpc_password_generated: bool,
}

/// Write MM2.json. `mode`: "create" (KDF generates and encrypts a new seed under wallet_name /
/// wallet_password), "import" (passphrase written for the first start only; removed by
/// `finalize_wallet` once KDF has encrypted and stored it).
pub async fn write_mm2(state: &AppState, kdf_dir: &Path, mode: &str, passphrase: Option<String>) -> Result<Mm2Summary, String> {
    let path = kdf_dir.join("MM2.json");
    if path.exists() {
        return Err("MM2.json already exists; delete it (or use the KDF tab to edit it) before generating a new one".into());
    }
    let rpc_password = match secret("KDF_RPC_PASSWORD")? {
        Some(p) => p,
        None => {
            let p = generate_password(24);
            secret_store("KDF_RPC_PASSWORD", &p)?;
            p
        }
    };
    let wallet_password = match secret("WALLET_PASSWORD")? {
        Some(p) => p,
        None => {
            let p = generate_password(24);
            secret_store("WALLET_PASSWORD", &p)?;
            p
        }
    };
    let nodes = seednodes(state).await;
    let mut conf = json!({
        "gui": "rxdltc-mm",
        "netid": GLEEC_NETID,
        "seednodes": nodes,
        "rpcip": "127.0.0.1",
        "rpcport": 7783,
        "rpc_password": rpc_password,
        "wallet_name": WALLET_NAME,
        "wallet_password": wallet_password,
        "dbdir": "./DB",
        "i_am_seed": false
    });
    match mode {
        "create" => {}
        "import" => {
            let words = passphrase.unwrap_or_default();
            let n = words.split_whitespace().count();
            if n != 12 && n != 24 {
                return Err(format!("a seed phrase has 12 or 24 words, got {n}"));
            }
            conf["passphrase"] = json!(words.split_whitespace().collect::<Vec<_>>().join(" "));
        }
        other => return Err(format!("unknown wallet mode {other}")),
    }
    std::fs::create_dir_all(kdf_dir).map_err(|e| e.to_string())?;
    std::fs::write(&path, serde_json::to_string_pretty(&conf).unwrap()).map_err(|e| e.to_string())?;
    restrict_permissions(&path);
    Ok(Mm2Summary {
        path: path.to_string_lossy().to_string(),
        netid: GLEEC_NETID,
        seednodes: conf["seednodes"].as_array().unwrap().iter().filter_map(Value::as_str).map(str::to_string).collect(),
        wallet_mode: mode.to_string(),
        rpc_password_generated: true,
    })
}

/// After KDF has started once with an imported passphrase it has encrypted and stored the seed
/// under wallet_name; the plaintext can be removed from MM2.json.
pub fn finalize_wallet(kdf_dir: &Path) -> Result<bool, String> {
    let path = kdf_dir.join("MM2.json");
    let text = std::fs::read_to_string(&path).map_err(|e| e.to_string())?;
    let mut conf: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    let had = conf.as_object_mut().map(|m| m.remove("passphrase").is_some()).unwrap_or(false);
    if had {
        std::fs::write(&path, serde_json::to_string_pretty(&conf).unwrap()).map_err(|e| e.to_string())?;
    }
    Ok(had)
}

fn restrict_permissions(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(windows)]
    {
        // Best effort: remove inherited ACEs and grant the current user only.
        let _ = std::process::Command::new("icacls")
            .args([path.as_os_str().to_str().unwrap_or(""), "/inheritance:r", "/grant:r", &format!("{}:F", std::env::var("USERNAME").unwrap_or_default())])
            .output();
    }
}

/// Latest release tag on GitHub (informational; never auto-installed).
pub async fn latest_release(state: &AppState) -> Result<Value, String> {
    let v: Value = state
        .http
        .get(format!("https://api.github.com/repos/{KDF_REPO}/releases/latest"))
        .header("User-Agent", "rxdltc-mm-ui")
        .header("Accept", "application/vnd.github+json")
        .send().await.map_err(|e| e.to_string())?
        .error_for_status().map_err(|e| e.to_string())?
        .json().await.map_err(|e| e.to_string())?;
    let tag = v.get("tag_name").and_then(Value::as_str).unwrap_or("?").to_string();
    Ok(json!({
        "latest": tag,
        "pinned": KDF_RELEASE_TAG,
        "newer": tag != KDF_RELEASE_TAG,
        "url": v.get("html_url").and_then(Value::as_str).unwrap_or(""),
        "published_at": v.get("published_at").and_then(Value::as_str).unwrap_or(""),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy_ok(p: &str) -> bool {
        p.len() >= 8
            && p.chars().any(|c| c.is_ascii_uppercase())
            && p.chars().any(|c| c.is_ascii_lowercase())
            && p.chars().any(|c| c.is_ascii_digit())
            && p.chars().any(|c| !c.is_ascii_alphanumeric())
            && !p.as_bytes().windows(3).any(|w| w[0] == w[1] && w[1] == w[2])
            && !p.to_lowercase().contains("password")
    }

    #[test]
    fn generated_passwords_satisfy_kdf_policy() {
        for _ in 0..200 {
            let p = generate_password(24);
            assert_eq!(p.len(), 24);
            assert!(policy_ok(&p), "{p}");
        }
        assert!(generate_password(4).len() >= 12);
    }

    #[test]
    fn pinned_asset_is_consistent() {
        if let Some(a) = kdf_asset() {
            assert_eq!(a.sha256.len(), 64);
            assert!(a.file.contains(KDF_RELEASE_COMMIT));
            assert!(a.inner == "kdf.exe" || a.inner == "kdf");
        }
        assert_eq!(COINS_COMMIT.len(), 40);
        assert!(!SEEDNODES_FALLBACK.is_empty());
        assert!(CONFIG_EXAMPLE.contains("dry_run: true"));
        assert!(CONFIG_EXAMPLE.contains("quote: LTC-segwit"));
    }

    #[test]
    fn finalize_removes_only_passphrase() {
        let dir = std::env::temp_dir().join(format!("rxdltc-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("MM2.json");
        std::fs::write(&path, r#"{"gui":"x","passphrase":"a b c","wallet_name":"w","wallet_password":"p"}"#).unwrap();
        assert!(finalize_wallet(&dir).unwrap());
        let v: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert!(v.get("passphrase").is_none());
        assert_eq!(v["wallet_name"], "w");
        assert!(!finalize_wallet(&dir).unwrap());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
