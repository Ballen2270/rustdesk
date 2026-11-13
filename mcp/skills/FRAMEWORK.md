# RustDesk 自动化 skill 框架设计

以 `rustdesk-jianhui`(剑会)+ 网页观察台为原型,沉淀成可复用模板,让新自动化(更多任务、更多游戏、更多设备)只写"差异部分"。

## 一、现状盘点:通用 vs 专属

**已经通用(直接复用,不再改动):**
- 底层设备面:`rustdesk_mcp.py` + `bridge` — screenshot/ocr/click/key/type/scroll/keep_alive,fleet 多设备,租约管理
- 观察台:`viewer.py + viewer.html` — 只读 run 目录,不关心状态含义(仅状态→颜色映射需参数化)
- 产物协议:`state.json / events.jsonl / timeline.jsonl / latest-*.png / {ns}-{win}-{tag}.png / frames/`
- 识别原语:Apple Vision OCR、区域放大 OCR、颜色校验、模板锚点(`local_features.py`)

**剑会专属(需要从 jianhui.py 中抽出来变成"声明"):**
- 状态定义:room/ready/matching/battle/result/panel/kickwarn 及其 OCR 签名(文本+归一化框+阈值)
- 动作定义:match/close/rematch 的点击目标、鼠标键、确认条件
- 策略:两帧去抖及例外(结算单帧计数)、超时表、计数语义(先 battle 后 result 才 +1)、unknown 处理、漏结算暂停
- 配置:room_keyword / ready_slots / panel_close / room_timeout

## 二、目标形态:三层结构

```
mcp/skills/
  _framework/            ← 运行时(Phase A/B 从 jianhui 抽取,一次性)
    vision.py            # ocr / region_ocr / color / TemplateAnchors
    session.py           # MCP stdio 会话 + 设备调用 + 超时 + keep_alive 租约
    artifacts.py         # run 目录:events/timeline/state/证据帧(原子写)
    runner.py            # 核心循环 + 安全不变量(见四)
    policy.py            # 去抖/超时/计数/unknown 策略引擎(数据驱动)
    mock.py              # 通用 mock MCP:剧本 = {状态: 图片} + 点击迁移表
    console/             # viewer.py + viewer.html(状态色从 state_meta 读)
  rustdesk-jianhui/      ← 实例 = SKILL.md + profile.py + 参考图 + 测试
    profile.py
  rustdesk-<next>/       ← 以后每个新自动化 ≈ 1 个 profile + 文档 + 图
```

## 三、Profile:新 skill 要写什么

一个 Python 文件,纯声明(表达能力够,又不引入 YAML 解析):

```python
from _framework import Profile, State, Click, Key, Type

profile = Profile(
    name='jianhui',
    windows=2,                                   # 任意数量,串行输入
    config_extra={                               # 公共字段(device/rects/resolution)之外
        'room_keyword': str, 'ready_slots': list, 'panel_close': list, 'room_timeout': int,
    },
    states={
        'panel':   State(text=[('每周任务', (.25,.05,.45,.2)), ('战斗历程', (.50,.15,.4,.15))]),
        'kickwarn': State(text=[('开始匹配', (.50,.52,.30,.18)), ('我再等等', (.24,.52,.26,.18))],
                          button='开始匹配'),     # 该态的可点按钮 → 坐标来自 OCR
        'ready':   State(room_required=True, button=('天梯匹配', (.78,.89,.22,.11)),
                         slots='ready_slots'),    # 4 槽全绿才算
        'battle':  State(any_of=[toolbar3of5, round_and_cancel], maintain_only=True),  # 锚点只续不启
        'result':  State(text=[('胜负结果', (.3,.03,.4,.18)), ('数据统计', (.3,.75,.4,.2))]),
        ...
    },
    actions={
        'match':   Click(button_state='ready'),   # 点该态 button 的 OCR 中心,left
        'rematch': Click(button_state='kickwarn'),
        'close':   Click(config='panel_close', button='right', escalate=('left','right')),
    },
    policy=dict(
        count_on='result', count_gate='battle_active',   # 先观察到战斗,结算才 +1
        debounce={'*': 2, 'result': 1},                  # 单帧例外按态声明
        timeouts={'room': 'room_timeout', 'matching': 600, 'result': 90, 'kickwarn': 45},
        confirm={'match': ['matching','battle'], 'close': ['room','ready'], 'rematch': ['room','ready','matching','battle','panel']},
        on_return_without_count='pause',                 # 漏结算 → 暂停核对
        unknown_after=20,                                # unknown 20s → warning,禁输入
        battle_timeout=None,                             # 战斗无时限
    ),
)
```

**新 skill 的最小清单**:① 参考图(每个状态 1-2 张)② profile.py(状态签名+动作+策略,大多从模板改)③ SKILL.md(意图/边界/停止/试跑阶梯,模板化)④ replay 期望表 ⑤ mock 剧本 ⑥ 真机校准。框架负责其余一切。

## 四、安全不变量:框架强制,profile 不可绕过

这些是剑会三轮实战验证过的资产,全部下沉到 runner,任何 profile 都自动继承:

1. observe/execute 两级授权;首帧必须是就绪类状态,拒绝"半路接管"
2. 点击前重截屏复核状态一致;pending 未确认不盲试(确认窗/超时暂停)
3. 两帧去抖(例外按态声明,如结算单帧);所有输入串行;单实例 fcntl 锁
4. 计数完整性:未见结算回房 → 暂停,不猜数;同局去重
5. unknown 持续 → warning 禁输入;截图分辨率/裁剪校验,变了即停
6. STOP 文件 + SIGTERM;操作前后证据帧强制落盘;timeline 全量;keep_alive 续约
7. 循环耗时上界;状态产物原子写;离线(mock/replay/单测)三件套先于真机
8. **启动对位检查**(2026-09-28 加,menpai 首跑教训:窗口位置会变,绝不信旧校准):
   启动时每个配置矩形必须真的框住游戏客户端(顶栏 活动/排行/指引 至少一个可读),
   否则停机并把该设备全屏存为 `misaligned-<device>.png` 供重新校准;模型侧流程同样
   强制"每次启动先重新截图核对,不问用户"。固定坐标锚点(如活动按钮)启动时从
   OCR 现场重推导,其余点击都落在 OCR 项上、天然抗漂移。menpai 已全量实装,
   jianhui/mijing 已实装存证分支。

## 五、Console / Mock / 测试的通用化

- **Console**:`state.json` 增加 `states_meta`(profile 写入:{state: {label, color, actionable}}),前端颜色/徽章/泳道全走数据,不再内置 jianhui 状态;其余已通用
- **Mock**:通用 mock 读剧本文件 `{window: [(state, image, 持续秒), ...]} + 点击迁移表`,新 skill 的离线端到端 = 一份 JSON
- **测试**:框架提供表驱动测试台(`feed(['battle','result','result']) → 期望动作/计数`),实例只需写场景行;replay 模板化(期望表即测试)

## 六、迁移路径(不破坏现有剑会)

- **Phase A**:抽 `_framework/{vision,session,artifacts}.py` + console,jianhui 改 import,行为零变化(单测/回放/mock e2e 全绿为准)
- **Phase B**:抽 runner+policy,jianhui 改写为 profile.py + 薄入口;现有 24 项单测迁移为表驱动,语义必须逐一复刻
- **Phase C**:写第一个新 skill(真实消费者,反推框架缺口)
- **Phase D**:观察台多 run 切换/历史列表、profile 热校准工作流(inspect 辅助测量槽位)

### Phase A/C 落地记录(2026-09-20,分支 patch/mhxy-skills)

- `_framework/` 已建:`vision`(OCR/区域放大/绿底/战斗锚点,自 jianhui 逐字节平移)、`artifacts`(原子写/JSONL)、`session`(MCP stdio + keep_alive 租约)、`humanize`(点击/滚轮拟人:内区采样、坐标历史去重、随机延迟、分段滚轮)、`workflow`(声明式 Screen 签名 + Node 节点引擎)、`mock`(剧本驱动离线 MCP,几何盒优先/OCR 文本兜底解析点击)。
- jianhui 仅改 import(vision/artifacts),`ocr()` 等已核对逐字节一致;**剑会 session/policy 迁移推迟到 Phase B**——本机 Vision 对 `匹配界面单个.png` 1x 标题识别漂移(3x 可读,置信 1.0),属拆分前既存环境问题,按"行为零变化"原则本轮不动剑会识别逻辑;其单测 27 项 + viewer 7 项仍全绿。
- 首个 workflow 引擎消费者:`rustdesk-mijing`(秘境降妖,单人活动)。 Screen 声明顺序=优先级(弹窗>菜单>面板>场景);竖排"继续挑战"按钮 OCR 产出分离 item,签名锚定 继续+挑战 双词;`find_row_button` 按中心距行带匹配(游戏按钮略低于行文字,包围盒重叠不可靠)。

## 六点五、视觉吞吐与多设备规模(2026-09-19 实测,14 核 Mac)

单帧识别成本(954×716 内容区):准确档 OCR ~164ms + 管线(PNG 编码/放大/绿底)~75ms ≈ **240ms/帧**。并发实测:

| 方案 | 每帧聚合 | 结论 |
|---|---|---|
| 准确档 + 4 线程 | 207ms(1.2×) | pyobjc 持 GIL,线程无效 |
| 准确档 + 4 进程(预热) | 159ms(1.6×) | Vision 系统级服务部分串行,非线性 |
| FAST 识别档 | 106ms(2.2×) | 对中文游戏艺术字几乎不产出,**不可用** |
| 准确档降采样 0.75× | 127ms | 结算小字识别失败,**不可用** |
| 关闭语言纠错 | 无收益 | — |

**单台控制机的视觉吞吐上限 ≈ 240/1.6 ≈ 150ms/帧 → 1Hz 全量轮询约 6-7 个客户端**。突破手段(按优先级):

1. **状态自适应轮询**:battle 态(无决策)降到 2s,room/ready/result 等(有决策/闪帧)保持 1s → 实际战斗占比 60%+ 时总吞吐 ≈ ×1.5-2(十场循环的漏结算保护需复测)
2. **每设备一个 runner + 运行目录**:runner 与 viewer 天然按 run 目录隔离,两台/两个 runner 各管一组设备,互不挤占视觉;网页端多 run 目录切换即可总览
3. MCP/bridge/点击侧不是瓶颈:每设备独立 bridge 进程,跨设备光标独立;同 PC 多屏共享光标由设备内串行保证

**跨进程 Vision 串行(2026-11-12 实测)**:同一台 Mac 上两个 runner 各按 ~1Hz 调
Vision 时,ANE/Espresso 的流名额约一分钟后耗尽,两个进程几乎同时抛
`CRImageReaderError.e5rtError("...execution_stream... call failed")` 并暂停。
`_framework/vision.ocr` 现在用机器级 fcntl 锁串行 `performRequests`,失败按
0.5/1/2/4/8 秒阶梯重试(sleep 在锁外,不阻塞其他 runner)。实测 e5rt 故障可持续
数分钟且 CPU-only 请求同样失败(仍会编译 ANE 程序),所以引擎级故障(`e5rt`/
`CRImageReaderError`)触发**熔断**:30s 冷却起步、连败翻倍至 120s 封顶,冷却期内
所有 OCR 立即抛 `VisionUnavailable`,不 perform 也不抢锁;恢复自动复位并记日志。
调用方(窗口发现)把 `VisionUnavailable` 视为降级而非失败:像素带结果保留为
`unconfirmed`,不中止启动(见下)。

### 启动窗口发现的成本控制(2026-11-12)

启动时的全屏 OCR 是每设备秒级开销,也是"启动慢 → 人工改脚本"的来源。三层优化:

1. **窗口清单缓存(v2,布局指纹)**:`window_targets` 把每设备最近一次成功
   **且身份解析通过**的发现结果存到 `~/.cache/rustdesk-mhxy/<device>.windows.json`
   (`RUSTDESK_WINDOW_CACHE` 可改路径,空串=关闭)。下次启动命中需要两个条件:
   当前帧**纯像素**测出的窗口集合与缓存逐边 ±8px 完全一致(布局指纹,无 OCR),
   且每个缓存 rect 上方标题条经 3 变体 recipe 确认 `梦幻西游` 锚点仍在。部分缓存
   因此**不可能命中**——缺的那扇窗的像素带还在帧上,指纹计数对不上;任何 OCR
   异常按 miss 处理,回退全量发现。带 `estimated` 边或 `unconfirmed` 的布局一律
   不落盘。缓存只缩短搜索,从不授权输入,也不替代各 runner 每次启动的对位检查;
   运行时仍按当前帧重新匹配身份。
2. **验证 2x 优先**:顶栏小字号标签(活动/排行/指引)1x 只有 ~40% 命中率,
   2x 接近 100%——验证先跑 2x、空结果才回退 1x,多数窗口省一半验证 OCR 调用。
   标题条确认用 3 变体 recipe(精确条带 1x → +4px 下垫 2x → +16px 下垫 2x,
   早停):单 crop 对 ±6px 矩形抖动只有 ~75% 命中,3 变体实测 130/130。
3. **设备间并行**:`locate_targets` 各设备的截图+发现并发执行(设备独立,
   互不冲突);OCR 受 GIL/系统服务限制不会线性加速,但省掉串行等待。
4. 共享帧:启用 `gateway_port` 的 fleet(见 `mcp/README.md`)把同一解码帧
   广播给该设备的所有 worker,PNG/swizzle 每帧只做一次;OCR 仍各窗独立。
5. **标题条像素带兜底 + 遮挡恢复**:OCR 标题在窗口贴屏幕边缘时会被裁掉(或把
   “梦幻西游”读成低分),纯像素的 `measure_windows` 用长中性色横带找标题栏:
   通知图标只切断它覆盖的几行(同一标题栏被字形行切开的 band 会合并),下方行
   仍是整窗宽;内容底边取"距 start+宽度/兄弟实测宽高比 ±6px 的最后一条整宽
   边缘"(兄弟窗不足 2 个时回退旧规则)。**窄带恢复**:被连接面板截断的标题栏
   只剩 <0.8×中位宽的窄带——用兄弟中位宽度推导底边搜索范围,再垂直内容边缘
   扫描实测右边缘;实测到则整窗 measured,测不到则标 `estimated=['right']`
   (选择/身份解析/缓存三处全部拒绝,报错提示等面板收起重扫)。窄带必须过
   一次标题 OCR 才 admitted(防窗内中性标题栏对话框变成幻影窗);宽度异常的
   band 也不作为其他窗口底边搜索的 cap。发现路径先跑像素带,失败才回退原
   OCR 标题锚点。启动 autocal 复用同一函数:zhuagui 按配置中心最近的同尺寸
   带为每窗取新矩形,像素带失败才回退顶栏锚点/标题栏扫描。
6. **启动有界重试**:`locate_targets` 最多 3 次尝试(间隔 1.5s,总 deadline
   ~90s),每次全新截图(stale 帧只重试、永不入清单);取"已验证窗口数最多"
   的尝试做结果,身份解析失败换帧重试而非立即失败。每次解码帧(含失败尝试)
   都存 `startup/<device>.png`/`attemptN.png`,并写
   `startup/<device>.discovery.json`(尝试次数、band rects、verified/
   unconfirmed/estimated、缓存命中、每候选 OCR 文本与拒绝原因);最终失败抛
   **聚合错误**(每设备:找到的 id、标题锚点数、目标数、证据路径),不再只报
   第一个异常。窗口 id 改行聚类排序(y ±20px 聚簇后行内按 x),1-2px 行抖动
   不再翻转 wN。

## 七、明确不做

- 不做通用视觉 AI/在线识别(保持本地 OCR+规则,unknown 一律禁输入)
- 不做 profile 间共享状态签名(每个任务独立校准,避免串味)
- 不做多任务并行同设备(串行输入是硬约束)
