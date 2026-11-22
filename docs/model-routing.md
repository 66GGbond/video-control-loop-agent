# 当前模型与后续生成模型扩展

## 当前实际接入

| 角色 | 模型 |
| --- | --- |
| 背景工具选择、需求规划、参考图理解、视觉质检、修复决策 | `doubao-seed-2-1-pro-260915` |
| 视频生成、完整视频修复版本生成 | `MiniMax-H3`，当前本地配置 768P |
| 图片生成 | 尚未接入；当前支持上传和理解参考图，不生成参考图 |

服务端使用 `ARK_API_KEY` 和 `ARK_MODEL`，通过 Ark Responses API 发送各角色独立的输入合同。`MINIMAX_*` 配置继续用于视频生成。不要在前端或 Git 中保存密钥。

## 提示词和项目策略

更换的是模型适配层，保留了需求拆解、语义节拍、导演方案、素材 `@` 绑定、参考图事实与推断区分、原子验收、风险取样、帧引用、Rubric、证据不足处理、预算与发布质量门。

- `agent/planner.py`：需求规划与修复决策提示词、结构校验。
- `agent/context.py`：工具白名单与参考图观察提示词。
- `tools/evaluator.py`：视觉评估提示词、同证据格式重试与校准。
- `agent/prompt_compiler.py`：生成与评估合同，保持独立于理解模型。
- `agent/policies.py`、`domain/rubric.py`：预算、状态和发布策略，仍由代码决定。
- `tools/ark_responses.py`：共享传输层，只提取最终 assistant 文本，不把 reasoning 摘要当作业务 JSON；拒绝截断响应。

历史模型输出保留为历史记录，不冒充新模型结果；新的操作使用新模型。

## 视频模型路由边界

当前 `tools/providers.py` 按 `VIDEO_PROVIDER` 选择一个配置好的适配器；`agent/video_capabilities.py` 再校验该适配器能接受的输入模式。**目前没有实现 Agent 在多个视频模型间自主择优。** 保留现有适配器与能力声明，方便后续扩展。

下一步接入多个视频模型时，需要让决策 Agent 从真实可用的候选集合中选择，再让策略代码校验选择：

1. 每个候选声明支持的文本/图片/音频输入、时长、画幅、分辨率与参考素材规则；成本和延迟如未验证应标记未知。
2. 根据需求和不可变约束排除不兼容的模型，再比较可用候选；记录选择理由和未选理由。
3. 将选择绑定到生成合同，再调用对应适配器的 `submit` / `refresh`，保留任务 ID、证据、版本与预算记录。
4. 没有可用候选时明确停止；不得猜测参考能力、静默切换模型或改写已批准的需求。

未来若接入生图模型，应作为独立工具，并把产出的图片加入 Asset Manifest。用户确认或明确绑定之后，才可作为后续视频生成的参考素材；不应把理解模型的文字描述当成已经生成的图片。

API 依据：[Responses API 迁移](https://docs.volcengine.com/docs/ark/responses-api-migration?lang=en)、[结构化输出](https://docs.volcengine.com/docs/ark/responses-api-structured-output?lang=zh)。
