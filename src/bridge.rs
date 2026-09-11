// src/bridge.rs — controller-side bridge (long-running IPC server).
//
// Reads ./bridge.toml (or --config <file>, or legacy positional args) for the
// host connection (self-hosted hbbs supported via id/server/key) and video
// quality, then keeps a headless RustDesk Session alive and serves a local TCP
// IPC for the Python brain:
//   request  : one JSON line, e.g. {"cmd":...} plus params:
//     frame   {}                            -> latest decoded frame (binary reply)
//     status  {}                            -> {"connected",w,h,seq,"pending_2fa"} (seq = frame counter)
//     send_2fa {code,trust?}                -> answer the host's 2FA challenge (msgbox "input-2fa")
//     tap     {x,y,w,h}                     -> humanized left click inside rect
//     move    {x,y}                         -> plain cursor move
//     click   {x,y,button?,double?,humanize?} -> click at point; button left|right|middle
//     drag    {x1,y1,x2,y2}                 -> humanized left-button drag
//     scroll  {dx,dy,x?,y?}                 -> wheel deltas (dy>0 scrolls down); optional x,y = move first
//     key     {name,alt?,ctrl?,shift?,meta?} -> key tap; name is a single char or VK_* (VK_RETURN, ...)
//     type    {text}                        -> server-side whole-string injection
//     quit    {}                            -> close session & exit
//   response : [type:u8][len:u32 BE][payload]
//              type 0x01 = JSON(UTF8);  type 0x02 = frame ([w:u32][h:u32][RGBA bytes])
//
//   Build: cargo build --release --bin bridge   (no --features flutter)
//   Run:   ./bridge                  # reads ./bridge.toml
//          ./bridge --config path    # explicit config
//          ./bridge <peer-id> [pw]   # legacy (public/default server)

use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_derive::Deserialize;

use crate::client::{Data, Interface, QualityStatus};
use crate::ui_session_interface::{io_loop, InvokeUiSession, Session};
use hbb_common::message_proto::{
    CursorData, CursorPosition, DisplayInfo, FileEntry, PeerInfo, SwitchDisplay, TerminalResponse,
    WindowsSession,
};
use hbb_common::rendezvous_proto::ConnType;
use scrap::{ImageFormat, ImageRgb};

const DEFAULT_PORT: u16 = 21567;

const MOUSE_TYPE_MOVE: i32 = 0;
const MOUSE_TYPE_DOWN: i32 = 1;
const MOUSE_TYPE_UP: i32 = 2;
const MOUSE_TYPE_WHEEL: i32 = 3;
const MOUSE_TYPE_MOVE_RELATIVE: i32 = 5;
const MOUSE_BUTTON_LEFT: i32 = 1;
const MOUSE_BUTTON_RIGHT: i32 = 2;
const MOUSE_BUTTON_WHEEL: i32 = 4; // middle button
fn left_down() -> i32 {
    MOUSE_TYPE_DOWN | (MOUSE_BUTTON_LEFT << 3)
}
fn left_up() -> i32 {
    MOUSE_TYPE_UP | (MOUSE_BUTTON_LEFT << 3)
}

// ---- config --------------------------------------------------------------

#[derive(Deserialize)]
struct BridgeConfig {
    #[serde(default)]
    connection: ConnectionCfg,
    #[serde(default)]
    video: VideoCfg,
    #[serde(default)]
    ipc: IpcCfg,
}
impl Default for BridgeConfig {
    fn default() -> Self {
        Self {
            connection: ConnectionCfg::default(),
            video: VideoCfg::default(),
            ipc: IpcCfg::default(),
        }
    }
}

#[derive(Deserialize, Default)]
struct ConnectionCfg {
    id: String,
    #[serde(default)]
    server: Option<String>,
    #[serde(default)]
    key: Option<String>,
    #[serde(default)]
    password: String,
}

#[derive(Deserialize)]
struct VideoCfg {
    #[serde(default = "default_quality")]
    quality: String, // best | balanced | low | custom
    #[serde(default = "default_cq")]
    custom_quality: i32, // 0..100, only when quality = custom
    #[serde(default)]
    fps: Option<i32>,
}
impl Default for VideoCfg {
    fn default() -> Self {
        Self {
            quality: default_quality(),
            custom_quality: default_cq(),
            fps: None,
        }
    }
}
fn default_quality() -> String {
    "balanced".into()
}
fn default_cq() -> i32 {
    50
}

#[derive(Deserialize)]
struct IpcCfg {
    #[serde(default = "default_port")]
    port: u16,
}
impl Default for IpcCfg {
    fn default() -> Self {
        Self {
            port: default_port(),
        }
    }
}
fn default_port() -> u16 {
    DEFAULT_PORT
}

fn load_cfg_or_exit() -> BridgeConfig {
    let mut args = std::env::args().skip(1);
    let mut config_path: Option<String> = None;
    let mut legacy: Vec<String> = Vec::new();
    while let Some(a) = args.next() {
        if a == "--config" || a == "-c" {
            config_path = args.next();
        } else {
            legacy.push(a);
        }
    }
    if let Some(p) = config_path {
        return load_toml(&p);
    }
    if Path::new("bridge.toml").exists() {
        return load_toml("bridge.toml");
    }
    if !legacy.is_empty() {
        return BridgeConfig {
            connection: ConnectionCfg {
                id: legacy[0].clone(),
                password: legacy.get(1).cloned().unwrap_or_default(),
                ..Default::default()
            },
            ..Default::default()
        };
    }
    eprintln!(
        "usage: bridge [--config <file>] | <peer-id> [password]\n        (reads ./bridge.toml by default)"
    );
    std::process::exit(2);
}

fn load_toml(path: &str) -> BridgeConfig {
    let s = std::fs::read_to_string(path).unwrap_or_else(|e| {
        eprintln!("[bridge] read {path}: {e}");
        std::process::exit(1);
    });
    toml::from_str(&s).unwrap_or_else(|e| {
        eprintln!("[bridge] parse {path}: {e}");
        std::process::exit(1);
    })
}

fn build_peer(c: &ConnectionCfg) -> String {
    let mut p = c.id.clone();
    if let Some(s) = &c.server {
        p.push('@');
        p.push_str(s);
        if let Some(k) = &c.key {
            p.push_str("?key=");
            p.push_str(k);
        }
    }
    p
}

fn apply_video(session: &Session<BridgeHandler>, v: &VideoCfg) {
    match v.quality.as_str() {
        "custom" => session.save_custom_image_quality(v.custom_quality),
        q if !q.is_empty() => session.save_image_quality(q.to_string()),
        _ => {}
    }
    if let Some(fps) = v.fps {
        session.set_custom_fps(fps);
    }
}

// ---- PRNG (humanization) -------------------------------------------------

struct Rng(u64);
impl Rng {
    fn new() -> Self {
        let s = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0xC0FFEE);
        Rng(if s == 0 { 0xC0FFEE } else { s })
    }
    fn u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn range(&mut self, lo: i32, hi: i32) -> i32 {
        if hi <= lo {
            return lo;
        }
        lo + (self.u64() % (hi - lo) as u64) as i32
    }
}

// ---- session handler -----------------------------------------------------

#[derive(Clone, Default)]
struct BridgeHandler {
    latest: Arc<Mutex<Option<(Vec<u8>, usize, usize, ImageFormat)>>>,
    // Monotonic decoded-frame counter; lets a caller request "a frame newer
    // than the one I last saw" and detect a stale (pre-action) capture.
    seq: Arc<AtomicU64>,
    peer_info: Arc<RwLock<Option<PeerInfo>>>,
    error: Arc<Mutex<Option<String>>>,
    // Set while the host is waiting for a 2FA code (msgbox "input-2fa");
    // cleared by the `send_2fa` IPC command or on successful login.
    pending_2fa: Arc<Mutex<bool>>,
}

impl InvokeUiSession for BridgeHandler {
    fn on_rgba(&self, _display: usize, rgba: &mut ImageRgb) {
        self.seq.fetch_add(1, Ordering::Relaxed);
        *self.latest.lock().unwrap() = Some((rgba.raw.clone(), rgba.w, rgba.h, rgba.fmt));
    }
    fn set_peer_info(&self, pi: &PeerInfo) {
        *self.pending_2fa.lock().unwrap() = false;
        *self.peer_info.write().unwrap() = Some(pi.clone());
    }
    fn msgbox(&self, t: &str, title: &str, text: &str, _link: &str, _retry: bool) {
        eprintln!("[bridge] msgbox type={t} title={title} text={text}");
        if t == "input-2fa" {
            *self.pending_2fa.lock().unwrap() = true;
        } else if t == "error" {
            *self.error.lock().unwrap() = Some(format!("{title}: {text}"));
        }
    }
    fn get_rgba(&self, _display: usize) -> *const u8 {
        std::ptr::null()
    }
    fn set_cursor_data(&self, _: CursorData) {}
    fn set_cursor_id(&self, _: String) {}
    fn set_cursor_position(&self, _: CursorPosition) {}
    fn set_display(&self, _: i32, _: i32, _: i32, _: i32, _: bool, _: f64) {}
    fn switch_display(&self, _: &SwitchDisplay) {}
    fn set_displays(&self, _: &Vec<DisplayInfo>) {}
    fn set_platform_additions(&self, _: &str) {}
    fn on_connected(&self, _: ConnType) {}
    fn update_privacy_mode(&self) {}
    fn set_permission(&self, _: &str, _: bool) {}
    fn close_success(&self) {}
    fn update_quality_status(&self, _: QualityStatus) {}
    fn set_connection_type(&self, _: bool, _: bool, _: &str) {}
    fn set_fingerprint(&self, _: String) {}
    fn job_error(&self, _: i32, _: String, _: i32) {}
    fn job_done(&self, _: i32, _: i32) {}
    fn clear_all_jobs(&self) {}
    fn new_message(&self, _: String) {}
    fn update_transfer_list(&self) {}
    fn load_last_job(&self, _: i32, _: &str, _: bool) {}
    fn update_folder_files(&self, _: i32, _: &Vec<FileEntry>, _: String, _: bool, _: bool) {}
    fn confirm_delete_files(&self, _: i32, _: i32, _: String) {}
    fn override_file_confirm(&self, _: i32, _: i32, _: String, _: bool, _: bool) {}
    fn update_block_input_state(&self, _: bool) {}
    fn job_progress(&self, _: i32, _: i32, _: f64, _: f64) {}
    fn adapt_size(&self) {}
    fn cancel_msgbox(&self, _: &str) {}
    fn switch_back(&self, _: &str) {}
    fn portable_service_running(&self, _: bool) {}
    fn on_voice_call_started(&self) {}
    fn on_voice_call_closed(&self, _: &str) {}
    fn on_voice_call_waiting(&self) {}
    fn on_voice_call_incoming(&self) {}
    fn next_rgba(&self, _: usize) {}
    fn set_multiple_windows_session(&self, _: Vec<WindowsSession>) {}
    fn set_current_display(&self, _: i32) {}
    fn update_record_status(&self, _: bool) {}
    fn printer_request(&self, _: i32, _: String) {}
    fn handle_screenshot_resp(&self, _: String, _: String) {}
    fn handle_terminal_response(&self, _: TerminalResponse) {}
    #[cfg(any(target_os = "android", target_os = "ios"))]
    fn clipboard(&self, _: String) {}
    #[cfg(all(feature = "vram", feature = "flutter"))]
    fn on_texture(&self, _: usize, _: *mut std::ffi::c_void) {}
    #[cfg(feature = "flutter")]
    fn is_multi_ui_session(&self) -> bool {
        false
    }
}

struct BridgeState {
    latest: Arc<Mutex<Option<(Vec<u8>, usize, usize, ImageFormat)>>>,
    seq: Arc<AtomicU64>,
    peer_info: Arc<RwLock<Option<PeerInfo>>>,
    error: Arc<Mutex<Option<String>>>,
    pending_2fa: Arc<Mutex<bool>>,
    // Last position the cursor was sent to; lets humanized moves interpolate
    // from the real start point instead of assuming (0,0).
    mouse: Arc<Mutex<(i32, i32)>>,
    session: Session<BridgeHandler>,
}

pub fn run() {
    let cfg = load_cfg_or_exit();
    let port = cfg.ipc.port;
    let peer = build_peer(&cfg.connection);
    eprintln!(
        "[bridge] peer={peer} server={} port={port}",
        cfg.connection.server.as_deref().unwrap_or("(default)")
    );

    let handler = BridgeHandler::default();
    let latest = handler.latest.clone();
    let seq = handler.seq.clone();
    let peer_info = handler.peer_info.clone();
    let error = handler.error.clone();
    let pending_2fa = handler.pending_2fa.clone();

    let session: Session<BridgeHandler> = Session {
        password: cfg.connection.password.clone(),
        ui_handler: handler,
        server_keyboard_enabled: Arc::new(RwLock::new(true)),
        server_file_transfer_enabled: Arc::new(RwLock::new(true)),
        server_clipboard_enabled: Arc::new(RwLock::new(true)),
        reconnect_count: Arc::new(AtomicUsize::new(0)),
        ..Default::default()
    };
    session
        .lc
        .write()
        .unwrap()
        .initialize(peer.clone(), ConnType::DEFAULT_CONN, None, false, None, None, None);

    // Video quality/fps — applied before connect; send() is a no-op pre-connection,
    // the config is persisted and read when io_loop builds the OptionMessage.
    apply_video(&session, &cfg.video);

    let round = session.connection_round_state.lock().unwrap().new_round();
    let s = session.clone();
    std::thread::spawn(move || {
        io_loop(s, round);
    });

    let state = Arc::new(BridgeState {
        latest,
        seq,
        peer_info,
        error,
        pending_2fa,
        mouse: Arc::new(Mutex::new((0, 0))),
        session,
    });

    let listener = TcpListener::bind(("127.0.0.1", port)).unwrap_or_else(|e| {
        eprintln!("[bridge] bind 127.0.0.1:{port} failed: {e}");
        std::process::exit(1);
    });
    eprintln!("[bridge] listening on 127.0.0.1:{port}");
    for stream in listener.incoming() {
        match stream {
            Ok(st) => {
                let st_state = Arc::clone(&state);
                std::thread::spawn(move || {
                    if let Err(e) = handle_conn(st, st_state) {
                        eprintln!("[bridge] conn error: {e}");
                    }
                });
            }
            Err(e) => eprintln!("[bridge] accept error: {e}"),
        }
    }
}

fn write_msg(stream: &mut TcpStream, ty: u8, payload: &[u8]) -> std::io::Result<()> {
    stream.write_all(&[ty])?;
    stream.write_all(&(payload.len() as u32).to_be_bytes())?;
    stream.write_all(payload)?;
    stream.flush()?;
    Ok(())
}

fn handle_conn(stream: TcpStream, state: Arc<BridgeState>) -> std::io::Result<()> {
    stream.set_nodelay(true).ok();
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut writer = stream;
    let mut line = String::new();
    loop {
        line.clear();
        if reader.read_line(&mut line)? == 0 {
            return Ok(());
        }
        let req: serde_json::Value = match serde_json::from_str(line.trim()) {
            Ok(v) => v,
            Err(e) => {
                let msg = format!("{{\"ok\":false,\"err\":\"bad json: {e}\"}}");
                write_msg(&mut writer, 0x01, msg.as_bytes())?;
                continue;
            }
        };
        let cmd = req.get("cmd").and_then(|v| v.as_str()).unwrap_or("");
        match cmd {
            "frame" => match state.latest.lock().unwrap().clone() {
                Some((raw, w, h, fmt)) => {
                    let rgba = swizzle_rgba(&raw, w, h, fmt);
                    let mut payload = Vec::with_capacity(8 + rgba.len());
                    payload.extend_from_slice(&(w as u32).to_be_bytes());
                    payload.extend_from_slice(&(h as u32).to_be_bytes());
                    payload.extend_from_slice(&rgba);
                    write_msg(&mut writer, 0x02, &payload)?;
                }
                None => {
                    let err = state.error.lock().unwrap().clone();
                    let msg = match err {
                        Some(e) => format!("{{\"ok\":false,\"err\":\"{e}\"}}"),
                        None => "{\"ok\":false,\"err\":\"no frame yet\"}".to_string(),
                    };
                    write_msg(&mut writer, 0x01, msg.as_bytes())?;
                }
            },
            "tap" => {
                let get = |k: &str| req.get(k).and_then(|v| v.as_i64()).unwrap_or(0) as i32;
                let (tx, ty) = humanized_tap(&state.session, get("x"), get("y"), get("w"), get("h"));
                *state.mouse.lock().unwrap() = (tx, ty);
                let msg = format!("{{\"ok\":true,\"x\":{tx},\"y\":{ty}}}");
                write_msg(&mut writer, 0x01, msg.as_bytes())?;
            }
            "move" => {
                let get = |k: &str| req.get(k).and_then(|v| v.as_i64()).unwrap_or(0) as i32;
                humanized_move(&state.session, &state.mouse, get("x"), get("y"));
                write_msg(&mut writer, 0x01, b"{\"ok\":true}")?;
            }
            "click" => {
                let get = |k: &str| req.get(k).and_then(|v| v.as_i64()).unwrap_or(0) as i32;
                let getb = |k: &str| req.get(k).and_then(|v| v.as_bool()).unwrap_or(false);
                let button = match req.get("button").and_then(|v| v.as_str()).unwrap_or("left") {
                    "right" => MOUSE_BUTTON_RIGHT,
                    "middle" => MOUSE_BUTTON_WHEEL,
                    _ => MOUSE_BUTTON_LEFT,
                };
                let (tx, ty) = humanized_click(
                    &state.session,
                    button,
                    get("x"),
                    get("y"),
                    getb("humanize"),
                );
                *state.mouse.lock().unwrap() = (tx, ty);
                if getb("double") {
                    std::thread::sleep(Duration::from_millis(80));
                    press_button(&state.session, button, tx, ty);
                }
                let msg = format!("{{\"ok\":true,\"x\":{tx},\"y\":{ty}}}");
                write_msg(&mut writer, 0x01, msg.as_bytes())?;
            }
            "drag" => {
                let get = |k: &str| req.get(k).and_then(|v| v.as_i64()).unwrap_or(0) as i32;
                let (x2, y2) = (get("x2"), get("y2"));
                humanized_drag(&state.session, get("x1"), get("y1"), x2, y2);
                *state.mouse.lock().unwrap() = (x2, y2);
                write_msg(&mut writer, 0x01, b"{\"ok\":true}")?;
            }
            "scroll" => {
                let get = |k: &str| req.get(k).and_then(|v| v.as_i64()).unwrap_or(0) as i32;
                let (x, y, dx, dy) = (get("x"), get("y"), get("dx"), get("dy"));
                // Wheel events carry no position; optionally move first so the
                // scroll happens under the given point.
                if x != 0 || y != 0 {
                    humanized_move(&state.session, &state.mouse, x, y);
                    std::thread::sleep(Duration::from_millis(30));
                }
                // Wire convention (matches the flutter client): a NEGATIVE
                // wheel delta scrolls down/right, so negate the natural dy>0=down.
                state.session.send_mouse(
                    MOUSE_TYPE_WHEEL,
                    -dx,
                    -dy,
                    false,
                    false,
                    false,
                    false,
                );
                write_msg(&mut writer, 0x01, b"{\"ok\":true}")?;
            }
            "key" => {
                let getb = |k: &str| req.get(k).and_then(|v| v.as_bool()).unwrap_or(false);
                let name = req.get("name").and_then(|v| v.as_str()).unwrap_or("");
                if name.is_empty() {
                    write_msg(&mut writer, 0x01, b"{\"ok\":false,\"err\":\"missing name\"}")?;
                } else {
                    // press=true -> single down+up event; modifiers ride along
                    // in the same KeyEvent (Legacy mode).
                    state.session.input_key(
                        name,
                        false,
                        true,
                        getb("alt"),
                        getb("ctrl"),
                        getb("shift"),
                        getb("meta"),
                    );
                    write_msg(&mut writer, 0x01, b"{\"ok\":true}")?;
                }
            }
            "type" => {
                let text = req.get("text").and_then(|v| v.as_str()).unwrap_or("");
                state.session.input_string(text);
                write_msg(&mut writer, 0x01, b"{\"ok\":true}")?;
            }
            "status" => {
                let pi = state.peer_info.read().unwrap().clone();
                let err = state.error.lock().unwrap().clone();
                let (w, h) = pi
                    .as_ref()
                    .and_then(|p| p.displays.get(p.current_display as usize))
                    .map(|d| (d.width, d.height))
                    .unwrap_or((0, 0));
                let connected = pi.is_some() && err.is_none();
                let seq = state.seq.load(Ordering::Relaxed);
                let pending_2fa = *state.pending_2fa.lock().unwrap();
                let msg = format!(
                    "{{\"ok\":true,\"connected\":{connected},\"w\":{w},\"h\":{h},\"seq\":{seq},\"pending_2fa\":{pending_2fa}}}"
                );
                write_msg(&mut writer, 0x01, msg.as_bytes())?;
            }
            "send_2fa" => {
                let code = req.get("code").and_then(|v| v.as_str()).unwrap_or("");
                let trust = req.get("trust").and_then(|v| v.as_bool()).unwrap_or(true);
                if code.is_empty() {
                    write_msg(&mut writer, 0x01, b"{\"ok\":false,\"err\":\"missing code\"}")?;
                } else {
                    // With trust=true the host can mark this device trusted and
                    // skip 2FA on future connections (if it enables trusted
                    // devices). A wrong code makes the host re-challenge.
                    state.session.send2fa(code.to_string(), trust);
                    *state.pending_2fa.lock().unwrap() = false;
                    write_msg(&mut writer, 0x01, b"{\"ok\":true}")?;
                }
            }
            "quit" => {
                write_msg(&mut writer, 0x01, b"{\"ok\":true}")?;
                let _ = state.session.send(Data::Close);
                break;
            }
            other => {
                let msg = format!("{{\"ok\":false,\"err\":\"unknown cmd: {other}\"}}");
                write_msg(&mut writer, 0x01, msg.as_bytes())?;
            }
        }
    }
    Ok(())
}

fn swizzle_rgba(raw: &[u8], w: usize, h: usize, fmt: ImageFormat) -> Vec<u8> {
    // libyuv (and scrap's ImageFormat) name the packed u32 WORD (MSB->LSB);
    // on little-endian hosts that lands in memory reversed: "ARGB" = bytes
    // B,G,R,A and "ABGR" = bytes R,G,B,A. Reading the label as byte order put
    // the constant-0xFF alpha into the blue channel, tinting frames purple.
    let n = w * h * 4;
    let mut out = Vec::with_capacity(n);
    match fmt {
        ImageFormat::ARGB => {
            for i in (0..n).step_by(4) {
                if i + 3 >= raw.len() {
                    break;
                }
                out.extend_from_slice(&[raw[i + 2], raw[i + 1], raw[i], raw[i + 3]]);
            }
        }
        // "ABGR" words are already R,G,B,A bytes on little-endian
        _ => out.extend_from_slice(raw),
    }
    out
}

// Humanized left click within rect (x,y,w,h): random landing point near center
// (clamped inside the rect), a short off->on cursor travel, then down/up with
// randomized hold time. Avoids dead-center, instant, identical clicks.
fn humanized_tap(session: &Session<BridgeHandler>, x: i32, y: i32, w: i32, h: i32) -> (i32, i32) {
    let mut rng = Rng::new();
    let cx = x + w / 2;
    let cy = y + h / 2;
    let tx = (cx + rng.range(-(w / 5), w / 5)).clamp(x + 2, x + w - 2);
    let ty = (cy + rng.range(-(h / 5), h / 5)).clamp(y + 2, y + h - 2);

    let off_x = rng.range(5, 14);
    let off_y = rng.range(5, 14);
    session.send_mouse(MOUSE_TYPE_MOVE, tx - off_x, ty - off_y, false, false, false, false);
    std::thread::sleep(Duration::from_millis(rng.range(25, 55) as u64));
    session.send_mouse(MOUSE_TYPE_MOVE, tx, ty, false, false, false, false);
    std::thread::sleep(Duration::from_millis(rng.range(15, 40) as u64));
    session.send_mouse(left_down(), tx, ty, false, false, false, false);
    std::thread::sleep(Duration::from_millis(rng.range(35, 95) as u64));
    session.send_mouse(left_up(), tx, ty, false, false, false, false);
    (tx, ty)
}

// Button press (down + up) at a fixed point with jittered hold time.
fn press_button(session: &Session<BridgeHandler>, button: i32, x: i32, y: i32) {
    let mut rng = Rng::new();
    session.send_mouse(
        MOUSE_TYPE_DOWN | (button << 3),
        x,
        y,
        false,
        false,
        false,
        false,
    );
    std::thread::sleep(Duration::from_millis(rng.range(35, 95) as u64));
    session.send_mouse(MOUSE_TYPE_UP | (button << 3), x, y, false, false, false, false);
}

// Humanized click at a point: random landing within a few px, a short
// off->on approach travel, jittered press timing. Returns the landing point.
fn humanized_click(
    session: &Session<BridgeHandler>,
    button: i32,
    x: i32,
    y: i32,
    humanize: bool,
) -> (i32, i32) {
    let mut rng = Rng::new();
    let (tx, ty) = if humanize {
        (x + rng.range(-4, 4), y + rng.range(-4, 4))
    } else {
        (x, y)
    };
    if humanize {
        let (off_x, off_y) = (rng.range(5, 14), rng.range(5, 14));
        session.send_mouse(
            MOUSE_TYPE_MOVE,
            tx - off_x,
            ty - off_y,
            false,
            false,
            false,
            false,
        );
        std::thread::sleep(Duration::from_millis(rng.range(25, 55) as u64));
    }
    session.send_mouse(MOUSE_TYPE_MOVE, tx, ty, false, false, false, false);
    std::thread::sleep(Duration::from_millis(rng.range(15, 40) as u64));
    press_button(session, button, tx, ty);
    (tx, ty)
}

// Humanized cursor move: a jittered multi-step travel from the last known
// cursor position to (x, y) with randomized step timing — no teleport.
fn humanized_move(session: &Session<BridgeHandler>, mouse: &Arc<Mutex<(i32, i32)>>, x: i32, y: i32) {
    let mut rng = Rng::new();
    let (sx, sy) = *mouse.lock().unwrap();
    let steps = rng.range(6, 12);
    for i in 1..=steps {
        let px = sx + (x - sx) * i / steps + rng.range(-1, 2);
        let py = sy + (y - sy) * i / steps + rng.range(-1, 2);
        session.send_mouse(MOUSE_TYPE_MOVE, px, py, false, false, false, false);
        std::thread::sleep(Duration::from_millis(rng.range(15, 40) as u64));
    }
    session.send_mouse(MOUSE_TYPE_MOVE, x, y, false, false, false, false);
    *mouse.lock().unwrap() = (x, y);
}

// Humanized left-button drag from (x1,y1) to (x2,y2): approach, press, a
// jittered multi-step travel, then release.
fn humanized_drag(session: &Session<BridgeHandler>, x1: i32, y1: i32, x2: i32, y2: i32) {
    let mut rng = Rng::new();
    let (off_x, off_y) = (rng.range(5, 14), rng.range(5, 14));
    session.send_mouse(
        MOUSE_TYPE_MOVE,
        x1 - off_x,
        y1 - off_y,
        false,
        false,
        false,
        false,
    );
    std::thread::sleep(Duration::from_millis(rng.range(25, 55) as u64));
    session.send_mouse(MOUSE_TYPE_MOVE, x1, y1, false, false, false, false);
    std::thread::sleep(Duration::from_millis(rng.range(15, 40) as u64));
    session.send_mouse(left_down(), x1, y1, false, false, false, false);
    std::thread::sleep(Duration::from_millis(rng.range(40, 90) as u64));
    let steps = rng.range(8, 14);
    for i in 1..=steps {
        let px = x1 + (x2 - x1) * i / steps + rng.range(-1, 2);
        let py = y1 + (y2 - y1) * i / steps + rng.range(-1, 2);
        session.send_mouse(MOUSE_TYPE_MOVE, px, py, false, false, false, false);
        std::thread::sleep(Duration::from_millis(rng.range(20, 45) as u64));
    }
    std::thread::sleep(Duration::from_millis(rng.range(30, 80) as u64));
    session.send_mouse(left_up(), x2, y2, false, false, false, false);
}
