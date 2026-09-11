# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

RustDesk is a remote-desktop application: a Rust core engine (`librustdesk`) plus a Flutter (desktop + mobile) frontend that calls it over `flutter_rust_bridge`. The legacy Sciter UI in `src/ui/` is **deprecated** — do UI work in `flutter/`.

**Read and follow `AGENTS.md` first.** It defines the binding project rules: Rust style (no `unwrap()`/`expect()` in production code), the Tokio runtime rules (never nest runtimes, never `block_on` in async, no `std::thread::sleep` in async), minimal-diff editing hygiene (prefer additive `#[cfg]`-gated code), and the `src/lang/` localization workflow. The notes below cover the build commands and architecture that AGENTS.md does not.

## Build & Run

Requires: Rust toolchain (MSRV **1.75**, no pinned `rust-toolchain`), `VCPKG_ROOT` pointing at a vcpkg checkout (baseline pinned in `vcpkg.json`), and the vcpkg deps `libvpx libyuv opus aom` (Windows: the `x64-windows-static` triplets). On Linux also install the GTK/xcb/pulse/gstreamer system packages listed in `README.md`. Cargo is configured with `git-fetch-with-cli = true` (`.cargo/config`).

### Desktop dev (Flutter UI) — the common path
```sh
cd flutter && ./run.sh        # regenerate bridge, build the cdylib, run the app
```
`run.sh` installs `flutter_rust_bridge_codegen` **1.80.1**, runs `flutter pub get`, regenerates `flutter/lib/generated_bridge.dart` from `src/flutter_ffi.rs`, then `cargo build --features flutter` and `flutter run`. Run `flutter clean` if the cargo build fails on stale artifacts. `LLVM_HOME` may need exporting on macOS (see comment in `run.sh`).

### Release / packaging
`python3 build.py` orchestrates platform packaging (see `--help`). The underlying cargo invocations CI uses:
- Windows: `cargo build --locked --features inline,vram,hwcodec --release --bins`
- macOS: `MACOSX_DEPLOYMENT_TARGET=10.14 cargo build --locked --features flutter,hwcodec --release`, then `flutter build macos --release`
- Linux: `cargo build --locked --features flutter,hwcodec,unix-file-copy-paste --release --lib`, then `flutter build linux --release`
- iOS: `cargo build --features flutter,hwcodec --release --target aarch64-apple-ios --lib`

### Regenerating the FFI bridge
`generated_bridge.dart` and `macos/Runner/bridge_generated.h` are **gitignored — never checked in.** After changing any function exposed to Flutter in `src/flutter_ffi.rs`, regenerate:
```sh
~/.cargo/bin/flutter_rust_bridge_codegen --version 1.80.1 \
  --rust-input ./src/flutter_ffi.rs \
  --dart-output ./flutter/lib/generated_bridge.dart \
  --c-output ./flutter/macos/Runner/bridge_generated.h
```
The bridge version is pinned across `flutter/run.sh` and `.github/workflows/{bridge,mac-build,playground}.yml`; keep them in sync if you bump it.

## Test & Lint
```sh
cargo test                              # whole workspace
cargo test -p rustdesk <name>           # single test in the root crate
cargo test -p hbb_common                # single workspace member
cargo clippy --all-targets             # Rust lint (no clippy.toml — match surrounding style)
cd flutter && flutter analyze           # Dart lint (analysis_options.yaml → lints/recommended)
cd flutter && flutter test              # Dart tests in flutter/test
```

## Key Cargo Features
| feature | meaning |
|---|---|
| `flutter` | Build the Flutter `cdylib` + enable the FFI bridge (`flutter_rust_bridge`); this is the app's default build path |
| `inline` | Inline assets / Windows manifest into the binary (Windows release packaging) |
| `hwcodec` | Hardware video encode/decode via `scrap` |
| `vram` | VRAM codec path |
| `mediacodec` | Android MediaCodec |
| `unix-file-copy-paste` | X11 file clipboard |
| `screencapturekit` | macOS ScreenCaptureKit audio |
| `plugin_framework` | Plugin system |

Default feature set is `default = ["use_dasp"]` (audio resampling). The crate builds as `librustdesk` (`cdylib` + `staticlib` + `rlib`); there are also `naming` and `service` binaries (`src/naming.rs`, `src/service.rs`).

## Architecture

### Entry points
`main.rs` branches on target/feature: under `feature = "flutter"` (or on Android/iOS) it runs only `common::global_init()` — the GUI is then driven by the host process. Otherwise `core_main::core_main()` parses CLI args (custom client, elevate, quick support, no-server, etc.), does Windows bootstrap, and hands off to `ui::start()` (legacy Sciter). `core_main()` is the shared arg-handling entry for both UIs.

### Rust engine (`src/`, all heavily `#[cfg]`-gated per platform)
- `rendezvous_mediator.rs` — **connectivity core.** Registers the peer with a rustdesk-server (hbbs), then establishes the session via direct TCP hole-punching or relay (hbbr). Wire protocol / protobuf live in `libs/hbb_common`.
- `server/` — the **incoming** side (this machine is being controlled): `video_service`, `audio_service`, `input_service`, `clipboard_service`, `display_service`, `terminal_service`, `video_qos`. `connection.rs` is the per-session server state machine.
- `client.rs` + `client/` — the **outgoing** side (controlling a remote): `io_loop.rs` runs the session packet loop.
- `platform/` — per-OS native code: Rust (`linux.rs`, `windows.rs`, `macos.rs`) plus C++ shims (`windows.cc`, `macos.mm`). Per AGENTS.md, **new platform-specific logic goes here in self-contained functions**, with call sites in shared files (`tray.rs`, `core_main.rs`, `server/connection.rs`, …) kept as thin one-line hooks.
- `ipc.rs` + `ipc/` — IPC between the elevated service process and the user process (`parity-tokio-ipc`).
- `lang/` — translations; read AGENTS.md's Localization section before editing (`template.rs` is the master key list — never edit it for translation work).
- Supporting features: `tray.rs`, `virtual_display_manager.rs`, `privacy_mode.rs`, `port_forward.rs`, `updater.rs`, `hbbs_http.rs`, `plugin/`, `whiteboard/`.

### Workspace libraries (`libs/`)
`hbb_common` (config / proto / fs / tcp-udp wrappers — **all user-facing options live in `libs/hbb_common/src/config.rs`**), `scrap` (screen capture), `enigo` (keyboard/mouse simulation), `clipboard` (file copy/paste), `virtual_display`, `remote_printer`, `portable`. Members are declared in the root `Cargo.toml` `[workspace]`. `libxdo-sys` is patched to a stub (`libs/libxdo-sys-stub`) so Linux builds don't require libxdo.

### Flutter UI (`flutter/`)
`lib/desktop/` (desktop screens/pages/widgets), `lib/mobile/` (Android/iOS), shared `lib/common/` and `lib/models/`. The generated bridge is consumed in `lib/models/{native_model,platform_model,model}.dart`. The web client's JS lives in `flutter/web/`.

## Versioning
`Cargo.toml` (`version = "1.4.9"`) and `flutter/pubspec.yaml` (`1.4.9+67`) are kept in sync — bump both together. `build.rs` runs `hbb_common::gen_version()` to bake the git revision into the binary at build time.
