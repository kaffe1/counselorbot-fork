# A 使用场景的前端 payload 与后端 prompt 组装全解

> 本文专门分析 [prompt-envelope-report.md](./prompt-envelope-report.md) 中的 **A 使用场景：引导式咨询对话**。前端入口是 [GuidedChatInterface.tsx](../frontend/src/components/qsa/GuidedChatInterface.tsx)，主要请求是 `POST /api/chat/stream` → 后端 `POST /chat/stream`。
>
> 文件名按需求保留为 `playload-analyse.md`（`playload` 是需求中的拼法）。

## 1. 先说结论：payload 不是最终 prompt

前端不会把完整的 `system / history / user` 三件套直接发给模型。它发送的是一份“组装参数”：

```text
HTTP JSON payload
  = 本轮内容（message / memory_message / scores_context）
  + 路由参数（questionnaire_type / phase / mode / use_phase_prompt）
  + 会话键（session_id / conversation_id）
  + 用户选项（language / response_length / response_format / reasoning_effort ...）
  + 专用场景参数（IDEA / 教师班级 / 笔记本 / 预览）
```

后端使用这些字段重新查询可信的步骤、提示词、咨询师人设、会话记忆、学生档案和知识来源，最后才生成：

```text
SYSTEM = 当前步骤任务 + 人设 + 模式级系统规则 + 学生/路径/档案/知识 + 本轮合同
HISTORY = 服务端会话记忆中的近期 user/assistant 原文
USER = 本步分数、学生原话，或无输入时的占位句
```

因此，`mode` 不是 prompt，`phase` 也不是 prompt；它们是后端选 prompt 和决定组装分支的键。

---

## 2. 前端共有哪些 payload 字段

### 2.1 字段总表

`GuidedChatInterface` 的首次请求最多会使用 23 个字段；如果 SSE 断流，续写层会再加第 24 个 `partial_response`。下表同时列出常驻字段和条件字段。

| 类别 | 字段 | JSON 类型 | 是否每次都发 | 直接影响 prompt 文本吗 |
|---|---|---:|---|---|
| 本轮内容 | `message` | string | 是 | 是，但可能先被清洗或替换 |
| 本轮内容 | `memory_message` | string / null / 省略 | 否 | 是，它可以决定模型真正看到的学生原话 |
| 本轮内容 | `internal_message` | boolean | 否 | 间接，影响意图、日志和记忆 |
| 路由 | `mode` | string | 是 | 是，选系统 prompt key 和分支 |
| 路由 | `phase` | string / 省略 | 否 | 是，选步骤、特殊 prompt 和当前范围 |
| 路由 | `use_phase_prompt` | boolean | 否 | 是，决定是“进入步骤”还是“步骤内追问” |
| 本轮内容 | `scores_context` | string | 否 | 是，但后端会过滤和按步骤缩小范围 |
| 会话 | `session_id` | string | 是 | 间接，用来取 history、持久化分数和状态 |
| 会话 | `conversation_id` | string / 省略 | 首轮可省略 | 间接，用来串联一段对话和去重建议 |
| 路由 | `questionnaire_type` | string | 是 | 是，但真实 DB 步骤的类型优先 |
| 表达 | `language` | string | 是 | 是，进入 `[TURN CONTRACT]` 并影响本地化 |
| 输出 | `max_tokens` | integer | 是 | 不写入 prompt，限制输出 token |
| 输出 | `response_length` | enum | 是 | 是，加长度指令，并覆盖 token 档位 |
| 输出 | `response_format` | enum | 是 | 是，在 SYSTEM 尾部加可见格式指令 |
| 路径 | `guided_path` | enum | 是 | 是，决定完整/精简路径 |
| 模型 | `reasoning_effort` | enum | 是 | 不直接写入 prompt，改变推理开关/预算 |
| 咨询师 | `counselor_id` | integer / null | 是 | 是，可选人设；也可选 provider/model preset |
| IDEA | `idea_variant` | enum / 省略 | 仅 IDEA | 是，加 IDEA 变体指令 |
| IDEA | `idea_budget` | integer / 省略 | 仅 IDEA | 是，进入 IDEA map 的节奏上下文 |
| 教师 | `group_ids` | integer[] / 省略 | 仅教学目标 | 是，构建已授权的班级上下文 |
| 笔记本 | `notebook_context` | enum / 省略 | 教师选择后 | 是，决定 `[PROFILE]` 放谁的资料 |
| 笔记本 | `practice_notebook_id` | integer / 省略 | 仅模拟笔记本 | 是，选择模拟学生 |
| 管理员 | `preview` | boolean | 预览时 | 不直接改 prompt；改变权限和持久化 |
| 断流续写 | `partial_response` | string / 省略 | 只在断流重试时 | 是，后端把已显示文本包成续写指令 |

后端的入参定义见 [api_models.py:16](../backend/api_models.py#L16)；三种主要前端构造点分别在 [GuidedChatInterface.tsx:973](../frontend/src/components/qsa/GuidedChatInterface.tsx#L973)、[GuidedChatInterface.tsx:1039](../frontend/src/components/qsa/GuidedChatInterface.tsx#L1039) 和 [GuidedChatInterface.tsx:1268](../frontend/src/components/qsa/GuidedChatInterface.tsx#L1268)。

### 2.2 `undefined`、空字符串、`null` 和 `false` 不是一回事

前端最后用 `JSON.stringify(payload)`。因此：

- 值为 `undefined` 的属性不会出现在 JSON 中，后端使用 Pydantic 默认值。
- `message: ""` 是显式发送的空字符串，常用于“请后端从 DB 读取步骤 prompt”。
- `counselor_id: null` 是显式表示未选咨询师，不是数字 `0`。
- `use_phase_prompt` 省略时后端默认为 `false`；`internal_message` 和 `preview` 也一样。
- `memory_message` 省略与 `memory_message: ""` 有区别：后者明确表示不要把技术指令当作学生原话写入记忆。

---

## 3. 字段 one by one：含义、全部取值与后端效果

### 3.1 `message`

**类型与可能值**：任意字符串，在本组件中有四种实际形态。

1. **进入普通步骤**：`""`。
2. **学生追问**：学生输入；对访谈型路径，前端可把它包成 `CURRENT STEP INTERNAL INSTRUCTIONS ... STUDENT ANSWER: ...`。
3. **固定 questions 阶段**：前端写死的英文内部指令，要求恰好提三个开放式问题。
4. **精简路径进入步骤**：前端原始值仍是 `""`，但后端会改写成 `Present the current essential-path step using the conversation evidence.`。

**后端作用**：

- `use_phase_prompt=true` 且 `message` 为空：后端根据 `phase` 从 `guided_steps.prompt` 读步骤指令。
- `use_phase_prompt=false`：后端先提取干净的学生话语，再把它放入当前 USER 消息。
- 该文本还参与意图识别、RAG 检索 query、推荐上下文和 IDEA map 更新。

解析逻辑见 [_resolve_user_message_for_chat](../backend/chat_logic.py#L2013)。

### 3.2 `memory_message`

**可能值**：

- 学生追问：学生未包装的原话。
- questions 内部回合：`""`。
- 精简路径步骤进入：`""`。
- 完整路径普通步骤进入：省略。
- API schema 还允许 `null`；当前组件通常使用省略而非显式 `null`。

**后端作用**：它是“学生真正说的话”的优先来源。只要它是非空字符串，后端就忽略 `message` 外层的技术包装，用它生成 USER 消息和会话记忆。见 [extract_clean_student_message](../backend/chat_logic.py#L1983)。

### 3.3 `internal_message`

**可能值**：`true` / `false`（省略等于 `false`）。

**前端何时为 `true`**：

- 固定 `questions` 阶段。
- 精简路径的步骤进入；后端也会在精简路径改写时强制它为 `true`。

**后端作用**：

- 不把这条技术指令记为学生发言。
- 日志/PDF 中不把它当作学生输入。
- 不把它识别为学生主动索要建议的 follow-up；skills 意图按 `guided` 处理。

### 3.4 `mode`

**类型**：后端 schema 是开放字符串，默认 `generic`。前端从 DB 步骤的 `system_prompt_mode` 取值，因此管理员创建的自定义 mode 在技术上也可能出现。

**内置 A 场景的全部 mode**：

| 工具 | 进入步骤的 mode | 追问时的 mode |
|---|---|---|
| QSA | `intro`, `factor`, `second-level` | `factor-qa`（当前步骤是 `factor`/`second-level`），其他为步骤 mode |
| QSAr | `intro`, `qsar-factor`, `qsar-second-level` | `qsar-factor-qa` 或步骤 mode；非分析阶段可为 `qsar-generic` |
| ZTPI | `intro`, `ztpi-factor`, `ztpi-btp` | 保持当前步骤 mode |
| QPCS | `qpcs-factor`, `qpcs-analysis`, `qpcs-summary` | 保持当前步骤 mode |
| QPCC | `qpcc-factor`, `qpcc-interview`, `qpcc-summary` | 保持当前步骤 mode |
| QAP | `qap-factor`, `qap-interview`, `qap-summary` | 保持当前步骤 mode |
| SAVICKAS | `intro`, `savickas-interview`, `savickas-summary` | `savickas-interview` 或当前步骤 mode |
| EVENTO ×2 | `intro`, `evento-interview`, `evento-summary` | 保持当前步骤 mode |
| OBIETTIVO ×2 | `intro`, `obiettivo-interview`, `obiettivo-summary` | 保持当前步骤 mode |
| IDEA | `idea-focus` | `idea-focus` |
| 固定 questions | `generic` | — |
| 精简路径 | QSA 虚拟步骤为 `generic`；两种 OBIETTIVO 为 `obiettivo-interview` | 同左 |

追问转换见 [GuidedChatInterface.tsx:1170](../frontend/src/components/qsa/GuidedChatInterface.tsx#L1170) 和 [chat_logic.py:1918](../backend/chat_logic.py#L1918)；mode 到 prompt key 的内置映射见 [prompt_config.py:1183](../backend/prompt_config.py#L1183)。

**后端作用**：

- 选择 mode 级系统 prompt key。
- 判断是否是 QSA/QSAr 对话型追问。
- 判断是否包含分数分析指令、是否允许新建议、是否做 ZTPI 清洗。
- `mode` 不可以单独决定 prompt：特殊 `phase` 和 DB 步骤都可以覆盖它。

### 3.5 `phase`

**类型**：可选字符串。它是步骤 ID，不是显示名。完整路径的步骤从 DB `guided_steps` 动态读取，所以运行时最终集合可被管理员扩展。

**内置默认 phase 全集**：

- **QSA**：`intro`, `cognitive`, `affective`, `sl-elaboration`, `sl-selfcontrol`, `sl-motivation`, `sl-emotions`, `sl-attribution`, `sl-social`, `sl-synthesis`。
- **QSAr**：`qsar-intro`, `qsar-cognitive`, `qsar-affective`, `qsar-processing`, `qsar-selfcontrol`, `qsar-motivation`, `qsar-emotions`, `qsar-attributions`, `qsar-synthesis`。
- **ZTPI**：`ztpi-intro`, `ztpi-t1`, `ztpi-t2`, `ztpi-t3`, `ztpi-t4`, `ztpi-t5`, `ztpi-btp`。
- **SAVICKAS**：`savickas-intro`, `savickas-patto`, `savickas-q1`, `savickas-q2`, `savickas-q3`, `savickas-q4`, `savickas-q5`, `savickas-final`。
- **EVENTO_STUDIO**：`evstudio-intro`, `evstudio-patto`, `evstudio-evento`, `evstudio-fatto`, `evstudio-funzionato`, `evstudio-criticita`, `evstudio-rilettura`, `evstudio-prossima`, `evstudio-final`。
- **EVENTO_PROFESSIONALE**：与上面同样的后缀，前缀改为 `evprof-`。
- **OBIETTIVO_STUDIO**：`obbstudio-intro`, `obbstudio-patto`, `obbstudio-partenza`, `obbstudio-livello`, `obbstudio-smart`, `obbstudio-sfida`, `obbstudio-piano`, `obbstudio-verifica`, `obbstudio-final`。
- **OBIETTIVO_DOCENZA**：与上面同样的后缀，前缀改为 `obbdocenza-`。
- **IDEA**：`idea-intro`, `idea-statement`, `idea-assumptions`, `idea-evidence`, `idea-alternatives`, `idea-implications`, `idea-question`, `idea-synthesis`。
- **QPCS**：`qpcs-intro`, `qpcs-profilo`, `qpcs-emozioni`, `qpcs-comunicazione`, `qpcs-volizione`, `qpcs-apprendimento`, `qpcs-fiducia`, `qpcs-sintesi`。
- **QPCC**：`qpcc-intro`, `qpcc-profilo`, `qpcc-comunicazione`, `qpcc-controllo`, `qpcc-volizione`, `qpcc-elaborazione`, `qpcc-convinzioni`, `qpcc-sintesi`。
- **QAP**：`qap-intro`, `qap-profilo`, `qap-preoccupazione`, `qap-controllo`, `qap-curiosita`, `qap-fiducia`, `qap-sintesi`。
- **固定 AI 阶段**：`questions`。`conclusion` 是前端本地结束页，不会再发起 chat 请求。
- **QSA 精简路径**：`qsa-essential-focus`, `qsa-essential-experience`, `qsa-essential-action`, `qsa-essential-summary`, 总结后自愿追问为 `qsa-essential-followup`。
- **OBIETTIVO_STUDIO 精简路径**：`obbstudio-essential-focus`, `obbstudio-essential-smart`, `obbstudio-essential-plan`, `obbstudio-essential-summary`。
- **OBIETTIVO_DOCENZA 精简路径**：`obbdocenza-essential-focus`, `obbdocenza-essential-smart`, `obbdocenza-essential-plan`, `obbdocenza-essential-summary`。

默认步骤定义见 [prompt_config.py:1594](../backend/prompt_config.py#L1594)；精简路径的封闭集合见 [qsa_essential.py:12](../backend/qsa_essential.py#L12)。

**后端作用**：

- 查找 `GuidedStep`，获得可信的 `questionnaire_type`、`prompt`、`label`、`sort_order` 和 `system_prompt_mode`。
- 对 `intro`/`questions` 等特殊 ID 优先选用专属系统 prompt。
- 限定当前分数因子，读取步骤组件开关，构建 `[GUIDED PATH]`。
- 判断是否是综合步骤，从而选 `[JOURNEY EVIDENCE]` 而不是 `[SESSION LEDGER]`。

### 3.6 `use_phase_prompt`

**可能值**：`true` / `false`（省略等于 `false`）。

- `true`：“进入步骤”。后端用 `phase` 取 DB 步骤 prompt，把它放在 SYSTEM 的指令前缀；USER 通常只有当步分数或占位句。
- `false`：“学生追问/继续”。后端不把步骤 prompt 当作本轮 USER 命令，而是把它包装为 `[CURRENT STEP MANDATE - CONTINUATION]` 放入 SYSTEM。
- 精简路径是例外：前端进入虚拟步骤时发 `true`，后端验证路径后立即改写为 `false + internal_message=true + 固定内部 message`，再追加精简路径专属指令。

### 3.7 `scores_context`

**类型与可能值**：字符串，不是 JSON 分数对象。

前端格式化规则在 [GuidedChatInterface.tsx:279](../frontend/src/components/qsa/GuidedChatInterface.tsx#L279)：

- QSA/QSAr/QPCS/QPCC/QAP 等有因子的问卷：`PROFILO <TYPE> DELLO STUDENTE:\n- <code> (<localized name>): <value>/9 ...`。
- ZTPI：`PROFILO TEMPORALE DELLO STUDENTE:\nT1 (...): <value>/9 ...`。
- SAVICKAS：固定文字，说明这是无数值的叙事访谈。
- IDEA：空字符串；后端也会再次强制清空，防止服务文字被画成 Idea 节点。
- 无数值的其他路径：固定的“定性、无数字分数”描述。
- `scoresContextOverride`存在时：可以是跨问卷组合的任意上下文字符串。

**何时发送**：

- 进入步骤、questions 内部回合：发送。
- 普通学生追问：只有存在 `scoresContextOverride` 或处于精简路径时重发；否则省略。

**后端作用**：

1. QSA/QSAr 因子代码被标注和清洗。
2. 根据当前步骤组件开关过滤认知/情感/其他分数。
3. 根据 `phase` 的因子代码再缩小到本步范围。
4. 生成 `[CURRENT STEP SCORE PROFILE]`、成长区和建议闸门。
5. 进入步骤时一般放入 USER；追问不重发时，后端从 `session_id` 持久化记忆取回，放入 SYSTEM 的 `[PROFILE]` 作参考。
6. 同时参与 RAG/skills 检索 query。

### 3.8 `session_id`

**可能值**：非空会话字符串，通常是 UUID 或前端生成的稳定 ID。后端 schema 允许省略；若省略则后端生成 UUID，但 `GuidedChatInterface` 正常每次都会发。

**后端作用**：

- 读写最多 12 条、约 6000 字符的 verbatim transcript。
- 保存/取回分数、已完成步骤、目标、偏好和外部备注。
- 索引 IDEA map/reference/sources、session ledger、thread guard、推荐和日志。
- 进入问卷第一步时可清空旧会话记忆。

记忆限制见 [memory_service.py:30](../backend/memory_service.py#L30)。

### 3.9 `conversation_id`

**可能值**：首轮省略；后端返回后，之后是稳定字符串（通常 UUID）。

**后端作用**：

- 把同一段对话的日志聚合在一起。
- 构建整段旅程证据。
- 查找本对话已经提供过的认证策略，避免重复发放。

### 3.10 `questionnaire_type`

**A 场景的内置取值**：

`QSA`, `QSAr`, `ZTPI`, `QPCS`, `QPCC`, `QAP`, `SAVICKAS`, `EVENTO_STUDIO`, `EVENTO_PROFESSIONALE`, `OBIETTIVO_STUDIO`, `OBIETTIVO_DOCENZA`, `IDEA`。

后端 schema 技术上接受任意字符串/空值，以支持数据库中的动态工具。

**信任边界**：如果 `phase` 能查到真实 `GuidedStep`，后端用 `step.questionnaire_type` 覆盖前端声称的值；权限检查也以服务端步骤所属工具为准。这防止通过伪造 `questionnaire_type` 跨越班级/工具权限。

### 3.11 `language`

**前端可能值**：`it`, `en`, `es`, `fr`, `de`, `sv`。

前端会把 `en-US`/`sv_SE` 这类 locale 缩成主语言；不支持的值回退到 `it`。后端再做一次同类归一化，见 [routes/chat.py:112](../backend/routes/chat.py#L112)。

**后端作用**：

- 加入全局语言指令和 `[TURN CONTRACT]`。
- 选择因子名、分数解读、推荐条目、路径标签和某些上下文的本地化版本。
- 它还被放入 HTTP `X-CounselorBot-Language` header，但 prompt 组装使用的是 JSON body 字段。

### 3.12 `max_tokens`

**前端本组件的实际值**：

- questions 回合：`500`。
- 进入/重复步骤：`700`。
- 学生追问：`900`。

**重要覆盖规则**：只要同时有 `response_length`，后端便忽略上述 500/700/900，改用：

- `short` → 256 tokens；
- `medium` → 800 tokens；
- `long` → 2000 tokens。

只有 `response_length` 缺失时才使用 `max_tokens`，并夹在 128–8192 之间。IDEA 还会另加 1200 tokens，留给不对学生显示的 map JSON。见 [chat_logic.py:245](../backend/chat_logic.py#L245) 和 [chat_preparation.py:190](../backend/chat_preparation.py#L190)。

### 3.13 `response_length`

**全部取值**：`short`, `medium`, `long`。默认 `medium`。

| 值 | SYSTEM 中的可见字数上限 | 输出 token 上限 |
|---|---:|---:|
| `short` | 80 words | 256 |
| `medium` | 260 words | 800 |
| `long` | 600 words | 2000 |

它同时产生 `[RESPONSE LENGTH - BINDING]` 指令，并在模型返回后再做可见字数硬截断，因此不只是 UI 偏好。断流续写时，已显示的 `partial_response` 不再重复套用可见字数截断。

### 3.14 `response_format`

**全部取值**：

- `standard`：短的对话式段落，克制加粗。
- `bullets`：Markdown 项目符号，突出关键点和优先级。
- `table`：对比/总结时使用最多三列的紧凑 Markdown 表格；个人问题仍用短文本。

后端在完整 SYSTEM 接近尾部添加 `[VISIBLE RESPONSE FORMAT]`，并明确格式选择不得改变意义、语言、长度预算或私有 JSON/protocol block。见 [chat_preferences.py:7](../backend/chat_preferences.py#L7)。

### 3.15 `guided_path`

**全部取值**：`complete`, `essential`。默认 `complete`。

- `complete`：使用 DB 中的完整步骤序列。
- `essential`：只允许 `QSA`, `OBIETTIVO_STUDIO`, `OBIETTIVO_DOCENZA`，且 `phase` 必须属于各自封闭集合；非法组合直接 422。后端会追加 `[QSA ESSENTIAL PATH - CURRENT TURN]` 或 `[ESSENTIAL PATH - CURRENT TURN]`。

### 3.16 `reasoning_effort`

**全部取值**：`off`, `standard`, `deep`。默认 `standard`。

- `off`：禁用 thinking/reasoning。
- `standard`：不覆盖咨询师 preset 或模型家族的默认值。
- `deep`：开启推理，覆盖预算为 8000 reasoning tokens。

这个字段不生成学生可见 prompt 块，而是设置 AI provider 的调用参数。介绍/合作约定等步骤可由服务端强制 `off`，该步骤规则最后执行，优先于学生的 `deep`。见 [routes/chat.py:207](../backend/routes/chat.py#L207) 和 [reasoning_profiles.py:125](../backend/reasoning_profiles.py#L125)。

### 3.17 `counselor_id`

**可能值**：`null` 或当前用户可见/可用的正整数咨询师 ID。

**后端作用**：

1. 做班级和私人咨询师权限校验。
2. 读取 counselor `persona` 和 `name`；persona 以 `[PERSONA]` 块加入 SYSTEM，name 替换 `{{counselor_name}}`。
3. 若 counselor 绑定 model preset，选择 provider/model、thinking 开关和 reasoning budget。
4. 若是用户私人 counselor，可选中私人 API/ChatGPT 连接。

`null` 表示没有专属人设/preset，使用全局配置和中性名称 fallback。

### 3.18 `idea_variant`

**全部取值**：`student-path`, `student-open`, `research`, `concept`。IDEA 默认 `student-path`；非 IDEA 时省略。

后端根据它把 IDEA 变体专属指令加到基础 system prompt，影响如何聚焦观点、研究问题或概念。它不改变固定 `mode=idea-focus`。

### 3.19 `idea_budget`

**前端 UI 的全部取值**：`8`, `16`, `30`, `0`；默认 `16`。

- `8` / `16` / `30`：期望的对话交换次数。
- `0`：无限，继续到需要时再停。
- API schema 还允许 `null`/省略，后端同样视为无明确上限。

它不是 token budget。后端把预算、已用回合数和节奏节点写入 `[IDEA MAP]` 上下文，影响下一步是继续深挖还是收束。取值定义见 [idea-map.ts:80](../frontend/src/lib/idea-map.ts#L80)。

### 3.20 `group_ids`

**可能值**：省略，或一个非空的正整数班级 ID 数组。只有 `OBIETTIVO_DOCENZA` 且教师已选班级时发送。

**后端作用**：

- 每一轮重新校验教师是否管理/共享这些班级。
- 只有在教师笔记本模式下，它们才被格式化进 `[PROFILE]` 中的教师班级上下文。
- 其他工具忽略该字段。

### 3.21 `notebook_context`

**全部取值**：`student`, `teacher`, `practice`, `none`；或省略。前端内部还有 `default`，但 `default` **永远不发给后端**。

- `student`：学生笔记本 + Portfolio + 目标 + 已允许的班级背景。
- `teacher`：教师笔记本；在教学目标对话中可再加 `group_ids` 班级资料。
- `practice`：教师的模拟学生笔记本，并加 `[SIMULATION]`。
- `none`：主动清空笔记本/档案上下文。
- 省略：后端按工具默认；教师向工具默认 teacher，其他通常默认 student。

只有服务端识别为教师/研究者/管理员的人才能覆盖默认值；普通学生伪造字段也不会生效。前端过滤见 [notebook-context.ts:43](../frontend/src/lib/notebook-context.ts#L43)，后端处理见 [chat_logic.py:2905](../backend/chat_logic.py#L2905)。

### 3.22 `practice_notebook_id`

**可能值**：省略，或当前教师所有的活跃模拟学生笔记本正整数 ID。只有 `notebook_context=practice` 时有意义。

后端会验证角色、所有权和是否归档。验证失败时不会回退到真实学生资料，而是按 `none` 清空 profile，避免资料越权泄露。

### 3.23 `preview`

**可能值**：`true` / `false`（前端非预览时直接省略）。

- `true` 只允许管理员，用于后台 sandbox。
- 这个字段本身不会加入 prompt，但会使回合不持久化日志、统计、共享回答候选和推荐。
- 预览还允许在管理员 sandbox 中测试工具，但不等于可绕过 counselor 可见性。

### 3.24 `partial_response`（断流时自动增加）

**可能值**：省略，或已经通过 SSE 显示给用户的非空部分回答；后端最大 60000 字符。

初始请求不带它。如果流在 `done` 之前中断，[useChatContinuation](../frontend/src/components/ui/ChatContinuation.tsx#L10) 会：

1. 保留原 payload。
2. 加入 `partial_response`。
3. 用后端已返回的 `session_id`/`conversation_id` 继续。
4. 自动重试最多 3 次，之后等用户点“继续”。

后端用 [chat_continuation.py](../backend/chat_continuation.py) 把原 `full_message` 与“从已有文本结尾继续”的指令组合，防止模型从头重写。

---

## 4. 三类实际请求的 payload 形状

### 4.1 进入一个完整路径步骤

```jsonc
{
  "message": "",
  "mode": "<step.system_prompt_mode>",
  "phase": "<step.id>",
  "use_phase_prompt": true,
  "scores_context": "<格式化的全部分数/定性说明>",
  "session_id": "<session id>",
  "conversation_id": "<首轮可省略>",
  "questionnaire_type": "<instrument>",
  "language": "it|en|es|fr|de|sv",
  "max_tokens": 700,
  "response_length": "short|medium|long",
  "response_format": "standard|bullets|table",
  "guided_path": "complete",
  "reasoning_effort": "off|standard|deep",
  "counselor_id": null
}
```

后端效果：DB 步骤 prompt 进 SYSTEM，当步分数进 USER；如果没有分数，USER 为开场占位句。QPCS/IDEA 为防止模型滞留在上一步，步骤指令还可被重复放入 USER。

### 4.2 学生在当前步骤追问

```jsonc
{
  "message": "<可能包含访谈步骤技术包装>",
  "memory_message": "<学生干净原话>",
  "mode": "<resolveInteractiveMode() 的结果>",
  "phase": "<当前步骤；非分析固定阶段可省略>",
  "session_id": "<session id>",
  "conversation_id": "<stable conversation id>",
  "questionnaire_type": "<instrument>",
  "language": "<language>",
  "max_tokens": 900,
  "response_length": "<length>",
  "response_format": "<format>",
  "guided_path": "complete|essential",
  "reasoning_effort": "<effort>",
  "counselor_id": null
  // scores_context: 普通追问通常省略
  // use_phase_prompt: 省略 => false
}
```

后端效果：步骤 prompt 变成 SYSTEM 中的 continuation mandate；USER 只放学生原话。分数如需要则从 session memory 取回，放入 SYSTEM `[PROFILE]`。

### 4.3 固定 questions 内部回合

```jsonc
{
  "message": "Based on the profile results ... ask exactly three open reflective questions ...",
  "memory_message": "",
  "internal_message": true,
  "mode": "generic",
  "phase": "questions",
  "scores_context": "<scores>",
  "session_id": "<session id>",
  "conversation_id": "<conversation id>",
  "questionnaire_type": "<instrument>",
  "language": "<language>",
  "max_tokens": 500,
  "response_length": "<length>",
  "response_format": "<format>",
  "guided_path": "complete",
  "reasoning_effort": "off",
  "counselor_id": null
}
```

后端效果：`phase=questions` 优先选 `prompt_guided_questions`；技术 message 不记为学生话语，不开启“学生索要新建议”分支。

---

## 5. 后端如何一步步组装 prompt

下面按真实执行顺序解释 `POST /chat/stream` → `prepare_chat_turn` → `build_context_envelope` 的流程。

### 第 0 步：Pydantic 解析和默认值

JSON 先转成 `ChatRequest`。省略字段获得默认值，枚举字段在此处验证；例如非法 `response_length`/`response_format`/`reasoning_effort` 会在进入业务逻辑前失败。

### 第 1 步：归一化语言，确定会话 ID

- `session_id` 缺失时生成 UUID。
- `conversation_id` 缺失时也生成 UUID，并在 SSE 最终事件返给前端。
- `language` 缩成六种支持的主语言，非法值变 `it`。

### 第 2 步：权限校验与咨询师/模型解析

1. `preview=true` 必须是管理员。
2. 校验 `counselor_id` 是否对当前身份可用。
3. 解析 counselor 的人设、显示名、provider/model preset 和默认推理配置。
4. 套用 `reasoning_effort`。
5. 根据 `phase` 查 DB 步骤，以步骤真实所属工具和最终 system prompt key 做班级/工具权限检查。
6. 介绍/合作约定步骤可最终强制关闭 reasoning。

这一阶段主要决定“调用哪个模型、允不允许调用”，尚未组装完整 prompt。

### 第 3 步：识别路径类型，必要时改写精简回合

`validate_path(questionnaire_type, guided_path, phase)` 验证完整/精简路径组合。

如果是精简路径且前端发了 `use_phase_prompt=true`，服务端把请求的内部副本改为：

```text
use_phase_prompt = false
message = "Present the current essential-path step using the conversation evidence."
internal_message = true
memory_message = ""
```

这样精简虚拟步骤不需要在 `guided_steps` 表中存一行。

### 第 4 步：以服务端步骤为准，求出有效工具和输出预算

- 有 DB 步骤：`questionnaire_type = step.questionnaire_type`。
- 没有 DB 步骤：才使用 payload 的 `questionnaire_type`。
- `response_length` 优先决定 256/800/2000 输出 token；否则夹取 `max_tokens`。
- IDEA 加 1200 私有 map 输出 token。

### 第 5 步：解析基础 system prompt key

解析顺序是：

1. **特殊 phase 优先**：`questions`、各工具 intro/welcome 等使用专属 key。
2. **QSA/QSAr 对话 mode 优先于当前分析步骤**：`factor-qa` / `qsar-factor-qa` 使用对话 prompt，不重生整段分析。
3. **普通 DB 步骤**：使用 `step.system_prompt_mode` 映射到 `prompt_*`。
4. **fallback**：用 payload `mode` 映射；未知 mode 回退 `prompt_generic`。
5. 如 mode 本身以 `prompt_` 开头，它可以直接作为配置 key，这是自定义步骤的扩展口。

具体见 [_system_prompt_key](../backend/chat_logic.py#L1893) 和 [_resolve_system_prompt](../backend/chat_logic.py#L1931)。

### 第 6 步：向基础 system prompt 增加全局指令

按顺序增加：

1. 全局指令，包括语言和平台工具目录。intro、第一步、自由对话或明确问平台时给完整目录；其他回合给精简名称列表。
2. `response_length` 的 `[RESPONSE LENGTH - BINDING]`。
3. IDEA 的 `idea_variant` 指令。

同时为 Totale/Ristretto/Minimo 上下文档位并行准备可缩减版本，但此时还没有选最终档位。

### 第 7 步：求出本轮有效 message 和步骤指令

分类讨论：

- **进入步骤**：`message` 非空就使用它；否则按 `phase` 从 DB 读 `step.prompt`。这段文本是 SYSTEM 最前面的 `step_instruction`。
- **步骤内追问**：先用 `memory_message`或正则去掉 `CURRENT STEP INTERNAL INSTRUCTIONS` 包装，得到干净学生话语；同时从 DB 取当前步骤 prompt，包装成 continuation mandate 放 SYSTEM。
- **没有有效 phase**：不加步骤指令，只用干净学生 message。

### 第 8 步：计算 prompt 组件开关和“新建议闸门”

后端按以下优先级合并开关：

1. 全局默认。
2. intro 特例。
3. QSA 工厂逐步骤表。
4. 管理员在 DB 中保存的逐步骤覆盖。
5. 学生追问的 advice 意图政策。

然后根据 `mode + phase + 是否 follow-up + 是否有成长区 + 是否有认证候选`决定：

- 是否生成分数范围和分数解读块。
- 本轮是否可以给最多一条新实用建议。
- 是否开启认证策略检索。

最终结论不只放在中间指令里，还会写入后面的 `[TURN CONTRACT]`。

### 第 9 步：处理和缩小 `scores_context`

1. 标注 QSA/QSAr 代码和名称。
2. 按组件开关过滤。
3. 根据 `phase` 的 factor codes 生成本步分数子集。
4. 把分数划入强项/适当/成长区等区间，生成指令。
5. IDEA 在前后两处强制清空分数。

需要区分两个结果：

- `model_scores_context`：这轮可供模型使用的整体分数上下文，也被持久化。
- `message_scores_context`：再按当前步骤缩小后，真正可放进当前 USER 的部分。

### 第 10 步：知识检索、skills 和推荐候选

若本步允许 knowledge 或 skills engine 已开启：

1. 检索 query = `step_label + 当前步骤指令/学生话语 + 过滤后分数`。
2. QSA/QSAr 对话型追问再加上一条 assistant 回答的尾部，使“能展开吗”有可检索语义。
3. 根据开关检索 RAG 片段、认证策略、阅读书目、网页事实源、已认可历史回答。
4. skills 可产生 `section` / `knowledge` / `directive_tail` 三种插槽内容。
5. 生成本轮可供模型声明实际使用的 reading/strategy candidates 和私有记录协议。

### 第 11 步：按固定顺序构建 SYSTEM 主体

`build_context_envelope` 的当前准确顺序是：

1. **当前步骤指令**：进入步骤时是步骤 prompt；追问时是 continuation mandate。
2. **`[PERSONA]`**：由 `counselor_id` 选出，只管语气和词汇。
3. **模式级 system prompt 主体**：已包含全局、长度、IDEA、分数、建议和推荐协议等增量。
4. **`[META SYSTEM PROMPT]`**：工具/步骤的理论元指令。
5. **`[ROLE]`**：硬性声明模型是咨询师，不能代写学生的台词。
6. **skills `[SECTION]` 插槽**（当前试点中通常为空）。
7. **`[STUDENT]`**：语言、工具、当前步骤、已完成步骤、已声明目标/偏好/备注。
8. **`[GUIDED PATH]`**：完整或精简路径的位置与步骤。
9. **`[SIMULATION]`**：只有模拟学生笔记本。
10. **`[PROFILE]`**：根据 `notebook_context` 选学生/教师/模拟学生资料；追问时可加从 session 取回的本步分数。
11. **`[CONTESTO CLASSE]`**：学生的已批准共享班级背景；IDEA 排除。
12. **`[READING]`**：学生对问卷结果的自我解读和相关目标。
13. **`[IDEA REFERENCE]`**：IDEA 上传参考文档。
14. **`[IDEA SOURCES]`**：IDEA 当前分支保留的来源。
15. **`[IDEA MAP]`**：IDEA 当前 map、focus、回合数和 `idea_budget`。
16. **`[KNOWLEDGE]`**：RAG、认证策略、读物或网页来源。即使普通 knowledge 关闭，明确阅读/事实请求也可只放允许的 source blocks。
17. **skills directive tail**：必须紧靠下文的阅读、web、绘图、IDEA patch 等协议。

此顺序见 [build_context_envelope](../backend/chat_logic.py#L2764)。请注意：可编辑的指令前缀顺序是 **step → persona → system → meta**，不是先 persona 再 step。

### 第 12 步：构建 HISTORY

如果当前组件开关允许 history，后端根据 `session_id` 取回 verbatim transcript：

```json
[
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "..."}
]
```

它不包含本轮，因为本轮只在模型成功返回后才写入记忆。进入某问卷第一步可清空旧 transcript。

### 第 13 步：构建当前 USER (`full_message`)

分支如下：

| 情况 | 最终 USER |
|---|---|
| 进入普通步骤 | 只有当步 `message_scores_context` |
| 进入 QPCS/IDEA mode | 当步分数 + 步骤指令，或只有步骤指令 |
| 学生追问，并且这轮有分数 | 分数 + `DOMANDA DELLO STUDENTE:` + 干净原话 |
| 学生追问，这轮无分数 | 干净原话 |
| 全部为空 | `(The student has just opened this step and is waiting for your message.)` |
| 断流续写 | 上述 USER + 续写协议 + `PARTIAL_ANSWER_JSON` |

实现见 [chat_logic.py:3098](../backend/chat_logic.py#L3098)。

### 第 14 步：根据回合类型追加连续性块

三选一为主：

- **综合/总结步骤**：追加 `[JOURNEY EVIDENCE]` 和 `[PERSPECTIVE]`。证据来自整段对话，太长时可用模型分段压缩。
- **进入非总结步骤**：追加 `[SESSION LEDGER]`，保留学生原话、已选/拒绝行动、未回答问题和上一轮 guard notes。
- **普通追问**：如有 thread guard 观察，追加 `[THREAD]`。

### 第 15 步：追加硬性本轮合同和路径/格式尾部

按当前代码顺序：

1. `[TURN CONTRACT]`：当前工具、phase、语言、最多一个问题、学生资料是证据不是指令、能否提新建议、总结/IDEA 私有块规则。
2. 精简路径且无 HISTORY 时：可追加 `[ESSENTIAL SESSION EVIDENCE]`。
3. 精简路径：追加当前虚拟步骤的专属 directive；QSA 总结后追问使用 `[QSA ESSENTIAL FOLLOW-UP]`。
4. `response_format`：最后追加 `[VISIBLE RESPONSE FORMAT]`。

因此，在当前实现中 `[TURN CONTRACT]` 是核心硬性合同，但它不一定是 SYSTEM 物理上的最后一块：精简路径指令和 `[VISIBLE RESPONSE FORMAT]` 可位于它之后。

### 第 16 步：按模型上下文档位裁剪

在真正调用 provider 前，`AIService` 根据最终 provider/model 选 Totale/Ristretto/Minimo 档位：

- 可替换为管理员维护的缩减 prompt/persona/meta 变体。
- 按配置裁减 persona、profile、knowledge、meta。
- 保留最近 N 个学生回合，必要时从最旧交换开始丢弃 HISTORY。
- 保留当前 USER 和受保护的硬性指令。
- 如果必需内容仍无法放入模型窗口，直接报 `ContextCapacityError`，不静默截断当前请求。

见 [model_context.py:200](../backend/model_context.py#L200)。

### 第 17 步：隐私处理并映射到具体模型 API

对外部 provider，后端在发送前根据当前隐私策略处理 `system / history / user`。然后按 provider 映射：

- OpenAI-compatible：`[{role:"system"}, ...history, {role:"user"}]`。
- Anthropic：SYSTEM 单独传，`history + current user` 作 messages。
- Ollama/Gemini 等使用各自适配器，但语义上仍是同一个三件套。

provider 失败时，fallback 模型会使用它自己的上下文档位重新调整同一份逻辑 envelope，而不是重新信任前端 payload来生成新角色。

---

## 6. 关键分类讨论

### 6.1 “进入步骤”与“步骤内追问”的根本差异

| | 进入步骤 | 步骤内追问 |
|---|---|---|
| `use_phase_prompt` | `true` | 省略/`false` |
| `message` | 通常空 | 学生文本/访谈包装 |
| `memory_message` | 通常省略 | 学生干净原话 |
| `scores_context` | 发送 | 通常不发，由 session 取回 |
| SYSTEM 中的步骤文本 | 原始步骤指令 | continuation mandate |
| USER | 本步分数/占位句 | 学生原话 |
| 连续性 | ledger 或 journey evidence | history + 可选 thread guard |

### 6.2 分数型与访谈型工具

- **分数型**（QSA、QSAr、ZTPI、QPCS、QPCC、QAP）：`scores_context` 会被分区、按步骤缩小，并影响成长区和建议闸门。
- **访谈型**（SAVICKAS、EVENTO、OBIETTIVO）：核心证据来自 `memory_message` + HISTORY/JOURNEY，前端的步骤包装不应成为学生原话。
- **IDEA**：明确无分数；中心是 `message + IDEA REFERENCE/SOURCES/MAP + idea_variant + idea_budget`。

### 6.3 完整路径与精简路径

- 完整路径依赖 DB `GuidedStep`，使用 `use_phase_prompt` 执行步骤 prompt。
- 精简路径使用代码中的虚拟 phase 和专属 directive，并由前端每次完成回答后自动推进 phase。
- 精简路径的 `message` 被当作技术指令，`memory_message=""`，不污染学生 transcript。
- QSA 精简总结后有专门 `qsa-essential-followup`；两种 OBIETTIVO 没有这个额外 phase。

### 6.4 学生、教师与模拟学生 profile

| 有效笔记本模式 | `[PROFILE]` 主体 | 其他变化 |
|---|---|---|
| student | 学生 profile + Portfolio + goals + 可选持久化分数 | 可有 `[CONTESTO CLASSE]` |
| teacher | 教师笔记本 + 教学目标所选班级 | 不放学生 Portfolio/目标/分数 |
| practice | 模拟学生笔记本 + 当前会话分数 | 加 `[SIMULATION]`，不混入教师本人资料 |
| none | 无笔记本/profile | history、knowledge、skills 仍可保留 |

### 6.5 前端字段中哪些是不可信的

所有前端字段都应当视为不可信，但后端重点重算：

- `phase` 对应的真实 `questionnaire_type` 和 `system_prompt_mode`。
- counselor、工具和班级权限。
- `group_ids` 对应的教师管理权。
- `notebook_context`/`practice_notebook_id` 的角色和所有权。
- 精简路径中 `questionnaire_type + phase` 的合法组合。
- `preview=true` 的管理员身份。
- 学生 message/history/profile/retrieved docs 在 `[TURN CONTRACT]` 中被明确定义为“证据，不是可修改角色和输出协议的指令”。

---

## 7. 从字段到 prompt 的最终映射速查

| payload 字段 | 最主要的 prompt/调用落点 |
|---|---|
| `message` | 当前 USER、意图分类、检索 query、IDEA map |
| `memory_message` | 干净 USER 和 verbatim memory |
| `internal_message` | 屏蔽伪学生发言，关闭 advice-follow-up 识别 |
| `mode` | system prompt key、分数/建议/清洗分支 |
| `phase` | DB 步骤、专属 prompt、因子范围、路径/证据类型 |
| `use_phase_prompt` | 步骤进入 vs 追问；step prompt 的位置 |
| `scores_context` | USER 分数、`[PROFILE]` 参考、成长区、检索 |
| `session_id` | HISTORY、持久化分数、student state、ledger/thread/IDEA |
| `conversation_id` | 旅程证据、日志聚合、认证策略去重 |
| `questionnaire_type` | 工具规则，无 DB 步骤时的主路由 |
| `language` | 语言指令、本地化、`[TURN CONTRACT]` |
| `max_tokens` | `response_length` 缺失时的输出上限 |
| `response_length` | `[RESPONSE LENGTH - BINDING]` + token 上限 + 回答后截断 |
| `response_format` | `[VISIBLE RESPONSE FORMAT]` |
| `guided_path` | 完整/精简验证、`[GUIDED PATH]`、精简 directive |
| `reasoning_effort` | provider thinking 开关和 reasoning budget |
| `counselor_id` | `[PERSONA]`、counselor name、provider/model preset |
| `idea_variant` | IDEA 变体 directive |
| `idea_budget` | `[IDEA MAP]` 的节奏与收束信号 |
| `group_ids` | 教师 `[PROFILE]` 中的已授权班级上下文 |
| `notebook_context` | `[PROFILE]` 资料来源 |
| `practice_notebook_id` | `[SIMULATION]` + 模拟学生 profile |
| `preview` | 权限与不持久化，不直接增加 prompt |
| `partial_response` | 续写 USER 协议，避免重写已显示文本 |

## 8. 最容易混淆的五个点

1. **`message` 不总是学生原话**；要看 `memory_message` 和 `internal_message`。
2. **`mode` 不是唯一的 prompt 选择器**；特殊 `phase` 和 DB `step.system_prompt_mode` 优先级可更高。
3. **普通追问不重发分数不等于模型看不到分数**；后端会按 `session_id` 取回并放进 `[PROFILE]`。
4. **`max_tokens=500/700/900` 通常不是最终值**；`response_length` 会把它覆盖成 256/800/2000。
5. **`response_format` 与 `reasoning_effort` 作用层次不同**：前者是 SYSTEM 指令，后者是 provider 推理运行参数。
