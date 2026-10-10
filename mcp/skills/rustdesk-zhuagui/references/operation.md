# 本地脚本运行

事实源是仓库 `mcp/skills/rustdesk-zhuagui/`；个人技能目录是指向它的链接。脚本从文件真实路径找到同级 `_framework`，复用剑会/门派/秘境的 Apple Vision OCR、Pillow 战斗锚点、拟人输入规划、MCP 会话和产物写入工具。没有在线图像识别、在线模型调用或本地大语言模型依赖。

解释器从仓库 `.mcp.json` 的 `mcpServers.rustdesk.command` 读取。当前为 `/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python`，已经包含 Pillow、Vision/Quartz 和 MCP SDK。若沙箱内 Vision 调用失败，需在允许访问本机 Vision 系统服务的执行环境运行；不要因此改为连接在线识别服务。

## 校准和配置

**每次运行启动时自动重新定位窗口（任意设备）**：脚本先对每个设备截全屏，用游戏顶栏「指引/活动/排行」锚点把每个窗口的内容矩形重新吸附到实际位置；顶栏不可读（如两窗都在战斗画面）时退回深色标题栏像素扫描（扫描条带跟随配置行，2×2 四窗铺屏时不会误绑上排）。配置里的 `rect` 只是尺寸提示，位置不信任旧值；窗口被挪过、launcher 重排、换设备都能自愈，吸附结果写入 `state.json` 和 events。配置 `autocal:false` 可关闭（仅 mock 测试用）。复制 [配置示例](../assets/pc1.example.json) 到本地临时目录，填写窗口尺寸/身份后设 `calibrated=true`。`identity` 是操作者校准时记录的绑定信息，脚本不宣称能够自动核验艺术字角色名。

支持一个或两个窗口，分别设置 `rounds` 和 `mode`。省略单倍 `rounds` 时默认 5。双倍窗口设置 `mode="double"`、该账号的 `double_points`，并在当前游戏界面核对模式后设 `reward_mode_confirmed=true`。删除示例的 `rounds` 字段可由点数推导完整轮数；180 点推导 4 轮，留 20 点。保留 `rounds` 或指定 `--rounds` 时明确指定目标，超过点数预算会拒绝启动。`--rounds 1` 是每个受控窗口本次目标 1 轮；零表示不覆盖配置。

`double_points` 是本次任务开始时的预算，按完成轮数累计 `estimated_spent`，不会修改游戏内点数。九张素材未包含双倍开关，所以脚本不切换单/双倍、不领点数、不购买点数；单倍开始前也应核对游戏配置。实际点数与估算不符时先核对，不把估算当作实时读取。

## 命令

以下在仓库根目录执行。参考图片有 25 像素 Windows 标题栏，inspect 的 `--rect` 指定内容裁剪：

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-zhuagui/scripts/zhuagui.py inspect --image images/抓鬼/3.png --rect 0,25,767,575
```

只读 observe 两分钟；不会发送点击、滚轮或领取：

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-zhuagui/scripts/zhuagui.py run --repo /Users/yanjoy/codeWorkSpace/vscode/rustdesk --config /private/tmp/zhuagui-pc1.json --output /private/tmp/zhuagui-observe --minutes 2
```

用户已要求开始/试跑时运行一轮，运行目录必须是新目录：

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-zhuagui/scripts/zhuagui.py run --repo /Users/yanjoy/codeWorkSpace/vscode/rustdesk --config /private/tmp/zhuagui-pc1.json --output /private/tmp/zhuagui-one-round --rounds 1 --execute --viewer
```

按配置完成每日任务时去掉 `--rounds 1`，换新运行目录。默认无任务总时限；战斗本身没有超时。终端启动后保留长任务 session_id，不使进程脱离管理。**只读观察台默认随运行启动**（复用剑会 viewer，默认从 `http://127.0.0.1:8792` 起找空闲端口，启动行会打印并回连校验后的地址；`--no-viewer` 关闭）。每窗实际目标看 `state.json` 的 `windows[].target`，观察台公共目标字段只是最大目标。

执行模式从任意画面开始：开局或中途遇到已领取的战斗/任务会**收编**（`adopted_open_round`），其轮末弹窗仍算有效完成证据；无主的轮末弹窗不计数，由倒计时自行取消后从菜单/活动面板重新领取。observe 可只读检查这些状态。

## 运行与停止

- 点击落点使用 ClickPlanner 在 OCR 目标内随机采样，先用 MCP `mouse_move` 执行 bridge 的随机多步轨迹，再截图复核目标和位置，点击使用 `humanize=true` 的落点抖动与随机按压。滚轮由 ScrollPlanner 分段并随机漂移锚点、随机间隔，bridge 在每段滚轮前拟人移动。点击意图与 pending 先落盘；按钮请求超时暂停核对。
- 自动领取后先观察是否已经移动，再点击任务。**领取确认后先在场景空白处（左侧约 26%–33% 宽、42%–48% 高的安全点）点一下关闭钟馗残留对话，再走导航逻辑**——对话开着时点任务面板不会触发寻路。导航补点最多一次，且仅限持续识别到同一任务、同一地图坐标、原地不动的新帧；点击瞬间坐标读不出时，以点击后持续可读且不动的坐标为基准。坐标始终读不出时不补点；未确认的其他输入不重试。地图坐标行是小字号，Apple Vision 常把全角逗号读成「.」「、」或拆成两条，坐标正则按误读容错，MAP 区域做 3 倍放大二次识别。
- 用户可能临时手动点开背包、菜单等画面再恢复：轮内任务短暂消失（normal/claim_menu）不再立刻暂停，宽限期 `assist_grace` 默认 60 秒，期间不发送输入；持续超时才暂停核对。
- 自动战斗、通常十场及追加千年厉鬼都只观察。只认本轮末提示计一次，完成目标立即停止该窗口，不再点续轮确定；最终弹窗留给游戏倒计时或用户处理。未达目标时，轮末提示首次确认后独立抽取 10–40 秒等待，再点“确定”；等待期间继续巡检两窗并响应 STOP，不长时间阻塞 sleep。
- 两窗独立计数及暂停，单个窗口错误不阻塞另一队。窗口外形/分辨率变化、掉线暂停；轮内任务在地图/菜单上超过 `assist_grace`（默认 60 秒）视为漏看轮末（`missed_round_end`）：**不计轮但按 40 点记账消耗**，自动重新领取续跑，不再冻结（2026-10-02 实跑：轮末弹窗被陌生界面遮挡 6 分钟未被识别，倒计时自动取消后旧版直接暂停卡死）。未知画面持续二十秒提示 warning 并禁输入，识别恢复可接续。
- 活动搜索先小幅向上，再向下，最多 `max_scrolls` 次意图；每个滚轮分段前重截图确认仍在活动面板。同名“参加”须唯一绑定同一行和同一列。
- `state.json` 原子写进度、目标、点数估算、最新观察及 pending；`continue_delay` 保存本轮随机秒数，`continue_at` 为本机单调时钟截止值，不是日历时间；`events.jsonl` 记录状态变化和点击前后证据，`timeline.jsonl` 逐窗巡检记录，`latest-*.png` 和 `recognition-*.json` 用于排错。含账号数据的产物不提交。
- 每次点击及每段滚轮保留 `*-before.png` / `*-after.png`；后续状态确认也可保留 after 帧。巡检帧只覆盖 latest，不累积状态截图。运行结束、停止或暂停时只清理本次目录内非 before/after 的 PNG，不删除日志、配置、素材或其他运行目录。
- 每分钟 keep_alive 为配置设备续约五分钟。轮询由本地脚本执行，模型只需读取进度并检查异常，不承担每秒识别。
- 运行持有 pc1 及显示设备的互斥锁，并兼容旧剑会/门派/秘境锁。它不能拦截用户或外部 MCP 手动输入，同一台 pc1 不同时执行别的自动活动。
- 用户停止时创建 `<output>/STOP`，检查 `state.json` 为 `stopped`；必要时只对本脚本 PID 发 SIGTERM，不终止 bridge。停止后不再发送游戏输入，已在途输入无法撤销。
- 不自动从旧目录恢复。重启先核对旧日志和 pending；确认无未决轮次后，按各窗口剩余轮数及剩余点数建立新配置与新目录。不能直接丢弃旧 pending 再重跑全目标。

## 离线验证

这些命令不会连接 pc1。回放输出必须使用新目录：

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-zhuagui/scripts/test_zhuagui.py
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-zhuagui/scripts/replay_zhuagui.py --output /private/tmp/zhuagui-replay
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python mcp/skills/rustdesk-zhuagui/scripts/mock_e2e_zhuagui.py
```

回放走运行时相同的区域 OCR/放大/全图兜底管线，保存原图哈希、裁剪矩形、OCR 项、状态、目标框叠加图。mock 端到端测试显式绑定 `_framework/mock.py`，不会回退真实 fleet。它用本地临时裁剪图模拟两窗各两轮、11 场战斗、续轮领取、双倍预算、observe 零输入和 STOP，不验证真实千年厉鬼画面的 OCR。

九张原图回放通过仍不能证明实机布局、时序和陌生异常可靠。首次真机使用仍先校准/observe，再按用户授权试一轮。验证摘要见 [离线结果](validation.md)。
