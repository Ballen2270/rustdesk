# 命令与配置

以下命令在仓库根目录执行。SKILL 指向已安装的 skill 目录，PY 指向 .mcp.json 的 Python。使用明确路径或任务专用变量，不修改系统环境变量。

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python /Users/yanjoy/.codex/skills/rustdesk-jianhui/scripts/jianhui.py inspect --image images/匹配中.png
```

inspect 可加 `--rect x,y,w,h`，指定**游戏内容**矩形；省略时将整张图作为游戏画面，带标题栏的参考图只用于初步识别验证。

配置字段见 assets/pc1.example.json。四个 ready_slots 是游戏内容归一化矩形；实际准备标记必须落在四个不同区域。panel_close 是任务面板游戏内 × 的归一化位置，不能指向 Windows 关闭按钮。live 截图宽度超过 2560 的窗口不支持，需先校准较小窗口。

ready_slots 可以放在每个 windows 条目内，优先于顶层默认值。它们必须排除该队房主所在槽位；房主站位和队长站位可能不同。当前 MCP SDK 使用 CallToolResult.is_error 字段。

```sh
# 只观察，运行目录必须为新目录；这里显式限制为两分钟
python /Users/yanjoy/.codex/skills/rustdesk-jianhui/scripts/jianhui.py run --repo /Users/yanjoy/codeWorkSpace/vscode/rustdesk --config /private/tmp/jianhui-pc1.json --output /private/tmp/jianhui-observe --minutes 2
# 明确授权开始后使用 --execute；试一场用 --games 1
python /Users/yanjoy/.codex/skills/rustdesk-jianhui/scripts/jianhui.py run --repo /Users/yanjoy/codeWorkSpace/vscode/rustdesk --config /private/tmp/jianhui-pc1.json --output /private/tmp/jianhui-run --execute --games 10
```

用终端长任务会话启动并保留 session_id；不要使进程脱离管理。run 本身是长循环，大模型无需持续调用截图。通过 state.json 看 mode、status、counts、last_observations、loop_seconds。events.jsonl 留存识别变化、操作意图和结果。latest-left.png / latest-right.png 为最近区域图，关键状态变化另存证据图。不把这些含账号信息的文件提交。

停止时用文件编辑工具创建 `<output>/STOP`，脚本每次请求/点击前及循环中检查。已有在途点击不能撤销；MCP 超时上限十二秒。SIGTERM 同样请求退出。

程序为这次运行单独启动 MCP stdio 代理，使用同一 fleet 配置。每分钟调用 keep_alive 为 pc1 续约五分钟；若另一个 MCP 代理拥有 bridge，则由操作者在其会话单独 keep_alive 覆盖预计时长，两个代理租约不共享。不要对另一个会话的桥接服务执行 stop/restart。

当前停止恢复流程：读取旧日志和每窗口完成数/active 状态，人工核对未决局次后，创建新运行目录。两个窗口已回到房间、没有未决局次时可设置 initial_counts 重新开始。目标 games 是包含 initial_counts 的本次总目标。不得把每周参赛进度当成本次场次。

若为修正识别而暂停，已依据新鲜截图确认两队仍在记录中的战斗，可显式使用 --resume-state checkpoint.json。检查点包含 config（与本次配置完全一致）、games 和 windows（两队已核实的 count、active、initialized）。不能残留 pending/paused，不自动清除未决点击；不可直接把未知状态当作 battle。恢复前必须确认旧进程退出，日志与新鲜截图足以证明计数未遗漏。若仍有歧义，等待核对后恢复。

战斗状态无时限；--minutes 默认 0，表示任务无总时限。显式指定正数时，到时停止监控和后续输入，不退出游戏或中断远端战斗。十场完成、用户停止及实际异常仍可结束运行。

## 本地识别与排错

每轮先 OCR 顶部与底部；房间增加准备区 OCR，漏字时放大对应队员槽。未知画面再放大回合区、执行全图 OCR，仍未知才对已经确认的战斗复核两块本地图像特征。所有步骤在控制端 Mac 执行，不需要联网推理或本地大语言模型服务。

`local_features.py` 每窗口最多保存八组内存图像模板；仅由连续确认的 OCR 战斗画面建立，房间、弹窗、结算或尺寸变化时清空。模板阈值是初始保守值，尚需新实战录像验证跨动画与遮挡鲁棒性。模板不能确认胜负或按钮可点击。

`recognition-left.json` / `recognition-right.json` 保存最新文字坐标、置信度、融合证据和识别耗时；state.json 的 observation.source 区分 roi_ocr、full_ocr_fallback、local_battle_anchors。未知画面不会结束观察，warning 表示不执行输入。网络/截图失败仍会暂停，不伪装成战斗正常。

离线回放（不连接 pc1）：

```sh
/opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python /Users/yanjoy/.codex/skills/rustdesk-jianhui/scripts/replay.py --images /Users/yanjoy/codeWorkSpace/vscode/rustdesk/images
```

回放核对六张单窗口图的状态、准备槽与模板对其他界面的误匹配，并输出本地识别耗时。这不包含 MCP 传输时间，不等同于实机端到端性能。首次试跑仍先短暂 observe，校准通过后再 execute。
