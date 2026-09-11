# 本地开发环境（bridge + MCP 代理）— 安装与清理

> 分支 `patch/mhxy-spike` · macOS arm64 (Apple Silicon) · 配置日期 2026-08-21
>
> 记录本机为编译 [src/bridge.rs](src/bridge.rs) 与运行 [mcp/rustdesk_mcp.py](mcp/rustdesk_mcp.py)
> 所安装的全部组件，方便日后整体清理。已装部分见「安装清单」，还原见文末「清理」。

## 安装清单

| 组件 | 位置 | 说明 |
|---|---|---|
| Xcode Command Line Tools | `/Library/Developer/CommandLineTools` | **原本就有**，未新装。clang 17 + libclang（bindgen 用），rust 编译器后端 |
| Rust 工具链 (stable, 现 1.98) | `~/.rustup`、`~/.cargo` | `brew install rustup`（二进制名是 `rustup`，**不是** rustup-init）→ `rustup toolchain install stable`；brew 版不会自动建 `~/.cargo/bin` 代理，需手动把 `~/.rustup/toolchains/stable-aarch64-apple-darwin/bin/*` 软链过去。**2026-09 起必须 ≥1.87**：上游合并的 webrtc-util 0.12 用了 `usize::is_multiple_of`（1.87 稳定），旧的 1.81 编不过（仓库 Cargo.toml 的 MSRV 1.75 已过时，上游 CI 也用 stable） |
| vcpkg（pinned baseline） | `~/vcpkg` | `git clone` 后 checkout `120deac3062162151622ca4860575a33844ba10b`（= [vcpkg.json](vcpkg.json) baseline），`bootstrap-vcpkg.sh` |
| vcpkg 库 | `~/vcpkg/installed/arm64-osx` | classic 模式：`libvpx libyuv opus aom`（bridge 用不到 ffmpeg，跳过 manifest 全量） |
| nasm、cmake、pkg-config | brew | **实测必需**：nasm 为 aom port 所需（`vcpkg_find_acquire_program(NASM)`），pkg-config 为 `vcpkg_fixup_pkgconfig` 收尾步骤所需 |
| conda env `rustdesk-mcp` | miniconda `envs/` | python 3.12 + `pip install -r mcp/requirements.txt`（mcp 2.x + pillow） |
| submodule `hbb_common` | `libs/hbb_common` | `git submodule update --init --recursive`（不装它无法编译） |
| 一次性测试 venv | `/tmp/mcpvenv` | 开发期验证 SDK API 用的临时 venv，可直接删 |

## 关键环境变量 / 配置文件改动

| 项 | 值 | 谁写入 | 作用 |
|---|---|---|---|
| `VCPKG_ROOT` | `~/vcpkg` | 需自己写进 `~/.zprofile` | [libs/scrap/build.rs](libs/scrap/build.rs) 与 magnum-opus 靠它定位静态库；**不设会 fallback 到 `/opt/homebrew/Cellar`（静态链接，brew 无 .a，会失败）** |
| `PATH` += `~/.cargo/bin` | — | rustup-init 自动写 `~/.zprofile` | cargo / rustc |
| `.mcp.json`（仓库根） | command = conda env 绝对 python 路径 | 手工 | Claude Code 拉起 MCP server 不带 conda 激活，必须绝对路径 |

## 不需要安装的（踩坑记录）

- **Flutter SDK** — bridge 是 `cargo build --bin bridge`，不带 `flutter` feature。
- **protoc** — proto 用 `protobuf-codegen`（纯 Rust）在 build.rs 里生成。
- **brew llvm** — Xcode CLT 的 libclang 够 bindgen 用（CI 装它只是保险）。
- **ffmpeg / dylibbundler** — 仅 `hwcodec` bundle 路径需要（CI 打自包含包才用）。
- **brew 的 libvpx/libyuv/aom/opus** — build.rs fallback 走 brew 但强制 `static=` 链接，brew bottle 无 `.a`，勿走此路。

## 构建 / 测试

```sh
export VCPKG_ROOT=~/vcpkg           # 已写 zprofile 则免
cargo build --release --bin bridge  # 首次 10–20 分钟（~600 crates）

# 冒烟（CI 同款）：打印 "[bridge] listening" 即成功，连不上对端属预期
./target/release/bridge 127.0.0.1 dummy

# 真连对端：复制 bridge.toml.example → bridge.toml 填 id/server/key/password
#（bridge.toml 已 gitignore，含密码不会被提交）
./target/release/bridge --config bridge.toml

# Claude Code：重启会话 → /mcp 启用 rustdesk server
# MCP 代理会自动拉起/复用/守护 bridge（无需手动起）：
#   - 端口没人监听 → spawn `bridge --config bridge.toml`（日志 /tmp/rustdesk-bridge-<port>.log）
#   - 已有 bridge（手动起的 / Python brain 起的）→ 复用，不接管生命周期
#   - bridge 崩了 → 下一次工具调用自动重拉
#   - 会话退出 → 只清理自己拉起的 bridge
```

## 清理

```sh
# brew 包
brew uninstall rustup nasm cmake pkg-config

# Rust 工具链（~/.zprofile 里 rustup 段 PATH 也手动删掉）
rm -rf ~/.rustup ~/.cargo

# vcpkg（含编译好的库）
rm -rf ~/vcpkg
# 并删除 ~/.zprofile 里的 export VCPKG_ROOT=...

# conda 环境
conda env remove -n rustdesk-mcp

# 仓库内构建产物
cargo clean        # 或 rm -rf target

# 临时测试 venv
rm -rf /tmp/mcpvenv

# 保留：libs/hbb_common submodule（属于仓库本身）
```

> 不装任何东西的替代方案：push 到 `patch/mhxy-spike` 后从 CI 下载
> `mhxy-bridge-macos-aarch64` artifact（自包含 dylib + 已签名），只需 conda 环境即可跑 MCP。
