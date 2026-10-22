记住一句话就够了：模型是一个昂贵、偶尔犯错、没有记忆的文字 API。AI 应用开发，就是围绕它做好"喂什么、怎么校验、怎么兜底"的 Web 工程。

```
 学生浏览器
    │  ① 发消息
    ▼
 前端 Next.js ──(SSE 代理 /api/chat/stream)──┐
                                              ▼
 ┌──────────────────────── 后端 FastAPI ─────────────────────────┐
 │                                                               │
 │  业务层：权限检查、确认当前步骤、读取学生数据（普通 Web 代码）         │
 │     │                                                         │
 │     ▼                                                         │
 │  ② 检索层：找"参考资料"                                         │
 │     ├ RAG 文档检索（rag_index.py，用 embedding）                 │
 │     ├ 认证过的学习策略、书目库（数据库查询）                         │
 │     └ Skills 引擎：根据学生意图选择要附加的指令                      │
 │     │                                                          │
 │     ▼                                                          │
 │  ③ Prompt 组装层：把规则、资料、历史拼成一个大请求                   │
 │     （chat_preparation.py、chat_logic.py）                      │
 │     │                                                          │
 │     ▼                                                          │
 │  ④ 安全层：去掉姓名、邮箱等隐私信息（pii.py）                        │
 │     │                                                          │
 │     ▼                                                          │
 │  ⑤ 模型调度层 AIService：选模型、失败切换、控制预算                  │
 │     │                                                          │
 └─────┼──────────────────────────────────────────────────────────┘
       ▼
 ┌─ 云端模型：OpenAI / Anthropic / Gemini / OpenRouter ...
 └─ 本地模型：Ollama（跑在自己服务器上，数据不出机器）
       │
       ▼  ⑥ 流式返回文字
 后端：⑦ 解析输出 → ⑧ 存日志和记忆 → ⑨ 另请一个模型做质检
       │
       ▼
 学生看到回复

 旁路的独立 AI 服务（Docker 容器）：
   services/transcription  语音转文字（Whisper）
   services/piper          文字转语音


```

---

顺序	学什么	在项目里看哪里
1	调用一次 LLM API（OpenAI 或 Anthropic 的 SDK），理解 system 和 messages 的区别	ai_service.py 的 _call_openai
2	流式输出与 SSE	routes/chat.py 的 event_gen、chat-stream.ts
3	Prompt 组装	运行 make prompt-dry Q=QSA STEP=intro，亲眼看一次完整信封（最推荐）
4	结构化输出与校验	recommendation_blocks.py、idea_map.py
5	Embedding 与 RAG	rag_index.py
6	多模型调度、降级与成本控制	AIService 的 _targets、_response_with_fallback
7	LLM-as-judge 与效果评估	thread_guard.py、prompt_lab/
8	真正的 Agent 是什么样子

---

## English Version

Remember one thing: a model is an expensive, fallible text API with no memory. AI application development is the web engineering required to decide what to feed it, how to validate its output, and how to recover when something fails.

```
 Student's browser
    │  ① Sends a message
    ▼
 Next.js frontend ──(SSE proxy /api/chat/stream)──┐
                                              ▼
 ┌────────────────────── FastAPI backend ────────────────────────┐
 │                                                               │
 │  Business layer: permission checks, current-step resolution,     │
 │  and student-data loading (ordinary web application code)        │
 │     │                                                         │
 │     ▼                                                         │
 │  ② Retrieval layer: finds reference material                    │
 │     ├ RAG document retrieval (rag_index.py, using embeddings)   │
 │     ├ Certified learning strategies and reading catalogue       │
 │     │ (database queries)                                         │
 │     └ Skills engine: selects additional instructions based on   │
 │       the student's intent                                          │
 │     │                                                          │
 │     ▼                                                          │
 │  ③ Prompt assembly layer: combines rules, materials, and history │
 │     into one large request                                          │
 │     (chat_preparation.py, chat_logic.py)                            │
 │     │                                                          │
 │     ▼                                                          │
 │  ④ Safety layer: removes names, email addresses, and other       │
 │     private information (pii.py)                                    │
 │     │                                                          │
 │     ▼                                                          │
 │  ⑤ Model-routing layer, AIService: selects a model, switches     │
 │     after failures, and controls the budget                         │
 │     │                                                          │
 └─────┼────────────────────────────────────────────────────────────┘
       ▼
 ┌─ Cloud models: OpenAI / Anthropic / Gemini / OpenRouter ...
 └─ Local models: Ollama (runs on the project's own server, so data
   does not leave the machine)
       │
       ▼  ⑥ Streams text back
 Backend: ⑦ parses the output → ⑧ stores logs and memory →
          ⑨ asks another model to perform a quality check
       │
       ▼
 The student sees the response

 Separate AI services running alongside the main path (Docker containers):
   services/transcription  Speech to text (Whisper)
   services/piper          Text to speech
```

---

| Order | What to learn | Where to look in the project |
|---:|---|---|
| 1 | Make one LLM API call using the OpenAI or Anthropic SDK, and understand the difference between `system` and `messages` | `ai_service.py`: `_call_openai` |
| 2 | Streaming output and SSE | `routes/chat.py`: `event_gen`; `chat-stream.ts` |
| 3 | Prompt assembly | Run `make prompt-dry Q=QSA STEP=intro` and inspect a complete prompt envelope firsthand (strongly recommended) |
| 4 | Structured output and validation | `recommendation_blocks.py`, `idea_map.py` |
| 5 | Embeddings and RAG | `rag_index.py` |
| 6 | Multi-model routing, fallback, and cost control | `AIService`: `_targets`, `_response_with_fallback` |
| 7 | LLM-as-a-judge and effectiveness evaluation | `thread_guard.py`, `prompt_lab/` |
| 8 | What a real agent looks like | |
