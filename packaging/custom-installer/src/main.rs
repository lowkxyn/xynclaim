#![windows_subsystem = "windows"]

use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    env, fs,
    io::Write,
    os::windows::{fs::MetadataExt, process::CommandExt},
    path::{Component, Path, PathBuf},
    process::{Command, ExitCode},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::{Duration, SystemTime},
};
use tao::{
    dpi::LogicalSize,
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoopBuilder, EventLoopProxy},
    platform::windows::{IconExtWindows, WindowExtWindows},
    window::{Icon, WindowBuilder},
};
use tempfile::TempDir;
use winreg::{enums::HKEY_CURRENT_USER, RegKey};
use wry::{WebContext, WebViewBuilder};

const CREATE_NO_WINDOW: u32 = 0x08000000;
const SETUP: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/xyn-payload.exe"));
const HTML: &str = include_str!(concat!(env!("OUT_DIR"), "/ui.html"));
const APP_JSON: &str = include_str!("../app.json");
const PAYLOAD: &str = include_str!(concat!(env!("OUT_DIR"), "/payload.json"));

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct App {
    name: String,
    exe: String,
    registry_key: String,
    uninstall_key: String,
}

#[derive(Clone, Deserialize)]
struct PayloadFile {
    path: String,
    size: u64,
}

#[derive(Debug)]
enum UiEvent {
    Request(Value),
    Update(Value),
    Complete(Result<PathBuf, String>),
}

fn message(text: &str, name: &str) {
    let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
    unsafe {
        windows_sys::Win32::UI::WindowsAndMessaging::MessageBoxW(
            std::ptr::null_mut(),
            wide(text).as_ptr(),
            wide(name).as_ptr(),
            windows_sys::Win32::UI::WindowsAndMessaging::MB_OK
                | windows_sys::Win32::UI::WindowsAndMessaging::MB_ICONEXCLAMATION,
        );
    }
}

fn installed_folder(app: &App) -> Option<PathBuf> {
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    hkcu.open_subkey(&app.registry_key)
        .ok()?
        .get_value::<String, _>("InstallDir")
        .ok()
        .map(PathBuf::from)
        .filter(|p| p.join(&app.exe).is_file())
}

// No shell is involved. NSIS requires the unquoted /D= remainder LAST, including spaces.
fn installer_command(
    exe: &Path,
    folder: &Path,
    desktop: bool,
    start_menu: bool,
    progress: &Path,
) -> Command {
    let mut cmd = Command::new(exe);
    cmd.creation_flags(CREATE_NO_WINDOW).arg("/S");
    if !desktop {
        cmd.arg("/NoDesktop");
    }
    if !start_menu {
        cmd.arg("/NoStartMenu");
    }
    cmd.arg(format!("/ProgressFile={}", progress.display()));
    cmd.raw_arg(format!("/D={}", folder.display()));
    cmd
}

fn validate_folder(text: &str, app: &App, local: &Path) -> Result<PathBuf, String> {
    let expanded = if text.to_uppercase().starts_with("%LOCALAPPDATA%\\") {
        local.join(&text[15..])
    } else {
        PathBuf::from(text)
    };
    let raw = expanded.to_string_lossy();
    if raw.contains(['"', '%', '<', '>', '|', '?', '*'])
        || raw.chars().any(|c| c.is_control())
        || !expanded.is_absolute()
        || raw.starts_with("\\\\")
        || raw.contains('/')
        || raw[2..].contains(':')
        || expanded
            .components()
            .any(|c| matches!(c, Component::ParentDir))
        || raw.split('\\').any(|p| p.ends_with([' ', '.']))
    {
        return Err("Choose a full local folder path, such as C:\\Apps\\YourApp. Quotes, network paths and relative paths aren't supported.".into());
    }
    let lower = raw.trim_end_matches('\\').to_lowercase();
    let protected = [
        Some(local.to_path_buf()),
        env::var_os("USERPROFILE").map(PathBuf::from),
        env::var_os("ProgramFiles").map(PathBuf::from),
    ];
    let blocked_trees = [
        Some(local.join(&app.name)),
        env::var_os("WINDIR").map(PathBuf::from),
    ];
    if expanded.parent().is_none()
        || expanded.components().count() < 3
        || protected.into_iter().flatten().any(|p| {
            let p = p.to_string_lossy().to_lowercase();
            lower == p
        })
        || blocked_trees.into_iter().flatten().any(|p| {
            let p = p.to_string_lossy().to_lowercase();
            lower == p || lower.starts_with(&(p + "\\"))
        })
    {
        return Err("Choose a dedicated application folder. This location contains Windows, account or application data.".into());
    }
    let internal = expanded.join("_internal");
    for ancestor in internal.ancestors() {
        if let Ok(meta) = fs::symlink_metadata(ancestor) {
            if meta.file_attributes() & 0x400 != 0 {
                return Err("Choose a folder without junctions or symbolic links.".into());
            }
        }
    }
    if expanded.exists() {
        let mut entries =
            fs::read_dir(&expanded).map_err(|e| format!("Can't read the install folder: {e}"))?;
        if entries.next().is_some() && !expanded.join(&app.exe).is_file() {
            return Err(format!(
                "Choose an empty folder or an existing {} installation.",
                app.name
            ));
        }
    }
    Ok(expanded)
}

fn fingerprint(path: &Path) -> Option<(u64, SystemTime)> {
    let meta = fs::symlink_metadata(path).ok()?;
    if !meta.is_file() || meta.file_attributes() & 0x400 != 0 {
        return None;
    }
    Some((meta.len(), meta.modified().ok()?))
}

fn payload_bytes(folder: &Path, payload: &[PayloadFile]) -> u64 {
    payload
        .iter()
        .map(|file| {
            let Some(stamp) = fingerprint(&folder.join(&file.path)) else {
                return 0;
            };
            stamp.0.min(file.size)
        })
        .sum()
}

fn extract_setup(temp: &TempDir) -> Result<PathBuf, String> {
    let path = temp.path().join("xyn-payload.exe");
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|e| format!("Couldn't prepare setup: {e}"))?;
    file.write_all(SETUP)
        .map_err(|e| format!("Couldn't extract setup: {e}"))?;
    file.sync_all().map_err(|e| e.to_string())?;
    Ok(path)
}

fn clean_profile(profile: TempDir) {
    // Only the exact directory created by tempfile is eligible, never a caller path.
    let path = profile.path().to_path_buf();
    if profile.close().is_err() {
        // WebView2's child process can briefly keep cache files open after controller close.
        for _ in 0..20 {
            thread::sleep(Duration::from_millis(100));
            if !path.exists() || fs::remove_dir_all(&path).is_ok() {
                break;
            }
        }
    }
}

fn run_install(
    folder: PathBuf,
    desktop: bool,
    start_menu: bool,
    proxy: &EventLoopProxy<UiEvent>,
    app: &App,
) -> Result<PathBuf, String> {
    let temp = tempfile::Builder::new()
        .prefix("xyn-setup-")
        .tempdir()
        .map_err(|e| e.to_string())?;
    let setup = extract_setup(&temp)?;
    let payload: Vec<PayloadFile> = serde_json::from_str(PAYLOAD).map_err(|e| e.to_string())?;
    let total: u64 = payload.iter().map(|f| f.size).sum();
    let progress = temp.path().join("progress.txt");
    let mut child = installer_command(&setup, &folder, desktop, start_menu, &progress)
        .spawn()
        .map_err(|e| format!("Couldn't start setup: {e}"))?;
    let mut pct = 0.0_f64;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    let code = status.code().unwrap_or(-1);
                    let hint = match code {
                        10 => format!("Close {} and try again.", app.name),
                        2 => "Setup couldn't write all files or shortcuts. Check permissions and free disk space, then retry.".into(),
                        1 => "Setup was cancelled or couldn't complete. Check the folder and try again.".into(),
                        _ => "Check permissions, disk space and whether the app is closed, then try again.".into(),
                    };
                    return Err(format!("Setup exited with code {code}. {hint}"));
                }
                // Success must mean the actual expected payload exists at the expected lengths.
                if !payload
                    .iter()
                    .all(|f| fingerprint(&folder.join(&f.path)).is_some_and(|m| m.0 == f.size))
                {
                    return Err("Setup returned success but some installed files are missing or incomplete. Retry setup.".into());
                }
                let registered = RegKey::predef(HKEY_CURRENT_USER)
                    .open_subkey(&app.registry_key)
                    .and_then(|k| k.get_value::<String, _>("InstallDir"))
                    .map(PathBuf::from);
                if registered.as_ref().ok() != Some(&folder)
                    || !folder.join("Uninstall.exe").is_file()
                {
                    return Err("Setup returned success but installation registration is incomplete. Retry setup.".into());
                }
                if RegKey::predef(HKEY_CURRENT_USER)
                    .open_subkey(&app.uninstall_key)
                    .is_err()
                {
                    return Err("Setup returned success but uninstall registration is missing. Retry setup.".into());
                }
                let _ = proxy.send_event(UiEvent::Update(
                    json!({"kind":"progress","pct":100,"step":"Finishing","detail":""}),
                ));
                thread::sleep(Duration::from_millis(400));
                return Ok(folder);
            }
            Err(e) => {
                // Preserve ownership until the installer has exited; never delete its extracted exe while running.
                let _ = child.wait();
                return Err(format!("Couldn't read the setup result: {e}"));
            }
            Ok(None) => {}
        }
        let copying = fs::read_to_string(&progress).is_ok_and(|s| s == "copying");
        let bytes = if copying {
            payload_bytes(&folder, &payload)
        } else {
            0
        };
        pct = pct.max((bytes as f64 / total.max(1) as f64 * 98.0).min(98.0));
        let _ = proxy.send_event(UiEvent::Update(json!({"kind":"progress","pct":pct,
            "step": if !copying {"Preparing"} else if pct >= 98.0 {"Finishing"} else {"Installing files"},
            "detail":format!("{:.1} / {:.1} MB", bytes as f64 / 1048576.0, total as f64 / 1048576.0)})));
        thread::sleep(Duration::from_millis(200));
    }
}

fn dry_install(proxy: EventLoopProxy<UiEvent>, folder: PathBuf, fail: bool) {
    for pct in 0..=100 {
        let _ = proxy.send_event(UiEvent::Update(json!({"kind":"progress","pct":pct,
            "step":if pct < 8 {"Preparing"} else if pct < 95 {"Installing files"} else {"Finishing"},
            "detail":if pct < 95 {"Preview only"} else {"Writing uninstall information"}})));
        thread::sleep(Duration::from_millis(100));
    }
    let result = if fail {
        Err(
            "Preview: setup exited with code 2. Check permissions and free disk space, then retry."
                .into(),
        )
    } else {
        Ok(folder)
    };
    let _ = proxy.send_event(UiEvent::Complete(result));
}

fn fallback(app: &App, dry: bool) -> ExitCode {
    if dry {
        message(
            "WebView2 isn't available. Dry-run stopped without starting the NSIS installer.",
            &app.name,
        );
        return ExitCode::FAILURE;
    }
    message(
        "The custom setup couldn't open WebView2. The standard setup will open instead.",
        &app.name,
    );
    let result = (|| -> Result<bool, String> {
        let temp = tempfile::Builder::new()
            .prefix("xyn-setup-")
            .tempdir()
            .map_err(|e| e.to_string())?;
        let setup = extract_setup(&temp)?;
        let status = Command::new(setup)
            .creation_flags(CREATE_NO_WINDOW)
            .status()
            .map_err(|e| e.to_string())?;
        Ok(status.success())
    })();
    match result {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(e) => {
            message(&e, &app.name);
            ExitCode::FAILURE
        }
    }
}

fn main() -> ExitCode {
    let app: App = serde_json::from_str(APP_JSON).unwrap();
    let args: Vec<String> = env::args().skip(1).collect();
    let dry = args.iter().any(|a| a == "--dry-run");
    if args.iter().any(|a| {
        !matches!(
            a.as_str(),
            "--dry-run" | "--dry-run-error" | "--dry-run-fresh" | "--test-no-webview"
        )
    }) || (!dry
        && args
            .iter()
            .any(|a| a.starts_with("--dry-run-") || a == "--test-no-webview"))
    {
        message(
            "Supported options: --dry-run [--dry-run-error] [--dry-run-fresh] [--test-no-webview]",
            &app.name,
        );
        return ExitCode::FAILURE;
    }
    if args.iter().any(|a| a == "--test-no-webview") {
        return fallback(&app, dry);
    }
    let Some(local) = env::var_os("LOCALAPPDATA").map(PathBuf::from) else {
        message(
            "LOCALAPPDATA is unavailable for this Windows account.",
            &app.name,
        );
        return ExitCode::FAILURE;
    };
    let installed = if args.iter().any(|a| a == "--dry-run-fresh") {
        None
    } else {
        installed_folder(&app)
    };
    let installed_version = RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey(&app.uninstall_key)
        .and_then(|key| key.get_value::<String, _>("DisplayVersion"))
        .ok();
    let folder = installed
        .clone()
        .unwrap_or_else(|| local.join("Programs").join(&app.name));
    let event_loop = EventLoopBuilder::<UiEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    let window = match WindowBuilder::new()
        .with_title(format!(
            "{} Setup{}",
            app.name,
            if dry { " (preview)" } else { "" }
        ))
        .with_inner_size(LogicalSize::new(720.0, 460.0))
        .with_resizable(false)
        .with_decorations(false)
        .with_visible(false)
        .with_window_icon(Icon::from_resource(1, None).ok())
        .build(&event_loop)
    {
        Ok(window) => window,
        Err(e) => {
            message(&format!("Couldn't open setup: {e}"), &app.name);
            return ExitCode::FAILURE;
        }
    };
    // A CSS radius leaves light fringes on the corners, so Windows 11 rounds the window instead.
    unsafe {
        use windows_sys::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND};
        let preference = DWMWCP_ROUND;
        DwmSetWindowAttribute(
            window.hwnd() as _,
            DWMWA_WINDOW_CORNER_PREFERENCE as u32,
            &preference as *const _ as *const _,
            std::mem::size_of_val(&preference) as u32,
        );
    }
    let profile = match tempfile::Builder::new().prefix("xyn-webview-").tempdir() {
        Ok(temp) => temp,
        Err(e) => {
            message(&e.to_string(), &app.name);
            return ExitCode::FAILURE;
        }
    };
    let mut context = WebContext::new(Some(profile.path().to_path_buf()));
    let ipc_proxy = proxy.clone();
    let initial_data_navigation = std::cell::Cell::new(false);
    let webview = match WebViewBuilder::new_with_web_context(&mut context)
        .with_html(HTML)
        .with_devtools(false)
        .with_hotkeys_zoom(false)
        .with_initialization_script("window.addEventListener('error', e => window.ipc.postMessage(JSON.stringify({cmd:'uiError',message:e.message}))); ")
        .with_navigation_handler(move |url| {
            // WebView2 can expose NavigateToString's first document as a data URL.
            // Only that initial, embedded navigation is allowed; later data URLs are denied.
            url == "about:blank" || (url.starts_with("data:text/html;charset=utf-8;base64,")
                && !initial_data_navigation.replace(true))
        })
        .with_new_window_req_handler(|_, _| wry::NewWindowResponse::Deny)
        .with_ipc_handler(move |request| {
            if let Ok(value) = serde_json::from_str::<Value>(request.body()) {
                let _ = ipc_proxy.send_event(UiEvent::Request(value));
            }
        })
        .build(&window)
    {
        Ok(view) => view,
        Err(_) => {
            drop(window);
            return fallback(&app, dry);
        }
    };
    let mut profile = Some(profile);
    let mut webview = Some(webview);
    let mut context = Some(context);
    let busy = Arc::new(AtomicBool::new(false));
    let mut ready_folder: Option<PathBuf> = None;
    let fail = args.iter().any(|a| a == "--dry-run-error");
    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        let update = |value: Value| { if let Some(view) = &webview { let _ = view.evaluate_script(&format!("window.host({value})")); } };
        match event {
            Event::UserEvent(UiEvent::Request(req)) => match req["cmd"].as_str().unwrap_or("") {
                "uiError" => {
                    message(&format!("Setup's interface couldn't load: {}", req["message"].as_str().unwrap_or("Unknown interface error")), &app.name);
                    if !busy.load(Ordering::SeqCst) { *control_flow = ControlFlow::Exit; }
                }
                "ready" => {
                    let display_folder = folder.strip_prefix(&local).map(|p| format!("%LOCALAPPDATA%\\{}", p.display()))
                        .unwrap_or_else(|_| folder.to_string_lossy().into_owned());
                    update(json!({"kind":"init","folder":display_folder,"installed":installed.is_some(),"installedVersion":installed_version})); window.set_visible(true);
                }
                "close" if !busy.load(Ordering::SeqCst) => *control_flow = ControlFlow::Exit,
                "minimise" => window.set_minimized(true),
                "drag" => { let _ = window.drag_window(); }
                "browse" if !busy.load(Ordering::SeqCst) => {
                    if let Some(path) = rfd::FileDialog::new().set_directory(&folder).pick_folder() {
                        update(json!({"kind":"folder","folder":path}));
                    }
                }
                "install" if !busy.load(Ordering::SeqCst) && ready_folder.is_none() => {
                    let destination = validate_folder(req["folder"].as_str().unwrap_or(""), &app, &local);
                    match destination {
                        Err(e) => update(json!({"kind":"error","message":e})),
                        Ok(destination) => {
                            busy.store(true, Ordering::SeqCst);
                            let proxy = proxy.clone();
                            let desktop = req["desktop"].as_bool().unwrap_or(true);
                            let start_menu = req["startMenu"].as_bool().unwrap_or(true);
                            thread::spawn(move || {
                                if dry { dry_install(proxy, destination, fail); }
                                else {
                                    let app = serde_json::from_str(APP_JSON).unwrap();
                                    let result = run_install(destination, desktop, start_menu, &proxy, &app);
                                    let _ = proxy.send_event(UiEvent::Complete(result));
                                }
                            });
                        }
                    }
                }
                "launch" if !busy.load(Ordering::SeqCst) => {
                    if let Some(destination) = &ready_folder {
                        if dry { *control_flow = ControlFlow::Exit; }
                        else {
                            match Command::new(destination.join(&app.exe)).current_dir(destination).creation_flags(CREATE_NO_WINDOW).spawn() {
                                Ok(_) => *control_flow = ControlFlow::Exit,
                                Err(e) => update(json!({"kind":"launchError","message":format!("Installation finished, but the app couldn't start: {e}. Open it from its install folder.")})),
                            }
                        }
                    }
                }
                _ => {}
            },
            Event::UserEvent(UiEvent::Update(value)) => update(value),
            Event::UserEvent(UiEvent::Complete(result)) => {
                busy.store(false, Ordering::SeqCst);
                match result {
                    Ok(destination) => { ready_folder = Some(destination); update(json!({"kind":"done"})); }
                    Err(e) => update(json!({"kind":"error","message":e})),
                }
            }
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } => {
                if !busy.load(Ordering::SeqCst) { update(json!({"kind":"close"})); }
            }
            Event::LoopDestroyed => {
                // These are uniquely created temporary directories, never the app's data or install tree.
                drop(webview.take());
                drop(context.take());
                if let Some(temp) = profile.take() { clean_profile(temp); }
            }
            _ => {}
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    fn app() -> App {
        serde_json::from_str(APP_JSON).unwrap()
    }
    #[test]
    fn destination_rejects_unsafe_and_data_paths() {
        let local = Path::new(r"C:\SyntheticLocal");
        for bad in [
            "",
            "relative",
            "C:\\",
            r"C:\Apps\..\Other",
            "C:\\Bad\"Path",
            r"\\server\share\app",
            r"C:\Apps\bad.",
            r"C:\SyntheticLocal\XynClaim",
            r"C:\SyntheticLocal\XynClaim\nested",
            r"C:\Apps\bad:stream",
        ] {
            assert!(validate_folder(bad, &app(), local).is_err(), "{bad}");
        }
        assert_eq!(
            validate_folder(r"%LOCALAPPDATA%\Programs\XynClaim", &app(), local).unwrap(),
            local.join(r"Programs\XynClaim")
        );
        assert!(validate_folder(r"C:\Installer Test\Example App", &app(), local).is_ok());
    }
    #[test]
    fn byte_progress_counts_only_expected_files_and_caps_each_length() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("payload"), b"1234").unwrap();
        fs::write(temp.path().join("unrelated"), [0; 1000]).unwrap();
        let files = vec![PayloadFile {
            path: "payload".into(),
            size: 8,
        }];
        assert_eq!(payload_bytes(temp.path(), &files), 4);
        fs::write(temp.path().join("payload"), b"1234567890").unwrap();
        assert_eq!(payload_bytes(temp.path(), &files), 8);
    }
    #[test]
    fn flags_keep_nsis_destination_last() {
        let cmd = installer_command(
            Path::new("setup.exe"),
            Path::new(r"C:\Apps With Spaces\Claim"),
            false,
            false,
            Path::new(r"C:\Temp\progress.txt"),
        );
        let rendered = format!("{cmd:?}");
        assert!(rendered.contains("/S"));
        assert!(rendered.find("/NoDesktop").unwrap() < rendered.find("/D=").unwrap());
        assert!(rendered.find("/NoStartMenu").unwrap() < rendered.find("/D=").unwrap());
    }
}
