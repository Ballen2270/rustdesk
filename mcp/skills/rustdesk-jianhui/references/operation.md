# 命令与配置

以下命令在仓库根目录(`$REPO`,当前 `/Users/yanjoy/codeWorkSpace/vscode/rustdesk`)执行。skill 的事实源在 `$REPO/mcp/skills/rustdesk-jianhui/`;PY 指向 `.mcp.json` 的 rustdesk.command(当前 `/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python`)。`~/.codex/skills/` 下若有旧副本,仅作同步目标,不要在那里开发。

```sh
$PY $REPO/mcp/skills/rustdesk-jianhui/scripts/jianhui.py inspect --image images/匹配中.png
```

inspect 可加 `--rect x,y,w,h`,指定**游戏内容**矩形;省略时将整张图作为游戏画面,带标题栏的参考图只用于初步识别验证。

配置字段见 assets/pc1.example.json。四个 ready_slots 是游戏内容归一化矩形;实际准备标记必须落在四个不同区域,且两队房主站位不同,必须分别实测。panel_close 是任务面板游戏内 × 的归一化位置(pc1 实测 [0.904, 0.139]),用鼠标右键关闭;窗口未激活未关闭时脚本自动先左键激活再右键一次,仍失败则暂停;不能指向 Windows 关闭按钮。「所有人都已经准备……倒计时」踢出警告弹窗出现时自动点击「开始匹配」,45 秒未决暂停。live 截图宽度超过 2560 的窗口不支持,需先校准较小窗口。`room_timeout`(整数秒,≥10,默认 90)是等待队员准备的观察上限,队友较慢时在配置中放宽。

ready_slots 可以放在每个 windows 条目内,优先于顶层默认值。它们必须排除该队房主所在槽位;房主站位和队长站位可能不同。当前 MCP SDK 使用 CallToolResult.is_error 字段。

`bands`(顶层或每窗口)覆盖识别区域的归一化矩形,缺省值等于全屏 1920x1080 单窗口布局(DEFAULT_BANDS,见 jianhui.py)。窗口不是全屏时(如两客户端并排一块屏、2x2 四窗)所有相对位置都会偏移,必须按实测覆盖;键含 top/bottom/room_band/ready_y/owner/panel_*/result_*/kick_*/round/battle_*/modal。2026-10-04 pc1 半屏并排实测(两窗各约 958x1008,矩形含下方叠窗冗余但无害):`bottom=[0.78,0.63,0.22,0.12], room_band=[0,0.42,0.98,0.32], ready_y=[0.42,0.62], owner=[0,0.40,1,0.45], battle_cancel=[0.75,0.58,0.25,0.42]`,弹窗类(panel/result/kick)放宽亦可用;完整文件见当日运行配置。kickwarn 与 match 点击竞态会让 pending 卡死自 pause,但游戏倒计时会自行开始匹配,接管时用 active 按实况标注即可。

## 自动布局校准(全本地,无云端模型)

```sh
$PY $REPO/mcp/skills/rustdesk-jianhui/scripts/auto_layout.py --repo $REPO --keyword 雾上青天 --keyword 剑霜九洲 --out /private/tmp/jianhui-pc1.json
# 或离线:$PY .../auto_layout.py --image 全屏图.png --native 1920x1080 --keyword 甲 --keyword 乙 [--expect-windows 4]
```

一张全屏帧自动出整套配置:窗口标题 OCR 锚点→标题栏白带/列边缘定窗口矩形(切掉 Windows 标题栏,底部到任务栏/下一排窗口),每窗 OCR 实测天梯匹配按钮、成员名字行、房主标记(标记缺失时按房主名字模糊定位),生成 rect/ready_slots/bands 并通过 validate 自检;窗口纵横比偏离常见 4:3 会在 warnings 提示核对;两窗重叠(面积>15%)直接拒绝并提示先平铺。关键词匹配容忍 1 字 OCR 误读(≥3 字才模糊)。识别管线(Apple Vision OCR + Pillow 像素)全部本地,运行循环零在线请求。注意:真重叠(窗口盖窗口)像素上无解,必须先平铺;下采样 JPEG 会丢小字标题,优先 live 原生截图;剑会房间界面元素最全,不在房间时 slots 测不到,工具会警告。2x2 四窗的实测验证待下次真机。

```sh
# 只观察,运行目录必须为新目录;这里显式限制为两分钟
$PY $REPO/mcp/skills/rustdesk-jianhui/scripts/jianhui.py run --repo $REPO --config /private/tmp/jianhui-pc1.json --output /private/tmp/jianhui-observe --minutes 2
# 明确授权开始后使用 --execute;试一场用 --games 1
$PY $REPO/mcp/skills/rustdesk-jianhui/scripts/jianhui.py run --repo $REPO --config /private/tmp/jianhui-pc1.json --output /private/tmp/jianhui-run --execute --games 10
```

run 可选参数:`--viewer` 随运行拉起网页观察台(`--viewer-port`,默认 8788;端口被占时运行不受影响);`--keep-frames all` 额外按 `--frame-interval`(默认 5 秒,0 = 每轮)把关键帧存为 `frames/*.jpg`,操作前后帧与状态变化帧不受采样影响、永远保留;`--mcp-spec` 指向替代 mcpServers JSON,用于离线 mock,会跳过 fleet 配置检查。观察台截图证据区有「清理历史帧」按钮,手动清空 `frames/`(证据帧不受影响);运行目录在 /private/tmp 下,macOS 也会自动清理三天未访问的文件。

用终端长任务会话启动并保留 session_id;不要使进程脱离管理。run 本身是长循环,大模型无需持续调用截图。通过 state.json 看 mode、status、counts、last_observations、loop_seconds。events.jsonl 留存识别变化、操作意图和结果。timeline.jsonl 逐轮记录每窗口观察(状态、来源、准备槽、计数、动作、确认、识别耗时、锚点数),是复盘时序的数据源。匹配/关闭点击前后各存一张 `-before/-after` 截图并在 events 中引用;latest-left.png / latest-right.png 为最近区域图。不把这些含账号信息的文件提交。

停止时用文件编辑工具创建 `<output>/STOP`,脚本每次请求/点击前及循环中检查;网页观察台的停止按钮等价于此。已有在途点击不能撤销;MCP 超时上限十二秒。SIGTERM 同样请求退出。

程序为这次运行单独启动 MCP stdio 代理,使用同一 fleet 配置。每分钟调用 keep_alive 为 pc1 续约五分钟;若另一个 MCP 代理拥有 bridge,则由操作者在其会话单独 keep_alive 覆盖预计时长,两个代理租约不共享。不要对另一个会话的桥接服务执行 stop/restart。

当前停止恢复流程:读取旧日志和每窗口完成数/active 状态,人工核对未决局次后,创建新运行目录。两个窗口已回到房间、没有未决局次时可设置 initial_counts 重新开始。目标 games 是包含 initial_counts 的本次总目标。不得把每周参赛进度当成本次场次。核对无误后可用 checkpoint 子命令打包:

```sh
$PY $REPO/mcp/skills/rustdesk-jianhui/scripts/jianhui.py checkpoint --state /private/tmp/jianhui-run/state.json --out /private/tmp/checkpoint.json
```

它拒绝仍在运行或存在 pending/paused 的状态;产物即 `--resume-state` 所需格式(config 与本次配置完全一致、games、每窗口 count/active/initialized)。不能残留 pending/paused,不自动清除未决点击;恢复前必须确认旧进程退出,日志与新鲜截图足以证明计数未遗漏。若仍有歧义,等待核对后恢复。

战斗状态无时限;--minutes 默认 0,表示任务无总时限。显式指定正数时,到时停止监控和后续输入,不退出游戏或中断远端战斗。十场完成、用户停止及实际异常仍可结束运行。

## 网页观察台

只读观察,可挂在进行中的运行目录(实时),也可指向任意旧目录复盘:

```sh
$PY $REPO/mcp/skills/rustdesk-jianhui/scripts/viewer.py --run-dir /private/tmp/jianhui-run   # http://127.0.0.1:8788
```

默认只绑 127.0.0.1;`--host 0.0.0.0` 显式放开局域网(手机观察用),`--port` 可改。功能区:顶栏运行状态、双窗卡片(实时画面、状态徽章、计数进度、准备槽、**本地模型状态**——OCR 来源/耗时/P50/P95、战斗锚点模板数)、状态泳道、操作时序表(窗口过滤、只看动作/异常)、事件日志(连续重复条目自动折叠为 ×N,「只看关键」过滤掉状态噪声,「清空视图」只清屏显、磁盘 events.jsonl 永不改动)、截图证据画廊(操作前后对照、点击放大)。顶栏 ⚙ 设置面板:刷新频率、泳道/表格/日志容量存本浏览器;自动清理策略写入运行目录 GC.json,由 viewer 的 janitor 守护线程每分钟执行——**没有 GC.json 就从不自动删**(老运行目录打开即安全);白名单永远保留 latest-*.png 与操作前后帧,其余图片按年龄回收。唯一写操作是创建 STOP 文件与 GC.json/删除过期图片,与人工停止/人工清理等价;观察台不触碰 bridge 与设备输入。Claude Code 通过项目级 skill `.claude/skills/rustdesk-jianhui`(符号链接到本目录)发现本 skill。

## 离线端到端(不连接 pc1)

```sh
sh $REPO/mcp/skills/rustdesk-jianhui/scripts/mock_e2e.sh
```

用 mock_mcp.py(同一 MCP stdio 协议,剧本驱动两窗口状态机,基于 images/ 参考图)真实地跑一遍 `run --execute --games 1`,断言 complete、timeline、before/after 截图、点击送达与 viewer API,23 项检查。mock 把参考图缩放到窗口矩形,存在轻微形变——它验证管线与产物,识别精度以 replay.py 为准。

## 本地识别与排错

每轮先 OCR 顶部与底部;房间增加准备区 OCR,漏字时放大对应队员槽。未知画面再放大回合区、执行全图 OCR,仍未知才对已经确认的战斗复核两块本地图像特征。所有步骤在控制端 Mac 执行,不需要联网推理或本地大语言模型服务。

`local_features.py` 每窗口最多保存八组内存图像模板;由 OCR 确认的战斗画面建立并按图像距离去重(单帧确认即学习,非连续帧要求),房间、弹窗、结算或尺寸变化时清空。模板阈值是初始保守值,尚需新实战录像验证跨动画与遮挡鲁棒性。模板不能确认胜负或按钮可点击。

`recognition-left.json` / `recognition-right.json` 保存最新文字坐标、置信度、融合证据和识别耗时;state.json 的 observation.source 区分 roi_ocr、full_ocr_fallback、local_battle_anchors。未知画面不会结束观察,warning 表示不执行输入。网络/截图失败仍会暂停,不伪装成战斗正常。

离线回放(不连接 pc1):

```sh
$PY $REPO/mcp/skills/rustdesk-jianhui/scripts/replay.py --images $REPO/images
```

回放核对六张单窗口图的状态、准备槽与模板对其他界面的误匹配,并输出本地识别耗时。这不包含 MCP 传输时间,不等同于实机端到端性能。首次试跑仍先短暂 observe,校准通过后再 execute。

## 离线测试套件

```sh
$PY $REPO/mcp/skills/rustdesk-jianhui/scripts/test_jianhui.py   # 状态机 + checkpoint + 命名
$PY $REPO/mcp/skills/rustdesk-jianhui/scripts/test_viewer.py   # 观察 API 与路径防护
```

修改 runner/识别逻辑后先跑这两套与 replay,再考虑实机。
