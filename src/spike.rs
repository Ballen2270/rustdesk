// mhxy-buy spike (path-B additive module).
//
// Headless librustdesk session: connect to a peer by direct IP, grab ONE
// decoded frame to /tmp/rustdesk_frame.png, send ONE left click at the center
// of the remote display. Lives INSIDE the crate so it can reach the private
// `client` / `ui_session_interface` modules.
//
//   Build: cargo build --release --bin spike   (no --features flutter)
//   Run:   ./spike <peer-ip-or-id> <preset-password>
//
// First-draft; expect a compile iteration or two via CI. The trait method
// signatures are taken verbatim from src/ui_session_interface.rs; the only
// likely fixes are import paths for a couple of helper types.

use std::sync::atomic::AtomicUsize;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use crate::client::{Data, Interface, QualityStatus};
use crate::ui_session_interface::{io_loop, InvokeUiSession, Session};
use base::message_proto::{
    CursorData, CursorPosition, DisplayInfo, FileEntry, PeerInfo, SwitchDisplay, TerminalResponse,
    WindowsSession,
};
use hbb_common::rendezvous_proto::ConnType;
use scrap::{ImageFormat, ImageRgb};

// Mouse masks mirroring src/common.rs `pub mod input` (hardcoded to avoid
// import-path churn in the spike).
const MOUSE_TYPE_MOVE: i32 = 0;
const MOUSE_TYPE_DOWN: i32 = 1;
const MOUSE_TYPE_UP: i32 = 2;
const MOUSE_BUTTON_LEFT: i32 = 1; // encoded as (button << 3)
fn mask_left_down() -> i32 {
    MOUSE_TYPE_DOWN | (MOUSE_BUTTON_LEFT << 3) // == 9
}
fn mask_left_up() -> i32 {
    MOUSE_TYPE_UP | (MOUSE_BUTTON_LEFT << 3) // == 10
}

#[derive(Clone, Default)]
struct SpikeHandler {
    // (raw bytes, w, h, fmt) of the first decoded frame.
    first_frame: Arc<Mutex<Option<(Vec<u8>, usize, usize, ImageFormat)>>>,
    peer_info: Arc<RwLock<Option<PeerInfo>>>,
    error: Arc<Mutex<Option<String>>>,
}

impl InvokeUiSession for SpikeHandler {
    fn on_rgba(&self, _display: usize, rgba: &mut ImageRgb) {
        let mut slot = self.first_frame.lock().unwrap();
        if slot.is_none() {
            *slot = Some((rgba.raw.clone(), rgba.w, rgba.h, rgba.fmt));
        }
    }

    fn set_peer_info(&self, pi: &PeerInfo) {
        *self.peer_info.write().unwrap() = Some(pi.clone());
    }

    fn msgbox(&self, msgtype: &str, title: &str, text: &str, _link: &str, _retry: bool) {
        eprintln!("[spike] msgbox type={msgtype} title={title} text={text}");
        if msgtype == "error" {
            *self.error.lock().unwrap() = Some(format!("{title}: {text}"));
        }
    }

    fn get_rgba(&self, _display: usize) -> *const u8 {
        std::ptr::null()
    }

    // --- no-ops for the spike ------------------------------------------------
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

    // cfg-gated trait members — matched cfg attrs so the impl is complete under
    // every feature combo and the stock flutter build stays unaffected.
    #[cfg(any(target_os = "android", target_os = "ios"))]
    fn clipboard(&self, _: String) {}
    #[cfg(all(feature = "vram", feature = "flutter"))]
    fn on_texture(&self, _: usize, _: *mut std::ffi::c_void) {}
    #[cfg(feature = "flutter")]
    fn is_multi_ui_session(&self) -> bool {
        false
    }
}

pub fn run() {
    let peer_id = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "192.168.1.50".to_string());
    let password = std::env::args().nth(2).unwrap_or_default();
    eprintln!(
        "[spike] connecting to {peer_id} (password {} bytes)",
        password.len()
    );

    let handler = SpikeHandler::default();
    let first_frame = handler.first_frame.clone();
    let peer_info = handler.peer_info.clone();
    let error = handler.error.clone();

    let session: Session<SpikeHandler> = Session {
        password: password.clone(),
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
        .initialize(
            peer_id.clone(),
            ConnType::DEFAULT_CONN,
            None,
            false,
            None,
            None,
            None,
        );

    // io_loop is #[tokio::main(flavor = "current_thread")] — it brings its own
    // runtime; run it on a dedicated OS thread (mirrors flutter.rs:1426).
    let round = session.connection_round_state.lock().unwrap().new_round();
    let s = session.clone();
    std::thread::spawn(move || {
        io_loop(s, round);
    });

    // Wait for the first frame (or error / timeout).
    let started = Instant::now();
    let (raw, w, h, fmt) = loop {
        if let Some(f) = first_frame.lock().unwrap().take() {
            break f;
        }
        if let Some(e) = error.lock().unwrap().take() {
            eprintln!("[spike] session error: {e}");
            std::process::exit(2);
        }
        if started.elapsed() > Duration::from_secs(20) {
            eprintln!("[spike] timed out waiting for first frame");
            std::process::exit(3);
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    eprintln!("[spike] got frame {w}x{h}");

    // libyuv format labels name the packed u32 WORD (MSB->LSB); on
    // little-endian hosts that lands in memory reversed: "ARGB" = bytes
    // B,G,R,A and "ABGR" = bytes R,G,B,A (same fix as bridge.rs swizzle).
    // Assumes packed rows (stride = w*4); if the decoder pads rows the PNG will
    // look skewed — fix by respecting `align` then.
    let mut rgba = Vec::with_capacity(w * h * 4);
    let bytes_per_row = w * 4;
    let n = bytes_per_row * h;
    match fmt {
        ImageFormat::ARGB => {
            for i in (0..n).step_by(4) {
                if i + 3 >= raw.len() {
                    break;
                }
                rgba.extend_from_slice(&[raw[i + 2], raw[i + 1], raw[i], raw[i + 3]]);
            }
        }
        ImageFormat::ABGR => {
            rgba.extend_from_slice(&raw);
        }
        _ => {
            eprintln!("[spike] unsupported fmt, dumping raw");
            rgba.extend_from_slice(&raw);
        }
    }

    let path = "/tmp/rustdesk_frame.png";
    match std::fs::File::create(path) {
        Ok(mut f) => match repng::encode(&mut f, w as u32, h as u32, &rgba) {
            Ok(_) => eprintln!("[spike] wrote {path}"),
            Err(e) => eprintln!("[spike] repng error: {e}"),
        },
        Err(e) => eprintln!("[spike] fs error: {e}"),
    }

    // One left click at the center of the remote display.
    let dims = peer_info
        .read()
        .unwrap()
        .as_ref()
        .and_then(|pi| pi.displays.get(pi.current_display as usize))
        .map(|d| (d.width, d.height));
    if let Some((dw, dh)) = dims {
        let (cx, cy) = (dw / 2, dh / 2);
        eprintln!("[spike] click ({cx}, {cy})");
        session.send_mouse(MOUSE_TYPE_MOVE, cx, cy, false, false, false, false);
        std::thread::sleep(Duration::from_millis(30));
        session.send_mouse(mask_left_down(), cx, cy, false, false, false, false);
        std::thread::sleep(Duration::from_millis(40));
        session.send_mouse(mask_left_up(), cx, cy, false, false, false, false);
    } else {
        eprintln!("[spike] no peer display info; skipping click");
    }

    std::thread::sleep(Duration::from_millis(200));
    session.send(Data::Close);
    eprintln!("[spike] done");
}
