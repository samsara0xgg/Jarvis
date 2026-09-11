# Jarvis × Archify

基于当前 Jarvis 工作区代码绘制的中文交互图。HTML 自包含，可直接用浏览器打开。

| 图 | 用途 | 可编辑源文件 |
| --- | --- | --- |
| [运行架构](jarvis.architecture.html) | 六层职责、Runtime 装配、LLM 与本机 Worker | [architecture JSON](jarvis.architecture.json) |
| [任务执行与验证](task-run.sequence.html) | 已存在任务从输入、Worker 执行到验证与输出的时序 | [sequence JSON](task-run.sequence.json) |

图中箭头表示标注的运行交互或规范关系，不是 Python import 图；L1 → L3
表示规范约束，未声称 L3 在运行时直接导入 L1。跨层接口由 Runtime 注入。
时序图合并了轮询读返回和快照组装细节，事件持久化与验证步骤仍显式保留。

## 安装与使用

2026-09-05 从 [tt-a1i/archify](https://github.com/tt-a1i/archify) 的 `archify/`
目录安装至 `~/.agents/skills/archify`，版本为 `2.17.0-dev.1`（上游开发版本）。
`doctor` 已通过；生成器使用 Node.js 18+，本机为 24.14.1，无需安装 npm 依赖。

在 Codex 下一轮即可调用：

> 用 archify 根据 Jarvis 当前代码更新 docs/archify 的架构图和任务时序图，核对源码后重新验证。

修改 JSON 后，在仓库根目录重新生成：

```bash
bash docs/archify/render.sh
```

需要重新运行 Chrome 布局检查和截图时：

```bash
bash docs/archify/render.sh --visual-check
```

使用其他安装位置时设置 `ARCHIFY_SKILL_DIR`。`render.sh` 先验证，再原子交付
每张 HTML，并保存 `*.delivery.json`。浏览器检查会保存 `*.visual-check.json`、
四张截图和截图索引。生成或检查失败会以非零状态退出。

**重绘不会自动分析代码。** Jarvis 行为变化后，应重新阅读相关实现、更新 JSON
和源码快照，再运行生成与浏览器检查。旧检查记录须与新 HTML 的 SHA-256 核对；
重新生成后，旧截图不代表新产物已经完成浏览器验收。

## 源码依据

快照基于 2026-09-05 当前工作区，Git HEAD 为
`8155e10e85338d9c9b14014c2655292a9a8fb577`，当时存在未提交内容。
[source-snapshot.json](source-snapshot.json) 保存所依据文件的 SHA-256，避免把
这份本地快照误认为公共仓库的已提交版本。未启用 Archify 的公共 GitHub 源码跳转。

| 图中内容 | 实现入口 |
| --- | --- |
| 六层依赖边界 | [`.importlinter`](../../.importlinter) |
| L1 规范声明 | [`constitution/__init__.py`](../../jarvis/constitution/__init__.py) |
| 事件与只读投影 | [`event_log.py`](../../jarvis/state/event_log.py)、[`projections.py`](../../jarvis/state/projections.py) |
| Runtime 注入与多触发处理 | [`runtime/__init__.py`](../../jarvis/runtime/__init__.py)：`bootstrap_runtime_app`、`drive_turn` |
| 守护进程、输入观察与响应转发 | [`inherent_loop.py`](../../jarvis/runtime/inherent_loop.py)：`serve_inherent`、`_user_intent_watcher`、`_response_watcher` |
| Packet、策略与动作/输出门控 | [`decision/__init__.py`](../../jarvis/decision/__init__.py)：`decide`、`_dispatch_one_tool_call`、`_finalize_response`；[`gates.py`](../../jarvis/decision/gates.py) |
| Worker 启动与本机验证 | [`execution/tools.py`](../../jarvis/execution/tools.py)：`spawn_worker_handler`、`verify_diff_handler`；[`codex_action.py`](../../jarvis/execution/codex_action.py) |
| reported / observed / verified 区分 | [`result_interpreter.py`](../../jarvis/decision/result_interpreter.py)：`interpret_verify_diff_bundle`；[`reviewer.py`](../../jarvis/decision/reviewer.py) |
| 部署路径、驻留与电源观察 | [`deployment/`](../../jarvis/deployment/)、[`daemon.py`](../../jarvis/runtime/daemon.py) |

完整合同仍以 [spec §3](../spec.html#layer-state-object) 与 [ADR](../adr/) 为准。
这两张图描述当前代码的选定路径，不代表 Jarvis 全部设计目标已实现或已完成运行验收。

## 交付证据

每张图的 `*.delivery.json` 保存源 JSON 和 HTML 的 SHA-256、字节数及 9 项
showcase 检查结果。`*.visual-check.json` 单独记录真实 Chrome 的布局证据，
图像审阅结论另存于 [acceptance.json](acceptance.json)。这些检查验证图本身，
没有触发 Jarvis 的 LLM、Worker 或真实任务。
