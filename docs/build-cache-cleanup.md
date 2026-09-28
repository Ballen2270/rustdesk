# 构建缓存清理记录(target/)

> 最近一次清理:2026-09-18。日常清缓存直接看本文最后一节;完整卸载开发环境见 `DEV-ENV.md` 的「清理」章节。

## 2026-09-18 清理明细

| 项目 | 清理前 | 清理后 |
|---|---|---|
| `target/` 总计 | 2.9 GB | 21 MB |
| `target/debug/`(含增量编译缓存) | 1.3 GB | 已删除 |
| `target/release/`(deps/build/中间产物) | 1.6 GB | 已删除 |
| `target/release/bridge` + `spike` 二进制 | 22 MB | **保留** |

磁盘可用空间:清理前 59 GiB(数据卷 71% 已用)。

### 保留了什么、为什么

只保留了 `target/release/bridge` 和 `target/release/spike` 两个可执行文件(共 21 MB),其余全删:

- `.mcp.json` 里 `RUSTDESK_BRIDGE_BIN=target/release/bridge` —— MCP server(`mcp/rustdesk_mcp.py`)靠它自动拉起 bridge。删掉的话,下次用 rustdesk MCP 工具前必须先重新编译(`cargo build --release --bin bridge`,冷缓存约十分钟起步)。
- 两个二进制已用 `otool -L` 验证只依赖系统框架,不依赖 `target/` 内其它产物,单独保留可正常运行(冒烟测试输出 `[bridge] listening`,与 CI `spike.yml` 同款判据)。

下次 `cargo build` 会把它们视为过期产物直接覆盖,无需手动处理。要做**彻底**清理(连这两个二进制也删)见下文。

### 没动的东西

- `~/.cargo`、`~/.rustup`、`~/vcpkg`:本机不在标准路径上(实际工具链位置见 `~/.zprofile`),且属于全局工具链而非本仓库构建缓存。要卸载见 `DEV-ENV.md`。
- `flutter/.dart_tool`、`flutter/build`:当时不存在,无 Flutter 产物可清。
- `/tmp/rustdesk-bridge-*.log`:无残留。
- `libs/hbb_common` 等 submodule:属于仓库本身,永远不删。

## 日常清理手册(以后照抄)

```sh
# 常规:删掉所有构建产物但保留 bridge/spike 两个二进制(MCP 不中断)
cd /Users/ballen/codeWorkSpace/vscode/rustdesk
find target -mindepth 1 -maxdepth 1 ! -name release -exec rm -rf {} +
find target/release -mindepth 1 ! -name bridge ! -name spike -exec rm -rf {} +

# 彻底:全删(之后 MCP 首次用之前需 cargo build --release --bin bridge 重新编译)
cargo clean          # 或 rm -rf target

# 只想看各目录占了多少再决定:
du -sh target/* target/release/*
```

清理后验证保留的 bridge 还能跑(输出含 `[bridge] listening` 即正常,3 秒后自动杀掉):

```sh
(target/release/bridge 127.0.0.1 dummy & BPID=$!; sleep 3; kill $BPID 2>/dev/null) 2>&1 | head -5
```

> 注意:`target/` 整体在 `.gitignore` 里,以上操作不影响 git 状态;`generated_bridge.dart` 等 FFI 产物也在 ignore 列表,但它们属于"下次构建要重新生成"而非缓存,不要顺手去删。
