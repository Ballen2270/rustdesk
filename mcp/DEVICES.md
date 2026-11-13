# 设备配置指南（fleet 模式）

> 本文档进仓库，**不含任何密码**。真实配置（`mcp/devices.toml` 和
> `mcp/devices/*.toml`）已被 gitignore，永远不要提交。

## 文件结构

```
mcp/devices.toml            设备清单：每台一个 [[device]] 块
mcp/devices/<name>.toml     每台设备的连接配置（单设备格式）
mcp/devices.toml.example    清单模板
bridge.toml.example         单台设备连接配置模板
```

`.mcp.json` 里 `RUSTDESK_DEVICES_CONFIG = "mcp/devices.toml"` 启用 fleet 模式；
删掉该变量或留空则回到单设备模式（`bridge.toml`）。

## 新克隆后的初始化

```sh
mkdir -p mcp/devices
cp mcp/devices.toml.example mcp/devices.toml
cp bridge.toml.example mcp/devices/<name>.toml   # 每台一份
```

注意：`devices.toml` 里每个 `[[device]]` 的 `config` 文件都必须存在，
没配好的设备先把那一整块删掉，否则 MCP server 启动即报错。

## 添加一台新设备

### 第 1 步：在被控端机器上取三个值

| 值 | 哪里取 |
|---|---|
| `id` | 被控端 RustDesk 主界面左侧的大号 ID（自建服务器填 ID；局域网直连可填 `IP:端口`） |
| `password` | 被控端 **设置 → 安全 → 永久密码**（无人值守前提；关闭"每次连接需确认"） |
| `server` / `key` | 自建 hbbs 的域名:端口和公钥；被控端走公共服务器则**删掉这两行** |

### 第 2 步：建连接配置 `mcp/devices/<name>.toml`

```toml
[connection]
id = "被控端的ID"
server = "hbbs.example.com:21116"   # 公共服务器则删除本行与 key 行
key = "hbbs公钥"
password = "被控端永久密码"
force_relay = false                  # true 时强制通过 RustDesk 中继连接

[video]
quality = "balanced"                 # best | balanced | low | custom
custom_quality = 50                  # 仅 quality = custom 时生效
fps = 30

[ipc]
port = 21568                         # 本机端口，每台唯一，从 21568 递增
```

### 第 3 步：在 `mcp/devices.toml` 追加

```toml
[[device]]
name = "pc1"
port = 21567
config = "mcp/devices/pc1.toml"
idle_timeout = 600
# 可选：共享设备网关（推荐，多进程/多 skill 同时用一台时）
gateway_port = 22567
```

`gateway_port` 启用 `mcp/device_gateway.py`：该设备只由网关一个进程持有
bridge（spawn/睡眠/停止），**所有 MCP 会话的输入在网关内全局串行**，解码帧
也在网关内缓存共享（同一帧只从 bridge 取一次）。它解决了三个老问题：跨
进程点按交错、某个会话的空闲租约睡掉别人正在用的连接、会话退出杀掉共享
bridge。网关按需自动拉起，空闲（无 bridge 且 5 分钟无请求）自行退出；每台
设备一个唯一端口（建议 bridge 端口 + 1000）。不写 `gateway_port` 则保持
直连 bridge 的旧行为。

第二台（每台一个 `[[device]]` 块）：

```toml
[[device]]
name = "pc2"                # 工具调用的 device 参数，起好记的名（全局唯一）
port = 21568                # 必须与上面 [ipc] port 一致（校验项，不一致启动报错）
config = "mcp/devices/pc2.toml"
gateway_port = 22568        # 可选；每台唯一，不能与任何 bridge/gateway 端口重复
```

### 第 4 步：生效与验证

- 对话里说"重启 bridge"，或重开会话
- `list_devices()` 查看所有设备状态（不唤醒任何设备）
- 首次对某台调用任意工具（如 `status(device="pc2")`）自动拉起连接（约 2-8s）

### 冷启动首帧行为（新 bridge 二进制）

- bridge `status` 新增字段：`display`（最新解码帧的显示索引）、`target_display`
  （设备配置 `display` 或 0）、`connected_for_s`（会话年龄秒数，断开为 null）、
  `error`（最近 msgbox 错误）。旧二进制不带这些字段，Python 侧自动走老路径。
- 首帧门控：冷启动等 `display == target_display` 才收帧；若 `connected_for_s < 3.5s`
  最多再等 ~4s（对端 RustDesk 连接面板 3s 后自动最小化，避免首帧被面板遮挡）。
- 等待失败按证据分类报错：`host is waiting for a 2FA code`（先 `submit_2fa`）、
  `session dropped (error=…)`（会话已断）、`video stopped after seq=N`（流冻结，
  网关模式下自持桥 60s 自动重启）、`video is arriving from display X but
  target_display=Y`（多屏主机把 `[display]` 设为实际屏号）。
- 网关帧缓存按 `(generation, seq)` 键控：桥重启（generation 变化）后绝不返回
  旧会话的帧。

## 生命周期字段（`[[device]]` 块内）

| 字段 | 默认 | 说明 |
|---|---|---|
| `autostart` | `false` | 按需连接；`true` = MCP server 启动即预热 |
| `idle_timeout` | `600` | 租约秒数：每次工具调用自动续；静默超过该值自动断开释放对端。`0` = 永不休眠 |
| `max_duration` | `0` | 硬上限：连接总时长超过该值即断（防"持续活跃永不放"）。`0` = 关闭 |
| `gateway_port` | 无 | 共享设备网关端口（见第 3 步）。不写 = 直连 bridge |

运行时工具：

| 工具 | 用途 |
|---|---|
| `keep_alive(device, minutes)` | 显式续租（可越过 `max_duration`）——远端脚本在跑但本地没有工具调用时用 |
| `stop_bridge(device)` | 干完活立即断开该设备 |
| `restart_bridge(device)` | 重载该设备的 toml（改完配置用它，无需重启会话） |

## 规则与行为

- `name`、`port` 全局唯一；`port` 必须与该设备 toml 的 `[ipc] port` 一致
- 每台设备**独立**休眠 / 重启 / 崩溃，互不影响；同一设备内动作串行，跨设备并行
- 手动起的外部 bridge 只复用，绝不杀、绝不休眠（`stop_bridge` 也只动自己拉起的）
- `host` 仅支持回环地址；非回环的 host 要求远端自己跑着 bridge，本 fleet 不会去远端拉起
- 配置类型严格校验：`autostart` 必须是真布尔（TOML 里不加分号的 `true`/`false`），
  数值必须 ≥0 且有限

## 测试

```sh
conda run -n rustdesk-mcp python mcp/test_fleet.py   # 44 项检查，占 21601-21603 端口
```
