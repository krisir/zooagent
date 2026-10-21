//! zooagent desktop shell.
//!
//! The window never talks to the AI directly: it loads the local FastAPI
//! service (dev: the Vite dev server with an API proxy) and the service owns
//! every business engine. In release builds the shell spawns the bundled
//! Python sidecar, reads the one-line ready handshake from its stdout to learn
//! the random port and session token, injects the token into the page, and
//! kills the sidecar when the window closes.

use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{Manager, RunEvent, WindowEvent};

const WINDOW_LABEL: &str = "main";
const DEFAULT_DEV_URL: &str = "http://localhost:1420";

struct BackendProcess(Mutex<Option<Child>>);

pub fn run() {
    let builder = tauri::Builder::default()
        .manage(BackendProcess(Mutex::new(None)))
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { .. } = event {
                window.app_handle().exit(0);
            }
        })
        .setup(|app| {
            let (url, token) = if cfg!(debug_assertions) {
                let dev_url = std::env::var("ZOOAGENT_DEV_URL")
                    .unwrap_or_else(|_| DEFAULT_DEV_URL.to_string());
                (dev_url, String::new())
            } else {
                let (port, token, child) = spawn_backend()?;
                *app.state::<BackendProcess>().0.lock().unwrap() = Some(child);
                (format!("http://127.0.0.1:{port}/"), token)
            };

            let script = format!(
                "window.__ZOOAGENT_CONFIG__ = {{ token: {}, baseUrl: \"\" }};",
                serde_json::to_string(&token).map_err(|err| err.to_string())?
            );

            tauri::WebviewWindowBuilder::new(
                app,
                WINDOW_LABEL,
                tauri::WebviewUrl::External(
                    url.parse().map_err(|err| format!("无效的地址 {url}: {err}"))?,
                ),
            )
            .title("zooagent")
            .inner_size(1320.0, 860.0)
            .min_inner_size(1040.0, 680.0)
            .initialization_script(&script)
            .build()?;
            Ok(())
        });

    let app = builder
        .build(tauri::generate_context!())
        .expect("failed to build zooagent desktop");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(mut child) = handle.state::<BackendProcess>().0.lock().unwrap().take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    });
}

/// Start the bundled backend and wait for its ready handshake.
fn spawn_backend() -> Result<(u16, String, Child), String> {
    let binary = locate_backend().ok_or_else(|| {
        "找不到后端服务 zooagent-backend。请先运行 scripts/build/build-sidecar.sh".to_string()
    })?;

    let mut command = Command::new(&binary);
    command
        .arg("serve")
        .args(["--host", "127.0.0.1", "--port", "0"])
        // The packaged sidecar is a PyInstaller onefile binary: the process we
        // spawn is a bootloader that runs the real service as a *child*. The
        // env var lets the service notice that this shell is gone and exit,
        // so quitting the app never leaves an orphaned backend holding a port.
        .env("ZOOAGENT_PARENT_PID", std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());

    // Release builds use `windows_subsystem = "windows"` (no console); the
    // sidecar is a console program, so without CREATE_NO_WINDOW every launch
    // flashes a black window on Windows.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = command
        .spawn()
        .map_err(|err| format!("启动后端失败 ({}): {err}", binary.display()))?;

    let stdout = child.stdout.take().ok_or("无法读取后端输出")?;
    let mut reader = BufReader::new(stdout);

    // The backend prints a single ready handshake on stdout. Tolerate other
    // lines (warnings, anything a dependency decides to print) instead of
    // trusting the first line blindly.
    let mut line = String::new();
    let mut attempts = 0;
    let (port, token) = loop {
        line.clear();
        let read = reader
            .read_line(&mut line)
            .map_err(|err| format!("读取后端启动信息失败: {err}"))?;
        if read == 0 {
            return Err("后端在报告监听端口前已退出".to_string());
        }
        attempts += 1;
        if attempts > 200 {
            return Err("后端启动超时（未收到 ready 握手）".to_string());
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let Ok(info) = serde_json::from_str::<serde_json::Value>(trimmed) else {
            continue;
        };
        if info.get("event").and_then(|value| value.as_str()) != Some("ready") {
            continue;
        }
        let port = info["port"].as_u64().ok_or("后端没有报告监听端口")? as u16;
        let token = info["token"].as_str().unwrap_or_default().to_string();
        break (port, token);
    };

    // Keep draining stdout so the child can never block on a full pipe.
    std::thread::spawn(move || {
        let mut sink = String::new();
        let _ = reader.read_to_string(&mut sink);
    });

    Ok((port, token, child))
}

fn locate_backend() -> Option<PathBuf> {
    if let Ok(path) = std::env::var("ZOOAGENT_BACKEND_PATH") {
        let candidate = PathBuf::from(path);
        if candidate.is_file() {
            return Some(candidate);
        }
    }

    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?.to_path_buf();
    // Tauri places external binaries next to the app binary; on macOS the
    // fallback covers the Resources directory of the .app bundle.
    let candidates = [dir.clone(), dir.join("../Resources")];
    for candidate in candidates {
        if let Some(found) = scan_for_sidecar(&candidate) {
            return Some(found);
        }
    }
    None
}

fn scan_for_sidecar(dir: &Path) -> Option<PathBuf> {
    let entries = std::fs::read_dir(dir).ok()?;
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with("zooagent-backend") {
            return Some(entry.path());
        }
    }
    None
}
