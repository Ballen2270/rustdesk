# macOS 远程窗口不透明度功能 — 构建 / 测试 / 清理指南

本分支为 RustDesk 增加了一个 **macOS 远程控制窗口不透明度** 选项：连接到远程设备后，在顶部工具栏的 **Display（显示设置）** 菜单里，紧挨着 *Image Quality* 多了一个 **Window Opacity** 滑块。该值 **按 peer 记忆**，下次连同一台机器自动恢复。

> 仅 macOS 生效；Windows / Linux 菜单不显示此项（UI 用 `isMacOS` 守卫）。

---

## 1. 改了哪些文件

| 文件 | 改动 |
|---|---|
| `flutter/lib/consts.dart` | 新增 `kOptionWindowOpacity` 常量 |
| `flutter/lib/utils/platform_channel.dart` | `RdPlatformChannel.setWindowOpacity(double)`，走原生通道 |
| `flutter/macos/Runner/MainFlutterWindow.swift` | 新增 `setWindowOpacity` case：`window.alphaValue` + `isOpaque`。**对子窗口自动生效**（handler 已在 `setOnWindowCreatedCallback` 里按窗口注册，`registrar.view?.window` 解析到当前窗口的 NSWindow） |
| `flutter/lib/main.dart` | `runMultiWindow` 在 `show()` 后读取 peer 记忆值并应用 |
| `flutter/lib/desktop/widgets/remote_toolbar.dart` | Display 菜单新增 `windowOpacity()` + `_WindowOpacitySlider` 控件（内联滑块，拖动实时生效，松手持久化） |
| `.github/workflows/mac-build.yml` | **新增**：只编 macOS arm64、只产 unsigned dmg、不签名不发 release |

不透明度取值范围 **0.20 ~ 1.00**（下限 0.2 是安全值，避免窗口被调到完全看不见、抓不回来）。存储格式为字符串（如 `"0.65"`），key = `window_opacity`，存在该 peer 的 PeerConfig 里。

---

## 2. 用 GitHub Actions 编译（推荐，零本地环境）

> 公开仓库的 macOS 标准 runner **免费且不限量**，无需 Apple 开发者账号。你的 fork `Ballen2270/rustdesk` 是 public，直接可用。

### 2.1 把本分支推到你的 fork

本仓库本地 remote 指向上游 `rustdesk/rustdesk`。先加你的 fork 为 remote 并推送：

```bash
cd /Users/yanjoy/codeWorkSpace/vscode/rustdesk

# 加 fork remote（只需一次）
git remote add fork https://github.com/Ballen2270/rustdesk.git

# 建一个功能分支
git checkout -b feature/window-opacity-macos

# 提交本分支的全部改动
git add -A
git commit -m "feat(mac): add per-peer remote window opacity slider"

# 推到 fork（注意：workflow_dispatch 只能在默认分支手动触发，
# 所以第一次请把 mac-build.yml 推到默认分支 master）
git push fork feature/window-opacity-macos
# 若想让 Actions 菜单立即可手动触发，也把该 workflow 合到 master：
git push fork feature/window-opacity-macos:master
```

> 如果你已有别的本地副本指向 fork，按你习惯的方式推送即可，关键是 `.github/workflows/mac-build.yml` 要落到 fork 的**默认分支**（通常 `master`），否则 UI 上看不到手动触发的入口。

### 2.2 触发构建

1. 浏览器打开 `https://github.com/Ballen2270/rustdesk/actions`
2. 左侧选 **"Build macOS arm64 (custom)"**
3. 右上 **Run workflow** → 选分支（master）→ Run
4. 等 ~30–60 分钟（vcpkg 首次编译最久，后续有缓存会快很多）

### 2.3 下载产物

构建完成后，点进那次 run → 拉到底 **Artifacts** → 下载 `rustdesk-unsigned-macos-aarch64` → 解压得到 `rustdesk-1.4.9-aarch64.dmg`。

---

## 3. 安装未签名的 dmg（绕过 Gatekeeper）

未签名 + 未公证的包，双击会被拦。任选其一：

```bash
# 方法 A：去掉隔离属性后再装（推荐）
xattr -dr com.apple.quarantine /path/to/rustdesk-1.4.9-aarch64.dmg
# 然后双击 dmg 拖入 Applications

# 方法 B：已拖进 Applications 后对 .app 去隔离
xattr -dr com.apple.quarantine /Applications/RustDesk.app
```

或首次启动时 **右键 → 打开**，确认即可。

---

## 4. 与本机原有 RustDesk 不冲突的注意事项

你测的是**外发连接**（连到别的机器看窗口半透明），**不需要本地后台服务**，因此基本不会冲突。注意三点：

| 项 | 说明 | 处理 |
|---|---|---|
| **配置 / ID 目录共用** | 自定义 build 默认读写官方 RustDesk 同一套配置（`~/Library/.../RustDesk`），可能干扰你真实 ID / 联系人 | **测试前先完全退出官方 RustDesk**（菜单栏图标 → Quit） |
| **`rustdesk://` URL scheme** | 两个 app 都注册了同名 scheme，会抢处理器 | 同上，测试时退出官方版即可 |
| **后台服务（端口 21115-21119）** | 服务是 launchd 守护进程，需管理员权限才会安装 | 你只外发、不启用服务，**不会**装第二个守护进程。无需处理 |

> 一句话：**测试前 Quit 官方 RustDesk，测完再开回来**。官方的后台服务完全不用动。

---

## 5. 测试清单（验证功能是否生效）

装好后连一台测试机，重点验证：

- [ ] 顶部工具栏点开 **Display** 菜单，*Image Quality* 下方出现 **Window Opacity** + 滑块和百分比。
- [ ] 拖动滑块，**整个远程窗口（含视频画面）实时变半透明**，能看到背后的桌面。
- [ ] 松手后断开重连同一 peer → **不透明度自动恢复**（记忆生效）。
- [ ] 连**另一个 peer** → 应是 100%（记忆是 per-peer 的）。
- [ ] 滑块最低 20%（窗口不会消失到抓不回来）。
- [ ] 关掉再开 Display 菜单，百分比显示与实际一致。

### 如果拖动滑块窗口没变透明

说明 `alphaValue` 在你的环境下没生效（极少数 Flutter 纹理配置）。这是唯一的已知风险点，修复很小：编辑 `flutter/macos/Runner/MainFlutterWindow.swift` 的 `setWindowOpacity` case，在设 `alphaValue` 前加一行强制背景可合成：

```swift
window.isOpaque = false            // 已有
window.backgroundColor = NSColor.clear   // ← 加这行（import 已有 Cocoa）
window.alphaValue = CGFloat(opacity)
```

重新走一次 §2 编译即可。

---

## 6. 迭代节奏

每次改代码 → `git push` → Actions 重跑 → 下载新 dmg。一轮约 30–60 分钟。
如果连续多次只改 Dart/Swift、没动 Rust，**rust-cache 会命中**，Rust 部分秒过，整体会明显变快。

---

## 7. （可选）本地编译环境 —— brew 安装清单与清理

> 只有想用 `flutter run` 热重载快速迭代时才需要。**只走 GitHub Actions 的话完全不用装**，可跳过本节。

### 7.1 用 brew 装

```bash
brew install llvm create-dmg pkg-config

# Rust（macOS 需要 1.81）
brew install rustup-init && rustup-init -y
rustup toolchain install 1.81
rustup target add aarch64-apple-darwin

# Flutter（固定 3.24.5，不要装最新）
# 推荐用 fvm 或直接下 stable：https://docs.flutter.dev/release/archive
```

**NASM 必须是 2.16.x，不能用 brew 的 3.x**（3.x 是不兼容重写）：

```bash
wget https://www.nasm.us/pub/nasm/releasebuilds/2.16.03/macosx/nasm-2.16.03-macosx.zip
unzip nasm-2.16.03-macosx.zip
sudo cp nasm-2.16.03/nasm /usr/local/bin/nasm
```

vcpkg（按上游固定 commit）：

```bash
git clone https://github.com/microsoft/vcpkg
cd vcpkg && git checkout 120deac3062162151622ca4860575a33844ba10b
./bootstrap-vcpkg.sh
export VCPKG_ROOT="$PWD"
vcpkg install --triplet arm64-osx
```

### 7.2 一条命令编译（本地）

```bash
cd /Users/yanjoy/codeWorkSpace/vscode/rustdesk
./build.py --flutter --hwcodec --unix-file-copy-paste --screencapturekit
# 产物：flutter/build/macos/Build/Products/Release/RustDesk.app
```

调试用 `flutter/run.sh`（自动 pub get + 生成 bridge + cargo + flutter run）。

### 7.3 清理（功能做完后）

```bash
# brew 装的
brew uninstall llvm create-dmg pkg-config rustup-init
brew autoremove          # 清掉因此变成孤儿的依赖
brew cleanup -s          # 清缓存

# NASM
sudo rm -f /usr/local/bin/nasm

# Rust 工具链
rustup self uninstall    # 或：rustup toolchain uninstall 1.81

# Flutter（看你怎么装的，下面是手动装的路径）
rm -rf ~/development/flutter    # 或你的 flutter 解压目录
rm -rf ~/.pub-cache

# vcpkg
rm -rf /path/to/vcpkg

# RustDesk 自身的 target 构建缓存（很大，可删）
cd /Users/yanjoy/codeWorkSpace/vscode/rustdesk && cargo clean
rm -rf flutter/build flutter/.dart_tool
```

> 这些只清编译工具链，**不会动**你已正式安装的官方 RustDesk 及其配置。

---

## 8. 回退 / 移除本功能

功能全部集中在本分支的改动里（见 §1 文件清单），切回上游 master 或删掉这些改动即可完全移除，无副作用。peer 配置里残留的 `window_opacity` key 会被官方版直接忽略。
