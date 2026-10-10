# 本地脚本运行

事实源是仓库 `mcp/skills/rustdesk-fuben/`。脚本复用同仓库 `_framework` 的 Apple Vision 区域 OCR、Pillow 战斗锚点、拟人输入规划、MCP 会话和产物写入。没有在线识别、在线模型或本地大语言模型依赖。解释器从仓库 `.mcp.json` 读取（当前 conda `rustdesk-mcp` 环境）。若沙箱内 Vision 调用失败，需在允许访问本机 Vision 的环境运行。

## 计划

每窗目标 = `knight_runs`（周一默认 3，其余 2；配置或 `--knight N` 覆盖）+ `normal_runs`（默认 2）。先侠士后普通，都完成即停。侠义值/奖励不作判断依据。

## 校准和配置

**每次运行启动时自动重新定位窗口（任意设备）**：先对每设备截全屏，顶栏「指引/活动/排行」锚点吸附（跑马灯里的"活动"会被行位+罕用词过滤）为主，壁纸缝隙标题栏像素分段兜底（条带跟随配置行扫，2×2 四窗铺屏时不会误绑上排），冲突 >40px 时像素赢。配置 `rect` 只是尺寸提示；`autocal:false` 仅 mock 用。**例外**：2×2 四窗 + 屏幕右侧还有启动器面板的布局下，壁纸缝隙检测会把整行并成一个跨度、锚点吸附也会失败——此时用 `autocal:false` + 手工 rect（布局静态时完全可靠，2026-10-05 四窗布局实测识别/操作全通过）。复制 [配置示例](../assets/pc1.example.json) 填窗口尺寸/身份后设 `calibrated=true`。

## 命令

以下在仓库根目录执行。参考图有 25 像素标题栏，inspect 需裁剪：

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-fuben/scripts/fuben.py inspect --image images/副本/8.png --rect 0,25,768,575
```

只读 observe 两分钟：

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-fuben/scripts/fuben.py run --repo /Users/yanjoy/codeWorkSpace/vscode/rustdesk --config /private/tmp/fuben-pc1.json --output /private/tmp/fuben-observe --minutes 2 --no-viewer
```

用户已授权后执行（观察台默认随运行启动）：

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-fuben/scripts/fuben.py run --repo /Users/yanjoy/codeWorkSpace/vscode/rustdesk --config /private/tmp/fuben-pc1.json --output /private/tmp/fuben-run1 --execute
```

## 侠士准备（多设备自动）

点"进入"后队长出现等待界面，脚本同时开始扫配置里的 `members` 设备（如 `["pc1","pc2","pc3"]`——pc1 可与队长同设备：单连接、四窗一屏布局下，同设备的两个队员窗靠全帧弹窗扫描覆盖，无需 rect；缺省空列表=回退旧行为，人工点准备）。每个队员设备全帧截图 + OCR：找"准备"按钮的规则是 **`准?/淮?` 二字读数 + 同行左侧配对"拒绝"**——弹窗自带的成员 未准备/已准备 行不会配对（队长自己的等待界面同样不含拒绝、不误配），小窗（615px 级）下按钮误读成 淮备/准留 也能配上；全帧没配上时对每个"拒绝"行做 2× 放大重扫兜底。找到即拟人点击（mouse_move 后**重新截帧复核**弹窗仍在、落点仍在按钮框内才点——倒计时掐死弹窗时绝不会把点击漏到城市场景上）。

调度：与任一队长使用同一设备的队员在主循环内串行 sweep；其他独立设备由并行 worker sweep，间隔 `member_resweep`（默认随机 1.5–3 秒）。事件日志 `member_ready`，证据帧 `*-member-ready-before/after.png`，`state.json` 的 `members` 段累计各设备点击数。

若 20 秒倒计时先到而取消（sweep 没赶上或没找到弹窗），脚本随机延迟 8–20 秒重新点"进入"（弹窗会随重新排队再次出现，sweep 再点），**连续 3 次没成功即自动暂停**（`max_enter_retries` 默认 3；进入成功后配额重置）。连续第二把通常免准备直接进本；第三把必准备；两种路径脚本都按画面推进。

## 副本内推进

跳过剧情优先于任务条目；任务条目点击前要求坐标稳定 `navigation_delay`（默认 3 秒）；对话**几何点击**选项带（选项文字随剧情轮换，不作识别依据；读到文字时仅作更准的落点锚）；战斗只观察（战斗锚点兜底 OCR 闪烁）；奖励面板点"点击屏幕继续"区；"副本已完成"计一次并点击回城。对话/跳过点击后若画面处于转场（剧情过场可读 unknown 超一分钟），pending 宽限续期至 6 分钟而不是误判暂停。回城后 60 秒宽限内未见到灰框（`assist_grace`）也按已通关收尾（`missed_dungeon_end`），计数不回退。

## 运行与停止

- 点击拟人（OCR 目标内随机采样、mouse_move 多步轨迹、点击前新帧复核）；滚动分段随机；输入意图先落盘，未确认不盲重试（侠士排队重试是唯一显式例外，属设计）。
- 两窗独立；单窗异常（如面板无可用进入）只暂停该窗。未知画面 20 秒 warning 禁输入。开局或中途遇到已开副本自动收编（`adopted_open_dungeon`）。
- 观察台默认启动并回连校验；`--no-viewer` 关闭。`state.json` 原子写每窗 knight/normal 计数、目标与最近观察；`events.jsonl` 记输入与恢复事件；`timeline.jsonl` 逐窗巡检。
- 停止：说停或建 `<output>/STOP`；只终止本脚本 PID，不杀 bridge、不退游戏；等待中的进队请求由游戏倒计时自然取消。
- 每分钟 keep_alive；活动锁兼容剑会/门派/秘境/抓鬼（`fuben` 标签）。

## 离线验证

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-fuben/scripts/test_fuben.py
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-fuben/scripts/replay_fuben.py --output /private/tmp/fuben-replay
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-fuben/scripts/mock_e2e_fuben.py
```

mock 显式绑定 `_framework/mock.py`，不回退真实 fleet。结果见 [验证](validation.md)。
