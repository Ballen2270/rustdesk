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

## 五、Console / Mock / 测试的通用化

- **Console**:`state.json` 增加 `states_meta`(profile 写入:{state: {label, color, actionable}}),前端颜色/徽章/泳道全走数据,不再内置 jianhui 状态;其余已通用
- **Mock**:通用 mock 读剧本文件 `{window: [(state, image, 持续秒), ...]} + 点击迁移表`,新 skill 的离线端到端 = 一份 JSON
- **测试**:框架提供表驱动测试台(`feed(['battle','result','result']) → 期望动作/计数`),实例只需写场景行;replay 模板化(期望表即测试)

## 六、迁移路径(不破坏现有剑会)

- **Phase A**:抽 `_framework/{vision,session,artifacts}.py` + console,jianhui 改 import,行为零变化(单测/回放/mock e2e 全绿为准)
- **Phase B**:抽 runner+policy,jianhui 改写为 profile.py + 薄入口;现有 24 项单测迁移为表驱动,语义必须逐一复刻
- **Phase C**:写第一个新 skill(真实消费者,反推框架缺口)
- **Phase D**:观察台多 run 切换/历史列表、profile 热校准工作流(inspect 辅助测量槽位)

## 七、明确不做

- 不做通用视觉 AI/在线识别(保持本地 OCR+规则,unknown 一律禁输入)
- 不做 profile 间共享状态签名(每个任务独立校准,避免串味)
- 不做多任务并行同设备(串行输入是硬约束)
