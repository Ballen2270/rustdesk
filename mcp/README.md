# RustDesk MCP — 通过 RustDesk 远程操控机器的 MCP 服务

让 LLM（Claude Code / Claude Desktop / 任何 MCP 客户端）**看到并操作**一台远程设备：
截图 → 决策 → 鼠标键盘 → 再截图，即 computer-use 循环。

```
Claude Code ──MCP/stdio──> rustdesk_mcp.py（代理，常驻轻壳）
                              │
                              │ 本地 TCP JSON IPC (127.0.0.1:21567)
                              ▼
                         bridge（独立 Rust 二进制，内嵌完整 RustDesk 客户端引擎）
                              │  RustDesk 协议（hbbs 注册 / 打洞 / 中继）
                              ▼
                         远程被控端（官方 RustDesk 即可，无需任何修改）
```

- **bridge 与官方生态完全兼容**：自建 hbbs 或公共服务器、官方被控端，均无需修改。
- **代理托管 bridge 生命周期**：端口没人监听就自动拉起；崩了自动重拉；空闲自动睡眠。
- 配置只有一份 `bridge.toml`（含密码，已 gitignore）。

---

## 一、安装

### 1. 构建 bridge（Rust 侧，一次性）

需要 Rust 1.81 + vcpkg 静态库（libvpx/libyuv/opus/aom），完整环境搭建见
[DEV-ENV.md](../DEV-ENV.md)（含清理指南）：

```sh
export VCPKG_ROOT=~/vcpkg
cargo build --release --bin bridge     # 产物 target/release/bridge
```

不想装工具链：从 CI（spike.yml）下载 `mhxy-bridge-macos-aarch64` artifact，
自包含 dylib + 已签名，解压即用（把 `.mcp.json` 的 BIN 指向它即可）。

### 2. Python 侧（conda）

```sh
conda create -n rustdesk-mcp python=3.12 -y
conda run -n rustdesk-mcp pip install -r mcp/requirements.txt   # mcp>=2.0 + pillow
```

### 3. 连接配置

```sh
cp bridge.toml.example bridge.toml   # 仓库根目录，已 gitignore
```

`bridge.toml` 全部字段：

```toml
[connection]
id       = "1791144759"        # 必填。直连填 IP；走服务器填 peer id
server   = "hbbs.example.com:21116"  # 可选，自建 hbbs；省略则用公共/默认服务器
key      = "..."               # 可选，自建服务器的公钥
password = "被控端永久密码"      # 被控端 RustDesk → 设置 → 安全 → 永久密码

[video]
quality        = "balanced"    # best | balanced | low | custom
custom_quality = 50            # 仅 quality = custom 时生效（0..100）
fps            = 30            # 尽力值，受被控端限制

[ipc]
port = 21567                   # 本地 IPC 端口
```

被控端建议：设置永久密码、关闭"每次连接需确认"（无人值守前提）。

### 4. 接入 Claude Code

项目根 [.mcp.json](../.mcp.json)（按需改 conda python 路径）：

```json
{
  "mcpServers": {
    "rustdesk": {
      "command": "<conda环境python绝对路径>",
      "args": ["mcp/rustdesk_mcp.py"],
      "env": {
        "RUSTDESK_BRIDGE_PORT": "21567",
        "RUSTDESK_BRIDGE_BIN": "target/release/bridge",
        "RUSTDESK_BRIDGE_CONFIG": "bridge.toml",
        "RUSTDESK_SCREENSHOT_DIR": "~/rustdesk-screenshots",
        "RUSTDESK_IDLE_TIMEOUT": "600"
      }
    }
  }
}
```

重启 Claude Code 会话（或 `/mcp`）→ 启用 `rustdesk`。**不需要手动启动任何进程**。

---

## 二、环境变量参考

| 变量 | 默认 | 说明 |
|---|---|---|
| `RUSTDESK_BRIDGE_HOST` | `127.0.0.1` | bridge 的 IPC 地址（bridge 目前只监听回环） |
| `RUSTDESK_BRIDGE_PORT` | `21567` | IPC 端口，须与 `bridge.toml [ipc] port` 一致 |
| `RUSTDESK_BRIDGE_BIN` | 空 | bridge 可执行文件；相对路径按仓库根解析。**空 = 从不拉起**，要求已有 bridge 在跑 |
| `RUSTDESK_BRIDGE_CONFIG` | 空 | 拉起时传给 `--config` 的 toml |
| `RUSTDESK_SCREENSHOT_DIR` | `~/rustdesk-screenshots` | 截图落盘目录；空 = 不落盘 |
| `RUSTDESK_IDLE_TIMEOUT` | `600` | 空闲睡眠阈值（秒）；`0` 关闭 |

多设备：复制多份 `.mcp.json` 条目（不同 `RUSTDESK_BRIDGE_PORT` + 不同 toml），
每台设备一个独立命名的 server。

---

## 三、工具列表（12 个）

| 工具 | 参数 | 说明 |
|---|---|---|
| `status()` | — | 连接状态 / 分辨率 / 帧序号 / 是否等待 2FA |
| `screenshot()` | `wait_for_new_frame=false, max_width=1568, format="jpeg", crop_x/crop_y/crop_w/crop_h` | 返回图像 + 文本说明；自动落盘为 `<md5>.jpeg/png`（同画面去重）。`crop_*` 为原生坐标矩形：**先裁再缩放**，区域保真且更小，返回附注含 `crop_origin`（图上坐标 + 原点 = 原生坐标） |
| `ocr()` | `crop_x/crop_y/crop_w/crop_h` | Apple Vision OCR（需 `pip install pyobjc-framework-Vision pyobjc-framework-Quartz`，macOS）。返回 JSON：`{seq, crop_origin, items:[{text,x,y,w,h,score}]}`，原生坐标、阅读顺序排序——**读文字/找 UI 优先用它，省视觉 token 且坐标精确** |
| `tap_text(text)` | `index=0` | 一次往返：OCR 当前帧 → 找到含 `text` 的项 → 点击其中心（拟人化）。适合已知文案的按钮（如 `参加`、`A 新月步`）；多个匹配用 `index` 选第几个 |
| `mouse_move(x, y)` | — | 移动光标 |
| `click(x, y)` | `button="left", double=false, humanize=true` | 左/右/中键点击；默认拟人化（随机落点 ~4px + 抖动时序） |
| `drag(x1, y1, x2, y2)` | — | 拟人化多步拖拽 |
| `scroll(x, y)` | `dy=0, dx=0` | 一格滚轮；`dy>0` 向下，`dx>0` 向右；先移动到 (x,y) 再滚 |
| `key(name)` | `ctrl/alt/shift/meta=false` | 单键或组合键；`name` 为单字符或 `VK_*`（VK_RETURN、VK_ESCAPE、VK_F1…） |
| `type_text(text)` | — | 整串注入（被控端合成），中文/符号可靠 |
| `wait(seconds)` | — | 等动画/加载，0.2–2s 通常够 |
| `restart_bridge()` | — | 重启 bridge 以重载 `bridge.toml`（改完配置说一句即可） |
| `stop_bridge()` | — | 彻底断开并停止；之后任意工具调用会按需拉起 |
| `submit_2fa(code)` | `trust_this_device=true` | 应答被控端 2FA 挑战（见下） |

### ⚠️ 坐标系（最重要的使用约定）

**所有鼠标坐标使用远端原生分辨率**（`status` / screenshot 附注里的
`native_resolution`），不是缩放后图像的像素坐标。例如原生 1920×1080、
截图被缩到 1568×882：想点原生 (960, 540) 就传 960/540，不要按图片像素换算。

---

## 四、典型使用

### 基本循环（对话里直接说人话即可）

> "截个图看看远程现在什么样"
> "双击桌面上的『此电脑』，然后截图确认窗口打开了"
> "往下滚两格再截个图"

模型会自己组合工具。关键机制 **act-then-see**：操作后立即截图可能拿到
操作前的旧帧（RustDesk 只在画面变化时编码）。`screenshot(wait_for_new_frame=true)`
会等新帧（≤3s）；如果返回 `stale=true`，意思是"这段时间画面没变过"，
**不代表操作失败**。

### 2FA 流程

被控端开启 2FA 后，连接时 `status` 会显示 `pending_2fa=True`，模型会在
对话里向你要 6 位验证码（TOTP，30 秒有效，问完立即提交）。默认
`trust_this_device=true`：被控端（需开启"信任设备"）记住这台 Mac 后，
**以后连接免 2FA**——第一次人工过一下，之后完全无人值守。

### 改配置

改 `bridge.toml`（换设备 / 改画质 / 改密码）后，在对话里说"重启 bridge"，
`restart_bridge` 会优雅断开 → 按新配置重拉（无需手动 kill）。

---

## 五、生命周期（谁在什么时候活着）

| 场景 | 行为 |
|---|---|
| 首次工具调用 / 启动 | 端口空闲 → 自动 spawn `bridge --config bridge.toml`，日志 `/tmp/rustdesk-bridge-<port>.log` |
| 已有 bridge（手动 / Python brain 起的） | **复用，不接管**，退出时也不会杀它 |
| bridge 崩了 | 下一次工具调用自动重拉（对调用方透明） |
| 空闲超过 `IDLE_TIMEOUT`（默认 10 分钟） | **睡眠**：自己拉起的 bridge 被停止，RustDesk 会话释放，远程那头自由；代理常驻。下一条命令自动唤醒重连（约 2–8s） |
| Claude Code 会话退出 | 只清理自己拉起的 bridge（atexit + SIGTERM 链） |

配合效果：远程连接只在"活跃使用 + 至多一个空闲窗口"内被占用。

---

## 六、故障排查

| 症状 | 排查 |
|---|---|
| 工具报 "nothing is listening …" | `RUSTDESK_BRIDGE_BIN/CONFIG` 没配好，或二进制未构建 |
| `connected=False` | 看 bridge 日志的 msgbox 行：`Wrong Password`（密码错）、连接不可达（id/server）等 |
| `connected=True` 但 `frame_seq=0` 不涨 | 视频流不通：被控端缺屏幕录制权限 / 画面完全静止（正常现象，动一下就有） |
| 截图颜色不对 | 已修复过 libyuv 字节序问题；若再现，检查 `swizzle_rgba`（[src/bridge.rs](../src/bridge.rs)） |
| 首次唤醒后 `status` 显示未连接 | 正常——会话在握手，`status` 已内置 1.5s 平滑；稍等再查 |
| MCP server 起不来 | `conda run -n rustdesk-mcp pip install -r mcp/requirements.txt` |

日志位置：bridge → `/tmp/rustdesk-bridge-21567.log`；代理 → stderr（Claude Code 的 MCP 日志）。

---

## 七、安全须知

- 这个服务把远程机器的**完整键鼠控制权**交给模型。仅 stdio 传输、仅回环监听。
- `bridge.toml` 含密码，已 gitignore，**永远不要提交**；也不要把截图目录
  （`~/rustdesk-screenshots`，内容是远程屏幕）同步到公开位置。
- 2FA 验证码会经过对话与工具调用（TOTP 30 秒过期，风险可控）。
- 分发二进制受 **AGPL-3.0** 约束（RustDesk 上游协议）：需同时提供对应源码
  （公开本 fork 即可）。

---

## 八、开发者参考

- 文件：代理 `mcp/rustdesk_mcp.py`；bridge `src/bridge.rs`（+ `src/bin/bridge.rs`）；
  环境搭建/清理 [DEV-ENV.md](../DEV-ENV.md)。
- bridge IPC 协议：一行 JSON 请求；响应 `[type:u8][len:u32 BE][payload]`，
  `0x01`=JSON、`0x02`=`[w:u32][h:u32][RGBA]`。命令全集见 `src/bridge.rs` 头部注释
  （frame / status / send_2fa / tap / move / click / drag / scroll / key / type / quit）。
- 已知边界：仅操控被控端当前主显示器（多显示器切换未暴露）；滚轮符号已在
  代理层归一为自然约定（`dy>0` 向下）。
