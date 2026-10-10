# 梦幻西游统一启动

全部在 Mac 控制端完成：RustDesk 截图 → 本地 Apple Vision/Pillow 发现窗口 → 选择目标 → 活动脚本运行。不调用 Windows PowerShell，不要求远端代理；`windows/` 的客户端启动工具不参与这条链路。

| 活动 | 选择目标 |
| --- | --- |
| 抓鬼、副本、门派 | 选择已组队的队长；支持同连接任意窗口，也支持 pc1 一个、pc2 一个 |
| 秘境 | 单人活动，选择需要执行的账号窗口，不判断队长；窗口数量不设固定上限 |
| 剑会 | 通常选择 pc1 的两个房主，用固定房间界面核验；保留准备槽和匹配流程 |

## 发现窗口

使用 `.mcp.json` 配置的 Python 环境，在仓库根目录执行只读扫描：

```sh
python mcp/skills/_framework/window_targets.py scan --repo "$PWD" --device pc1 --device pc2 --output /tmp/mhxy-scan
```

只扫 pc1 时省略第二个 `--device`。工具通过 RustDesk MCP 获取原生新全屏截图，以游戏标题栏 OCR 和像素边缘发现窗口，再用未参与定位的游戏顶栏或房间标题复核候选。输出每设备的 `desktop.png`、`w1.png` 等窗口图以及 `windows.json`（坐标和 OCR 原文）。不发送点击、键盘、滚动或最小化请求。

有本地原生全屏截图时，可完全离线发现：

```sh
python mcp/skills/_framework/window_targets.py inspect --device pc1 --image /path/desktop.png --output /tmp/mhxy-scan/pc1
```

发现依赖标题栏可读、约 4:3 的窗口和独立游戏锚点。标题缺失、战斗中无启动锚点、边界含糊或目标被遮挡时明确失败，不猜坐标。四窗保持显示即可，但须平铺，不互相遮挡。当前 MCP 全屏截图宽度上限 2560；新入口拒绝缩小过的截图，超出上限的显示器暂不支持。合成四窗图已验证能发现四个候选，边界仍存在像素误差；真实布局须只读核对裁剪图，不把合成图当成可直接执行的精度保证。

## 选择队长或账号

查看窗口图后选出队长；普通地图不能可靠证明队长身份，所以由用户明确选择，而不是靠“活动”按钮推断。候选 `w1` 等编号只代表本次截图的位置排序，不能作为跨启动账号身份。

```sh
python mcp/skills/_framework/window_targets.py select \
  --activity zhuagui \
  --template mcp/skills/rustdesk-zhuagui/assets/pc1.example.json \
  --inventory /tmp/mhxy-scan/pc1/windows.json \
  --inventory /tmp/mhxy-scan/pc2/windows.json \
  --target pc1:w3 --identity 队长甲 \
  --target pc2:w1 --identity 队长乙 \
  --output /tmp/zhuagui-selected.json
```

名字替换为截图中实际可读的完整 OCR 文本。`--target` 与 `--identity` 按顺序配对。模板第一窗的轮数、双倍预算等计划分配给第一个目标，第二窗分配给第二个目标；生成后核对每账号的计划，换队长时不能沿用错误点数。

抓鬼、副本、门派最多两个队长。副本的 `members` 单独保留，表示扫描准备弹窗的设备。剑会仍为两个房主，`--identity` 使用完整房间标题，自动生成 `room_keyword`；窗口大小变化时准备槽和识别区域仍须通过原有剑会校准工具核对。

秘境可重复 `--target` 选择所有要跑的窗口，省略 `--identity`；此时只绑定本次位置，布局变化必须重新扫描选择。提供角色身份后则可跨启动按身份重新匹配。

队伍活动默认对客户区顶端 30% 两倍放大 OCR 核验身份，要求原文精确匹配（忽略空格）且置信度至少 0.5。稳定标识在别处时，在生成配置的对应窗口设置 `identity_box: [x,y,w,h]`（归一化坐标）。没有可读唯一身份时先展示身份，不模糊猜测，不要求隐藏其他正常窗口。截图/账号配置留在本地，不提交仓库。

## 运行与兼容

使用活动原有 `run --repo ... --config /tmp/zhuagui-selected.json --output ...` 命令，先 observe 核对，按用户授权添加 `--execute`。窗口、设备、轮数变化只改配置，不改脚本。

五个 runner 均在 `window_selection:true` 时重新截取新帧并发现所有候选，按设备+身份唯一匹配所选目标，更新矩形和每设备分辨率。输出 `startup/<device>.png`、`selected-targets.json`。身份缺失/重复、两目标指向同一窗口或目标被遮挡时，在活动输入前失败。抓鬼/副本跳过原来的最近位置吸附，避免覆盖已确认身份。

旧配置不含 `window_selection` 时走原启动路径。`calibrated:true` 只是配置已填写，不代表自动识别了队长。

每台电脑使用一个 RustDesk 连接、一个屏幕，以 pc1、pc2 等设备名区分。同连接内的多窗口输入仍串行。副本队员扫描与任一队长使用同一设备时加入串行循环；独立设备的队员扫描由后台任务执行，不再按设备名前缀分组。

启动后不移动窗口；此次不重写运行中的漂移处理。STOP、pending、计数、战斗锚点、未知画面禁输入和输入前复核保持各活动原逻辑。制作与离线验证不授权真机执行。

验证结果、已知失败和回归面见 [启动验证](STARTUP-VALIDATION.md)。
