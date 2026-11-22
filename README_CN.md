# AI 视频生成质检与修复 Agent

[English](README.md) | 中文

> 面向 AI 视频生成的质量控制 Agent：将用户 Brief 转化为可验证的控制计划，并基于视频证据自主决定修复、发布或请求人工授权。

## 项目解决的问题

用户通常只需给出一句创作意图与少量可选素材；难点在于把模糊 Brief 稳定交付为符合要求的视频版本。这个过程需要完成创意展开、镜头规划、多模态约束管理、视频生成、证据取证、质量判断与定向迭代。人工串联这条链路成本高、难以持续追踪，也无法在每次生成后稳定判断下一步应继续、修复、发布还是请求授权。

本项目让受预算、证据与能力边界约束的 Agent 代替用户执行这一闭环；用户只在补充关键上下文、调整约束或授权额外迭代时介入。

本项目建立闭环：

`用户 Brief → 控制计划 → 视频生成 → 风险感知取证 → Quality Rubric → Bad Case → 修复 / 发布`

## 核心产品能力

| 产品能力 | 实现方式 |
|---|---|
| 视频生成可控性 | 将 Brief 拆解为场景所需的 1–6 个语义节拍、不可变约束、风险预测与原子验收标准；先定位问题节拍，再带着已通过约束重新生成完整视频版本。 |
| 多模态素材治理 | 以 Asset Manifest 保存图片/音频/视频参考的输入顺序与 `@` 关联；只有被明确关联的参考图才能成为参考一致性验收的证据。 |
| Brief 编译 | 用户只需表达创作意图；Agent 在不虚构用户事实的边界内补全镜头语言、画面处理、动作连续性与防错约束，并将其写入可审计的生成合同。 |
| Prompt Engineering / 任务拆解 | `Prompt Compiler` 从同一控制计划生成可追溯的生成合同与评估合同，避免生成目标与评估标准脱节。 |
| Quality Rubric / Bad Case 治理 | 关键标准必须有帧级证据；综合分仅用于诊断，不能覆盖关键失败或 Blocking Bad Case。 |
| Agent 自主规划与 Tool Use | Context Agent 按需调用日历、参考图观察、能力路由等白名单工具；能力路由根据模型声明选择合法路径。 |
| 人机协作 | 用户设置/调整修复预算；Agent 可在预算内自主运行，遇到质量门、证据缺失、预算耗尽或用户暂停时停止。 |
| 可审计迭代 | 每个版本绑定生成合同、评估合同、证据、修复决策和被拒绝方案；历史版本评估不可被后续版本覆盖。 |

## 为什么是 Agent，而非普通 Workflow

| 普通 Workflow | 本项目中的 Agent |
|---|---|
| 固定顺序调用模型 | 根据 Brief、参考素材和已声明能力选择生成路径 |
| 固定抽帧或单一总分 | 根据镜头计划和风险调整取证帧位置 |
| 失败后整条视频重试 | 根据 Bad Case 选择最小、可解释的修改集与整片重生成优先级 |
| 到达次数即结束 | 在发布门、预算、证据与人工授权边界下自主停止或继续 |

LLM 负责规划与解释；策略代码负责证据有效性、质量门、预算和状态流转。

## Agent 运行流程

```mermaid
flowchart LR
  Brief["Brief + 多模态参考"] --> Context["Context Enrichment Agent"]
  Context --> Compiler["Prompt Compiler\n生成合同 + 评估合同"]
  Compiler --> Route["Capability Router"]
  Route --> Generate["视频生成工具"]
  Generate --> Sample["风险感知视觉取证"]
  Sample --> Evaluate["质量评估 + Quality Rubric"]
  Evaluate --> Decide["修复 / 发布 / 人工授权"]
  Decide -->|修复| Compiler
```

1. **Context Enrichment**：仅在会影响可控性时补充信息；例如应季内容缺少市场、或需要观察参考图时。
2. **Prompt Compiler**：生成与评估使用同一份已确认控制计划，并写入版本追踪 ID。
3. **Capability Router**：只有端点被明确声明支持参考图时，才发送参考图像素；不猜测模型能力。
4. **Asset Manifest**：将素材上传顺序、角色与 `@` 关联同时写入生成和评估合同；仅上传但未关联的参考图不能被用作一致性验收证据。
5. **Quality Agent**：按风险抽帧、评估每条原子标准，生成可引用的 Bad Case 证据；音频质量门由已确认的音频需求条件触发。
6. **Repair Planner**：在用户预算内选择修改集、拒绝不必要方案，并记录理由。当前视频模型执行的是完整视频新版本生成，不会虚构“局部镜头编辑”能力。

## 质量门

一个视频版本可发布，必须同时满足：

- 所有关键验收标准均有有效的帧级证据且通过；
- 当前激活的视觉 Rubric 维度达到 `3.0` 基线；
- 若 Brief 明确触发音频要求，相关音频原子标准也必须具备有效的时间级证据并通过；
- 不存在 Blocking 级 Bad Case。

证据不足时，系统返回 `insufficient_evidence`，不会伪造通过结果或自动创建没有依据的修复任务。

## 模型与工程边界

Agent 主链路与视频模型适配器分离。规划、Quality Rubric、版本记忆、预算策略和修复逻辑不依赖某个模型 ID；更换视频模型时，新增并验证对应的能力声明即可。

当前视频候选包括 Seedance 2.5、Seedance 2.0 和 MiniMax H3。Agent 按已声明的能力筛选并推荐模型，用户确认后提交生成；旧测试适配器不进入自动候选列表。

## 本地运行

```bash
python3 run_demo.py
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。可选 API 配置见 [.env.example](.env.example)，不要提交 `.env` 或 API Key。

启用 MiniMax H3 时，在 `.env` 中填写：

```dotenv
VIDEO_PROVIDER=auto
MINIMAX_API_KEY=你的 MiniMax API Key
MINIMAX_VIDEO_MODEL=MiniMax-H3
MINIMAX_VIDEO_RESOLUTION=768P
```

H3 V2 为异步任务；本项目会提交任务、轮询状态、下载生成视频，再进入原有的取证与质量闭环。该适配器将控制计划的 4–15 秒时长、比例与清晰度提交给 H3；若计划超出支持范围，系统会在生成前明确提示，而不会静默修改已批准的时长。

## Demo 与设计文档

- [5 分钟产品演示脚本](docs/interview-demo-script.md)
- [架构图与状态流](docs/architecture.md)
- [多模态质量 Rubric v1.2](docs/video_quality_rubric_v1_2.md)
- [Agent 决策策略](docs/agent-decision-policy.md)
- [真实视频集中验收计划](docs/real-video-acceptance-plan.md)

## 当前限制与下一步

- 质量 Rubric v1.2 已定义音频、ASR、音画同步和口型对齐的条件化质量门；当前运行时已执行本地音轨存在性与时长探测，ASR、音画同步和口型对齐仍待接入。
- 每个视频模型端点都需通过能力验证，才能开放参考图、视频或音频输入。
- 下一步用真实视频完成三类验证：品牌产品完整性、角色连续性、动作/叙事完成度。

## 本地面试演示

运行 `python3 run_demo.py` 后打开 http://127.0.0.1:8000。无需体验码，可直接使用已配置的模型；右上角可切换中文 / English。项目状态保存在本地，原有修复次数设置和模型单条时长限制仍然适用。

## 当前模型

Doubao Seed 2.1 Pro（`doubao-seed-2-1-pro-260915`）负责需求理解、规划、视觉质检与修复决策；Seedance 2.5、Seedance 2.0 和 MiniMax H3 负责视频生成。画布生图已接入 Seedream 5.0 Pro 与 Flash，先推荐、再由用户确认生成。 [模型职责、原有策略与后续路由扩展](docs/model-routing.md)。

## 视频模型推荐

Seedance 2.5、Seedance 2.0 与 MiniMax H3 已接入模型卡片与运行时选型 Skill。`VIDEO_PROVIDER=auto` 时，Doubao 推荐兼容候选并默认选中，用户在紧凑表格中调整生成参数，点击生成时使用当前设置。H3 仅在明确低预算目标下参与推荐。脚本后由 Agent 判断是否需要重要人物四视图，生成后作为视频参考；参考视频链接入口已移除。详见 [模型路由与能力边界](docs/model-routing.md)。

## 画布工作区

[本地画布与全局创作 Agent](docs/canvas.md)：节点编辑、参考连线、自动保存，并复用现有生成和质检流程。
