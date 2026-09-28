# RustDesk Control Centre

这是独立于 RustDesk 主程序的网页控制总端原型，未来可以迁移到单独仓库。

它当前实现的是控制面：设备/显示器清单、Skill 目录、模型角色选择、Skill 草稿、任务队列、物理设备输入租约和审计事件。它刻意**不**直接调用 bridge，也不保存设备密码；真正的截图、OCR、状态机和点击仍应在受控主机上的 edge runtime 内运行。

## 快速启动

```sh
python agent/control_plane/server.py
# open http://127.0.0.1:8790
python -m unittest agent/control_plane/test_control_plane.py
```

## 运行边界

- `execute` 只能由 `local-deterministic` edge runtime 承担。
- Codex/Claude 只能配置为 `design` 或 `exception`，用于生成/修订 Skill Bundle 或解释异常；不进入逐帧控制回路。
- 输入租约按 `physical_host_id` 锁定，同一台 PC 的两个 display 可以并行观察，但不能并发发送鼠标输入。
- 网页创建的只是 job；edge 领取 job 后还必须执行首帧校准、Skill 权限检查、操作前复核和证据记录。

## Skill Bundle 目标格式

```text
skill.yaml       元数据、权限、配置 schema
vision.yaml      OCR / 模板 / 颜色 / 本地模型检测器
workflow.yaml    状态机、计数、超时、确认条件
actions.yaml     操作与前后置条件
assets/          参考图和局部模型
fixtures/        回放图与期望结果
tests/           状态机、回放、Mock E2E
```

模板只是创建 Bundle 的便利入口；直接从“画面 + 逻辑 + 操作”生成 Skill 草稿也会落入同一格式，并必须经过离线回放、Mock、人工批准后才能发布。
