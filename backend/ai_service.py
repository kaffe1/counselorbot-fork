import os
import logging
import json
import re
from datetime import datetime, timezone
from functools import partial

import anthropic
import httpx
from mistralai.client import Mistral
from openai import OpenAI
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from . import personal_api
from . import pii
from . import pii_ner
from .api_secrets import API_KEY_ENV_MAP, config_key, resolve_api_secrets
from .model_context import context_profile, fit_context
from .reasoning_profiles import DISABLED_PLAN, ReasoningPlan, resolve_plan

logger = logging.getLogger(__name__)

_JSON_RESPONSE_DIRECTIVE_RE = re.compile(
    r"(?<!not )(?<!never )\b(?:return|respond|answer)\s+"
    r"(?:(?:only|with|a|single|raw|valid|strict)\s+){0,5}json\b",
    re.IGNORECASE,
)


def _requests_json_response(system_prompt: str, user_message: str) -> bool:
    """仅当明确要求整个回复以 JSON 格式返回时才为 True。

    仅出现 ``JSON`` 这个词并不够：一段对话可能包含针对嵌入式 JSON 块
    （例如概念图）的指令，但仍需返回一段正常的叙述性回复。
    """
    return bool(_JSON_RESPONSE_DIRECTIVE_RE.search(f"{system_prompt}\n{user_message}"))


class AIError(Exception):
    """AI 提供商的配置错误或调用错误。
    抛出此异常，而不是把错误字符串当作机器人的回复返回。"""
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


# 用于更新会话滚动摘要的提示词
SUMMARY_SYSTEM_PROMPT = (
    "You are an assistant that maintains a rolling summary of the counseling session. "
    "You will receive the previous summary (if any) and the latest interaction. "
    "Produce a SINGLE updated summary in MAXIMUM 80 words in Italian that integrates "
    "the entire journey done so far, including the latest step. "
    "Use a compact bulleted list. Do NOT add introductions or conclusions. "
    "IMPORTANT: Do NOT include raw numerical scores or tables of data from the QSA questionnaire. "
    "Report only the INTERPRETATIONS and ADVICE that emerged from the analysis. "
    "The result REPLACES the previous summary — do not append, rewrite."
)

# 运行时配置键 -> 环境变量的映射（密钥及本地设置）。
ENV_KEY_MAP = {
    **{config_key(provider): env_vars for provider, env_vars in API_KEY_ENV_MAP.items()},
    'ollama_ip': ('OLLAMA_BASE_URL',),
    'omniroute_url': ('OMNIROUTE_API_URL',),
    'ollama_num_ctx': ('OLLAMA_NUM_CTX',),
    'ollama_keep_alive': ('OLLAMA_KEEP_ALIVE',),
    'ollama_preload': ('OLLAMA_PRELOAD',),
    'llamacpp_url': ('LLAMACPP_BASE_URL',),
    'qsa_ocr_model': ('QSA_OCR_MODEL',),
    'qsa_parser_model': ('QSA_PARSER_MODEL',),
}

# 具有 OpenAI 兼容端点（chat/completions）的提供商：全部用同一个
# OpenAI 客户端处理，只需更换 base_url 和密钥。
# 提供商名称 -> base_url。数据库中的键始终为 'api_key_<名称>'。
# 新增一个 OpenAI 兼容提供商 = 在这里加一项（+ ENV_KEY_MAP）。
OPENAI_COMPAT_PROVIDERS = {
    'omniroute': 'http://omniroute:20128/v1',
    'groq':      'https://api.groq.com/openai/v1',
    'cerebras':  'https://api.cerebras.ai/v1',
    'deepseek':  'https://api.deepseek.com/v1',
    'together':  'https://api.together.xyz/v1',
    'fireworks': 'https://api.fireworks.ai/inference/v1',
    'deepinfra': 'https://api.deepinfra.com/v1/openai',
}

# 本地提供商：无需针对外部进行匿名化。
LOCAL_PROVIDERS = {'ollama', 'llamacpp'}
# #202：个人来源（API）上按所选级别分配的推理 token 数。
PERSONAL_REASONING_BUDGETS = {'minimal': 512, 'low': 1024, 'medium': 4096, 'high': 12000, 'xhigh': 24000}

# 无法用于对话的模型 ID（嵌入、语音、图像、内容审核、旧版补全接口）。
NON_CHAT_MODEL_MARKERS = ("embed", "tts", "whisper", "dall-e", "moderation", "transcribe", "gpt-image",
                          "davinci", "babbage", "-audio", "realtime", "guard")


def _chat_model(model_id):
    lowered = (model_id or "").lower()
    return bool(lowered) and not any(marker in lowered for marker in NON_CHAT_MODEL_MARKERS)


class AIService:
    def __init__(self, db: Session, username: str | None = None):
        self.db = db
        self.config = self._load_config()
        # #202：个人来源的推理级别；优先于预设。
        self.personal_reasoning = None
        self.shared_owner = None  # 由哪位教师的咨询机器人和 API 密钥来回答该学生
        self.personal_target = None
        self.personal_error = None
        self.personal_error_code = "personalAPI.errors.configuration"
        from .chatgpt_connections import preference, owner
        self.chatgpt_model = preference(db)
        account = owner(db) if "chatgpt_username" in getattr(db, "info", {}) else username
        try:
            connection = personal_api.active_settings(db, account, getattr(db, "info", {}).get("personal_ai_counselor_id"))
            if connection:
                self.use_personal_connection(connection)
            if connection or self.chatgpt_model:
                self.personal_reasoning = personal_api.chosen_reasoning(
                    db, account, getattr(db, "info", {}).get("personal_ai_counselor_id"))
            else:
                # 若用户自己没有选择，则由学生的教师所提供的咨询机器人
                # 通过该教师的 API 连接来回答。
                from .class_api_sharing import offers
                offer = offers(db, account).get(getattr(db, "info", {}).get("personal_ai_counselor_id"))
                if offer is not None:
                    self.shared_owner, connection_id, reasoning = offer
                    self.use_personal_connection(db.get(models.PersonalAPIConnection, connection_id))
                    self.personal_reasoning = reasoning
        except personal_api.PersonalAPIError as exc:
            # 推迟到 dispatch 阶段再抛出，以便现有的 HTTP/SSE 错误处理器
            # 能够报告它，同时禁止回退到系统凭据。
            self.personal_error = str(exc)
            if self.shared_owner is not None:
                from .class_api_sharing import SHARE_ERROR, record_error
                record_error(db, self.shared_owner, "personalAPI.errors.configuration")
                self.personal_error, self.personal_error_code = "Class AI connection failed.", SHARE_ERROR
        self.last_usage = None
        self.last_provider = None
        self.last_model = None
        self.last_attempts = []
        self.last_context_report = None
        self.context_data = None
        self.last_envelope = None
        # 最近一次提取到的「正在思考」推理内容（Ollama 原生字段或 <think> 标签），
        # 作为独立通道提供给非流式调用方（例如 /live 审计）。
        self.last_thinking = None
        if self.chatgpt_model:
            self.config.update(active_provider="openai_chatgpt", model_name=self.chatgpt_model)
        # "no thinking" 模式：在支持推理的模型上关闭推理
        self.disable_thinking = str(self.config.get('disable_thinking', 'false')).lower() == 'true'
        # 可选的推理预算覆盖值（例如来自咨询机器人的预设）。
        # None = 使用该模型系列的默认值（reasoning_profiles）。
        self.reasoning_budget_override = None
        # 本实例的显式温度。None = 交给提供商决定，这也是一直以来的行为：
        # 此前没有任何代码路径发送 `temperature`。它是给需要对相同输入
        # 得到相同结果的调用方用的 —— 评判器、解析器 —— 而不是给对话用的。
        self.temperature = None
        # 当前调用的推理计划：在 dispatch 之前按 (provider, model) 解析。
        # 对直接调用/摘要使用中性默认值（关闭）。
        self._reasoning_plan = DISABLED_PLAN
        # 月度预算：若 >0 且当月支出超过该值，则停用付费模型，
        # 只使用本地 Ollama（回退）。
        try:
            self.monthly_budget_usd = float(self.config.get('monthly_budget_usd', 0) or 0)
        except (TypeError, ValueError):
            self.monthly_budget_usd = 0.0
        self.budget_fallback_model = (self.config.get('budget_fallback_model') or 'muse-glimmer:30b').strip()
        self._budget_locked_cache = None  # 每个实例惰性计算一次
        # 大上下文：启用 thinking 时 num_predict 可能很高，不应被
        # 上下文长度（提示词 + 推理 + 回复）卡住。
        self.ollama_num_ctx = self._int_config('ollama_num_ctx', 16384, 2048, 32768)
        self.ollama_keep_alive = self.config.get('ollama_keep_alive', '5m') or '5m'
        self.ollama_preload_enabled = str(self.config.get('ollama_preload', 'false')).lower() == 'true'
        # 本地嵌入模型（通过 Ollama），用于网站聊天机器人的 RAG。
        self.embedding_model = (self.config.get('embedding_model') or 'qwen3-embedding:4b').strip()
        # 发往外部提供商时的 PII（个人身份信息）匿名化（见 `_anonymize_external`）。
        # `block` = NER 检测器无响应时报错；`send_raw` = 照样发送。
        self.external_pii_redact = str(self.config.get('external_pii_redact', 'true')).lower() not in ('0', 'false', 'no', 'off')
        self.external_pii_fallback = (self.config.get('external_pii_fallback') or 'block').strip().lower()

        # 提供商注册表：同步/流式分发的唯一可信来源。
        # call_max  = 阻塞调用（get_response）的默认 max_tokens
        # stream_max = 流式输出的默认 max_tokens
        # stream    = None → 该提供商不支持增量流式（通过 call 一次性返回单个块）
        # 新增提供商 = 在这里加一项 + 对应的 _call_/_stream_ 方法。
        self._providers = {
            'openai_chatgpt': {'call': self._call_chatgpt, 'stream': self._stream_chatgpt, 'call_max': None, 'stream_max': None},
            'openai':     {'call': self._call_openai,     'stream': self._stream_openai,     'call_max': None, 'stream_max': None},
            'anthropic':  {'call': self._call_anthropic,  'stream': self._stream_anthropic,  'call_max': 4096, 'stream_max': 4096},
            'openrouter': {'call': self._call_openrouter, 'stream': self._stream_openrouter, 'call_max': None, 'stream_max': None},
            'gemini':     {'call': self._call_gemini,     'stream': None,                    'call_max': None, 'stream_max': None},
            'mistral':    {'call': self._call_mistral,    'stream': None,                    'call_max': None, 'stream_max': None},
            'ollama':     {'call': self._call_ollama,     'stream': self._stream_ollama,     'call_max': 8000, 'stream_max': 4096},
            'llamacpp':   {'call': self._call_llamacpp,   'stream': self._stream_llamacpp,   'call_max': 8000, 'stream_max': None},
        }
        # OpenAI 兼容提供商（groq/cerebras/deepseek/together/fireworks/deepinfra）：
        # 走同一条 OpenAI 路径，只是 base_url 不同。通过 partial 绑定提供商名称。
        for _name in OPENAI_COMPAT_PROVIDERS:
            self._providers[_name] = {
                'call':   partial(self._call_openai_compatible, _name),
                'stream': partial(self._stream_openai_compatible, _name),
                'call_max': None, 'stream_max': None,
            }

    def use_personal_connection(self, connection):
        self.personal_target = (connection.provider, connection.model_name)
        self.personal_error = None
        self.chatgpt_model = None
        for provider in API_KEY_ENV_MAP:
            self.config.pop(config_key(provider), None)
        self.config[config_key(connection.provider)] = personal_api.decrypt_key(connection)
        self.config.update(active_provider=connection.provider, model_name=connection.model_name,
                           summary_model_name=connection.model_name)

    def _personal_failure(self, exc):
        if self.shared_owner is not None:
            # 学生只看到中性的提示；教师会在自己的 API 页面上看到具体原因。
            from .class_api_sharing import SHARE_ERROR, record_error
            record_error(self.db, self.shared_owner, personal_api.connection_error_code(exc))
            return AIError("Class AI connection failed.", code=SHARE_ERROR)
        return AIError("Personal AI connection failed.", code=personal_api.connection_error_code(exc))

    @staticmethod
    def _usage_to_dict(usage):
        if usage is None:
            return None
        if isinstance(usage, dict):
            return usage
        if hasattr(usage, "model_dump"):
            return usage.model_dump()
        if hasattr(usage, "dict"):
            return usage.dict()
        data = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost", "cost_usd", "total_cost"):
            if hasattr(usage, key):
                data[key] = getattr(usage, key)
        return data or None

    def _provider(self, name: str) -> dict:
        """未知提供商会显式报错；绝不能因此选中付费服务。"""
        if name not in self._providers:
            raise AIError(f"Provider sconosciuto: {name}")
        return self._providers[name]

    @staticmethod
    def _normalize_history(history) -> list:
        """将带角色标签的历史记录（{role,content}）规范化为干净的
        user/assistant 消息列表，按时间顺序排列。为空或格式错误时
        返回 []。提供商始终接受列表（即使为空）。"""
        if not history:
            return []
        out = []
        for item in history:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                out.append({"role": role, "content": content})
        return out

    # 咨询机器人的预设会在服务创建后设置这些值：
    # 为个人来源所选的级别（#202）优先。
    @property
    def disable_thinking(self):
        if self.personal_reasoning:
            return self.personal_reasoning == 'none'
        return self._disable_thinking

    @disable_thinking.setter
    def disable_thinking(self, value):
        self._disable_thinking = value

    @property
    def reasoning_budget_override(self):
        if self.personal_reasoning:
            return PERSONAL_REASONING_BUDGETS.get(self.personal_reasoning)
        return self._reasoning_budget_override

    @reasoning_budget_override.setter
    def reasoning_budget_override(self, value):
        self._reasoning_budget_override = value

    def _resolve_reasoning(self, model, requested_max_tokens, fallback_max_tokens) -> ReasoningPlan:
        """解析并保存针对（模型、开关、预算）的推理计划。

        返回该计划；`plan.max_tokens` 是已包含回复预留空间（headroom）的
        输出预算，用于发送给提供商。"""
        plan = resolve_plan(
            model,
            disable_thinking=self.disable_thinking,
            requested_max_tokens=requested_max_tokens,
            fallback_max_tokens=fallback_max_tokens,
            budget_override=self.reasoning_budget_override,
        )
        self._reasoning_plan = plan
        return plan

    def _plan(self) -> ReasoningPlan:
        """当前的推理计划（尚未解析时为中性默认值）。"""
        return getattr(self, "_reasoning_plan", None) or DISABLED_PLAN

    def _budget_is_locked(self) -> bool:
        """若已设置月度预算（>0）且当月支出（所有费用）已达到/超过该预算，
        则返回 True。按实例缓存（每个请求只查询一次）。"""
        if self.monthly_budget_usd <= 0:
            return False
        if self._budget_locked_cache is not None:
            return self._budget_locked_cache
        try:
            month_start = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            spent = self.db.query(func.coalesce(func.sum(models.Log.cost_usd), 0.0)).filter(
                models.Log.timestamp >= month_start,
                models.Log.cost_usd.isnot(None),
                func.coalesce(models.Log.details["credential_source"].as_string(), "system") != "personal",
            ).scalar() or 0.0
            self._budget_locked_cache = float(spent) >= self.monthly_budget_usd
        except Exception as e:
            logger.warning("Budget unavailable; paid dispatch blocked (%s)", type(e).__name__)
            self._budget_locked_cache = True
        return self._budget_locked_cache

    def _apply_budget_lock(self, provider: str, model: str):
        """若预算已超支，强制改用本地 Ollama + 回退模型。
        本来就是本地的提供商保持不变。不影响 benchmark
        （它使用 call_model，即管理员显式指定的路径）。"""
        if self.personal_target:
            return self.personal_target
        if not self._free_target(provider, model) and self._budget_is_locked():
            fallback = self.budget_fallback_model or 'muse-glimmer:30b'
            logger.info(f"Budget mensile superato: fallback {provider}/{model} -> ollama/{fallback}")
            return 'ollama', fallback
        return provider, model

    def _openrouter_reasoning(self) -> dict:
        """根据当前计划生成 OpenRouter 的 `reasoning` 参数。

        推理开启 -> 启用并设置上限 `max_tokens`（若已知），使推理
        不会占用为回复预留的预算。推理关闭 ->
        `enabled: False`（有些模型仍会推理，但不分配预算）。"""
        plan = self._plan()
        if not plan.enabled:
            return {"enabled": False}
        reasoning: dict = {"enabled": True}
        if plan.reasoning_budget:
            reasoning["max_tokens"] = int(plan.reasoning_budget)
        return reasoning

    def _anthropic_thinking(self, max_tokens: int | None) -> dict | None:
        """根据当前计划生成 Anthropic 的 `thinking` 参数；关闭时返回 None。

        Anthropic 要求 budget_tokens >= 1024 且 max_tokens > budget_tokens：
        预算会被压缩（clamp），为回复留出空间。"""
        plan = self._plan()
        if not plan.enabled or not plan.reasoning_budget:
            return None
        budget = int(plan.reasoning_budget)
        if max_tokens:
            budget = min(budget, max(int(max_tokens) - 512, 0))
        if budget < 1024:
            return None
        return {"type": "enabled", "budget_tokens": budget}

    def _int_config(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    def _apply_no_think(self, system_prompt: str) -> str:
        """在 system prompt 末尾追加 /no_think 指令（qwen3/llama.cpp/ollama 的约定）。"""
        if self.disable_thinking and "/no_think" not in system_prompt:
            return f"{system_prompt}\n\n/no_think"
        return system_prompt

    def _apply_ollama_thinking_directive(self, system_prompt: str) -> str:
        # Ollama 原生地把 thinking 和正文内容分开。如果再要求它输出
        # <think> 标签，模型可能会引用/嵌套标签，或在回答中重复计划
        # （Nemotron Cascade）。把 UI 约定留在传输层处理。
        directive = (
            "[THINKING] Use only the model's native thinking channel for internal reasoning. "
            "The content channel must contain only the final student-facing answer. "
            "Do not reproduce your reasoning, plans, instructions, thinking tags, or "
            "Reasoning/Answer headings in the content channel."
        )
        system_prompt = re.sub(
            r"\[THINKING\].*?(?=\n\s*\n|\n\[[A-Z_ ]+\]|$)",
            lambda _: directive, system_prompt, flags=re.DOTALL,
        )
        return self._apply_no_think(system_prompt)

    def _ollama_base(self) -> str:
        return (self.config.get('ollama_ip') or 'http://localhost:11434').rstrip('/')

    def _anonymize_external(self, provider: str, user_message: str,
                            system_prompt: str, history: list):
        """为外部提供商匿名化请求封装（envelope）（GDPR 数据最小化原则）。

        返回 `(user_message, system_prompt, history, mapping)`；对于本地
        提供商或开关关闭的情况，文本保持不变，mapping 为空。
        当 `external_pii_fallback=block` 时，若 NER 检测器不可达，
        则阻止此次调用，而不是暴露数据。"""
        if provider in LOCAL_PROVIDERS or not self.external_pii_redact:
            return user_message, system_prompt, history, {}
        history = history or []
        texts = [user_message, system_prompt] + [m.get('content', '') for m in history]
        # 读取本实例的数据库快照：管理员选择 basic 之后，
        # 其他 worker 的模块级开关不应继续要求使用 Ollama。
        ner_flag = self.config.get('pii_ner_enabled')
        ner_enabled = None if ner_flag is None else str(ner_flag).strip().lower() not in ('0', 'false', 'no', 'off')
        anon_texts, mapping, ner_ok = pii_ner.anonymize_texts(
            texts, ollama_base=self._ollama_base(), ner_enabled=ner_enabled,
            ner_model=self.config.get('pii_ner_model'))
        if not ner_ok and self.external_pii_fallback == 'block':
            raise AIError(
                "Anonimizzazione PII non disponibile (modello locale non raggiungibile). "
                "Riprova, oppure disattiva la redazione esterna dal pannello admin.",
                code="personalAPI.errors.privacy")
        anon_history = [
            dict(m, content=anon_texts[2 + i]) if m.get('content') else m
            for i, m in enumerate(history)
        ]
        return anon_texts[0], anon_texts[1], anon_history, mapping

    def _load_config(self):
        configs = self.db.query(models.Config).all()
        config_dict = {c.key: c.value for c in configs}

        # API 密钥有专门的单一来源约定，绝不会从通用的
        # Config 表中读取。
        for provider, resolved in resolve_api_secrets(self.db).items():
            key = config_key(provider)
            if resolved.value:
                config_dict[key] = resolved.value
            else:
                config_dict.pop(key, None)

        # 非密钥类运行时设置保留原有的环境变量覆盖机制。
        for db_key, env_vars in ENV_KEY_MAP.items():
            if db_key.startswith('api_key_'):
                continue
            for env_var in env_vars:
                env_value = os.environ.get(env_var)
                if env_value:
                    config_dict[db_key] = env_value
                    break

        return config_dict

    def _get_api_key(self, provider_key):
        return self.config.get(provider_key)

    def list_models(self, provider: str = None):
        """列出提供商实际提供的模型（OpenAI 兼容端点 /v1/models）。
        返回字符串 ID 列表；若提供商无法查询或不可达，则返回空列表。"""
        provider = provider or self.config.get('active_provider', 'openai')
        timeout = httpx.Timeout(15.0, connect=5.0)
        try:
            if provider == 'gemini':
                api_key = self._get_api_key('api_key_gemini')
                if not api_key:
                    return []
                from google import genai
                client = genai.Client(api_key=api_key)
                names = []
                for m in client.models.list():
                    actions = getattr(m, 'supported_actions', None) or []
                    if 'generateContent' in actions:
                        names.append((m.name or '').removeprefix('models/'))
                return sorted(n for n in names if n)
            if provider == 'mistral':
                api_key = self._get_api_key('api_key_mistral')
                if not api_key:
                    return []
                # Mistral 还会列出嵌入、OCR 和审核模型：只保留对话模型。
                listed = Mistral(api_key=api_key).models.list().data or []
                return sorted({m.id for m in listed
                               if m.id and getattr(getattr(m, 'capabilities', None), 'completion_chat', False)})
            if provider == 'anthropic':
                api_key = self._get_api_key('api_key_anthropic')
                if not api_key:
                    return []
                return sorted(m.id for m in anthropic.Anthropic(api_key=api_key, timeout=15).models.list(limit=100).data)
            if provider == 'openai':
                client = OpenAI(api_key=self._get_api_key('api_key_openai'), timeout=timeout)
            elif provider == 'openrouter':
                client = OpenAI(base_url="https://openrouter.ai/api/v1",
                                api_key=self._get_api_key('api_key_openrouter'), timeout=timeout)
            elif provider == 'ollama':
                base = (self._get_api_key('ollama_ip') or "http://localhost:11434").rstrip('/')
                client = OpenAI(base_url=f"{base}/v1", api_key="ollama", timeout=timeout)
            elif provider == 'llamacpp':
                base = (self._get_api_key('llamacpp_url') or "http://localhost:8080").rstrip('/')
                if not base.endswith('/v1'):
                    base = f"{base}/v1"
                client = OpenAI(base_url=base, api_key="llamacpp", timeout=timeout)
            elif provider in OPENAI_COMPAT_PROVIDERS:
                client = self._openai_compatible_client(provider, timeout=timeout)
            else:
                return []
            return sorted({m.id for m in client.models.list().data if _chat_model(m.id)})
        except Exception as e:
            logger.warning(f"list_models fallito per {provider}: {e}")
            return []

    def verify_api_key(self, provider: str) -> bool:
        """向提供商发起一次带认证的只读请求。"""
        api_key = self._get_api_key(f"api_key_{provider}")
        if not api_key:
            return False
        timeout = httpx.Timeout(15.0, connect=5.0)
        try:
            if provider == "anthropic":
                anthropic.Anthropic(api_key=api_key, timeout=15).models.list(limit=1)
            elif provider == "gemini":
                from google import genai
                client = genai.Client(api_key=api_key)
                next(iter(client.models.list()), None)
            elif provider == "mistral":
                Mistral(api_key=api_key).models.list()
            else:
                base_url = None
                if provider == "openrouter":
                    base_url = "https://openrouter.ai/api/v1"
                elif provider in OPENAI_COMPAT_PROVIDERS:
                    base_url = OPENAI_COMPAT_PROVIDERS[provider]
                    if provider == "omniroute":
                        base_url = self.config.get("omniroute_url") or base_url
                kwargs = {"base_url": base_url} if base_url else {}
                client = OpenAI(api_key=api_key, timeout=timeout, **kwargs)
                client.models.list()
            return True
        except Exception as exc:
            logger.warning("Verifica chiave API fallita per %s (%s)", provider, type(exc).__name__)
            return False

    def get_response(
        self,
        user_message: str,
        system_prompt: str,
        mode: str,
        conversation_summary: str = "",
        max_tokens: int = None,
        provider: str = None,
        model: str = None,
        history: list = None,
        json_mode: bool | None = None,
    ):
        """
        生成一条 LLM 回复。若提供了 conversation_summary，则将其作为
        上下文注入到用户消息的开头。
        `history`（可选）：之前各轮的带角色标签历史 [{role,content}]，
        以原生 ⟨user,assistant⟩ 消息的形式注入到当前用户消息之前。
        `provider`/`model` 可选，含义同上。
        """
        if conversation_summary:
            user_message = f"CONTEXT OF PREVIOUS CONVERSATIONS:\n{conversation_summary}\n\n{user_message}"
        return self._response_with_fallback(
            user_message, system_prompt, provider, model, max_tokens, history, json_mode,
        )

    def _selected_target(self, provider, model):
        if self.personal_error:
            raise AIError(self.personal_error, code=self.personal_error_code)
        if self.chatgpt_model:
            return "openai_chatgpt", self.chatgpt_model
        if self.personal_target:
            return self.personal_target
        chosen = provider or self.config.get("active_provider", "openai")
        if model:
            return chosen, model
        if provider and chosen != self.config.get("active_provider"):
            selected = self.config.get(f"model_name_{chosen}")
            defaults = {"openrouter": "openrouter/free", "omniroute": "auto/best-chat",
                        "ollama": self.budget_fallback_model, "llamacpp": "default"}
            selected = selected or defaults.get(chosen)
            if not selected:
                raise AIError(f"Seleziona un modello per {chosen}")
            return chosen, selected
        return chosen, self.config.get("model_name", "gpt-4o")

    @staticmethod
    def _free_target(provider, model):
        return provider in LOCAL_PROVIDERS or (provider == "openrouter" and
                (model == "openrouter/free" or (model.endswith(":free") and not model.startswith("openrouter/auto"))))

    def _targets(self, provider, model):
        primary = self._selected_target(provider, model)
        if self.personal_target or primary[0] == "openai_chatgpt":
            return [primary]
        primary = self._apply_budget_lock(*primary)
        raw = self.config.get("ai_fallback_targets", "[]")
        configured = json.loads(raw) if isinstance(raw, str) else raw
        targets = [primary]
        for target in configured or []:
            candidate = (target["provider"], target["model"])
            self._provider(candidate[0])
            if candidate not in targets:
                # 网关的名称并不能证明其上游是免费的。
                if not self._free_target(*candidate) and self._budget_is_locked():
                    continue
                targets.append(candidate)
        return targets[:4]

    def _attempt_input(self, provider, model, message, system, history, max_tokens, streaming):
        entry = self._provider(provider)
        plan = self._resolve_reasoning(model, requested_max_tokens=max_tokens,
                                       fallback_max_tokens=entry['stream_max' if streaming else 'call_max'])
        profile = context_profile(self.config, provider, model)
        system, message, history, self.last_context_report = fit_context(
            system, message, self._normalize_history(history), profile, plan.max_tokens,
            context_data=self.context_data,
        )
        self.last_envelope = {"system_prompt_final": system, "full_message": message, "history": history}
        # 从本地回退到云端同样会跨越隐私边界。
        message, system, history, mapping = self._anonymize_external(provider, message, system, history)
        self.last_usage = None
        self.last_thinking = None
        self.last_provider, self.last_model = provider, model
        return entry, plan.max_tokens, message, system, history, mapping

    def _response_with_fallback(self, message, system, provider, model, max_tokens, history, json_mode):
        self.last_attempts = []
        last_error = None
        for selected_provider, selected_model in self._targets(provider, model):
            attempt = {"provider": selected_provider, "model": selected_model, "status": "failed"}
            self.last_attempts.append(attempt)
            try:
                entry, mt, msg, sys, hist, mapping = self._attempt_input(
                    selected_provider, selected_model, message, system, history, max_tokens, False)
                kwargs = {"max_tokens": mt, "history": hist}
                if selected_provider == "ollama":
                    kwargs["json_mode"] = json_mode
                result = entry['call'](msg, sys, selected_model, **kwargs)
                if not result or not result.strip():
                    raise AIError("Il modello ha restituito una risposta vuota")
                attempt["status"] = "succeeded"
                return pii.restore(result, mapping) if mapping else result
            except Exception as exc:
                last_error = exc
                # 只记录错误类型；上游错误信息可能包含请求数据。
                attempt["error_type"] = type(exc).__name__
                logger.warning("AI attempt %s/%s failed (%s)", selected_provider, selected_model, type(exc).__name__)
        if self.chatgpt_model and isinstance(last_error, AIError):
            raise last_error
        if self.personal_target:
            raise self._personal_failure(last_error) from None
        raise AIError(f"Nessun modello configurato ha completato la risposta ({type(last_error).__name__}).") from last_error

    def call_model(self, provider: str, model: str, user_message: str, system_prompt: str, max_tokens: int = None):
        """对一个显式指定的 (provider, model) 发起阻塞调用，绕过当前激活的
        配置。用户显式的个人选择甚至优先于辅助模型。没有个人身份的
        benchmark 仍按显式指定执行。
        当提供商返回用量信息时，填充 self.last_usage（token/费用）。"""
        if self.personal_error:
            raise AIError(self.personal_error, code=self.personal_error_code)
        if self.chatgpt_model:
            provider, model = "openai_chatgpt", self.chatgpt_model
        elif self.personal_target:
            provider, model = self.personal_target
        self.last_provider, self.last_model = provider, model
        entry = self._provider(provider)
        plan = self._resolve_reasoning(model, requested_max_tokens=max_tokens,
                                       fallback_max_tokens=entry['call_max'])
        mt = plan.max_tokens
        self.last_usage = None
        user_message, system_prompt, _history, pii_mapping = self._anonymize_external(
            provider, user_message, system_prompt, None)
        try:
            result = entry['call'](user_message, system_prompt, model, max_tokens=mt)
            return pii.restore(result, pii_mapping) if pii_mapping else result
        except AIError:
            raise
        except Exception as e:
            logger.error("Errore call_model (%s/%s): %s", provider, model, type(e).__name__)
            if self.personal_target:
                raise self._personal_failure(e) from None
            raise AIError(f"Errore AI ({provider}/{model}): {type(e).__name__}") from e

    # 摘要的最大 token 数（约 80 个词 → 120 个 token 就足够）
    SUMMARY_MAX_TOKENS = 300

    def generate_summary(self, user_message: str, bot_response: str, step_label: str = "", previous_summary: str = "") -> str:
        """
        生成更新后的会话滚动摘要。
        融合之前的摘要 + 最近一次交互 → 生成一段 ≤80 词的文本，
        它会替换旧的摘要（不会累加）。
        """
        provider = self.config.get('active_provider', 'openai')
        model_name = self.config.get('model_name', 'gpt-4o')

        # 若已配置，摘要使用更轻量/更快的模型
        summary_model = self.config.get('summary_model_name', '') or model_name
        logger.info(f"generate_summary: provider={provider} model={summary_model}")

        prefix = f"[{step_label}] " if step_label else ""
        trunc_user = user_message[:1000]
        trunc_bot = bot_response[:1500]

        if previous_summary:
            user_msg = (
                f"RIASSUNTO PRECEDENTE DELLA SESSIONE:\n{previous_summary}\n\n"
                f"ULTIMA INTERAZIONE {prefix}:\n"
                f"UTENTE:\n{trunc_user}\n\n"
                f"ASSISTENTE:\n{trunc_bot}\n\n"
                f"Aggiorna il riassunto incorporando l'ultima interazione. Massimo 80 parole totali."
            )
        else:
            user_msg = (
                f"{prefix}Riassumi questa prima interazione:\n\n"
                f"UTENTE:\n{trunc_user}\n\n"
                f"ASSISTENTE:\n{trunc_bot}"
            )

        mt = self.SUMMARY_MAX_TOKENS
        # 摘要是简短任务：不使用推理（否则会消耗预算）。
        self._reasoning_plan = DISABLED_PLAN
        provider, summary_model = self._apply_budget_lock(provider, summary_model)
        # 对发往外部提供商的文本进行匿名化（摘要保留占位符：
        # 它是内部文本，从不向学生展示）。
        user_msg, _sp, _h, _m = self._anonymize_external(
            provider, user_msg, SUMMARY_SYSTEM_PROMPT, None)
        try:
            return self._provider(provider)['call'](user_msg, SUMMARY_SYSTEM_PROMPT, summary_model, max_tokens=mt)
        except Exception as e:
            logger.error("Errore nella generazione del riassunto (%s)", type(e).__name__)
            # 回退方案：简单截断
            truncated = bot_response[:300].rsplit(' ', 1)[0]
            return f"{prefix}{truncated}..."

    def _call_openai(self, user_message, system_prompt, model, max_tokens: int = None, history=None):
        api_key = self._get_api_key('api_key_openai')
        if not api_key: raise AIError("OpenAI API Key non configurata")

        client = OpenAI(api_key=api_key, timeout=self._int_config("ai_timeout_seconds", 120, 10, 600), max_retries=0)
        nak = self._normalize_history(history)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(nak)
        messages.append({"role": "user", "content": user_message})
        kwargs = dict(
            model=model,
            messages=messages,
        )
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    def _call_openrouter(self, user_message, system_prompt, model, max_tokens: int = None, history=None):
        api_key = self._get_api_key('api_key_openrouter')
        if not api_key: raise AIError("OpenRouter API Key non configurata")

        # OpenRouter 使用 OpenAI 客户端结构，并指定 base_url
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            timeout=self._int_config("ai_timeout_seconds", 120, 10, 600),
            max_retries=0,
        )
        nak = self._normalize_history(history)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(nak)
        messages.append({"role": "user", "content": user_message})
        kwargs = dict(
            model=model,
            messages=messages,
        )
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        kwargs["extra_body"] = {"reasoning": self._openrouter_reasoning()}
        response = client.chat.completions.create(**kwargs)
        self.last_usage = self._usage_to_dict(getattr(response, "usage", None))
        if isinstance(getattr(response, "model", None), str):
            self.last_model = response.model
        return response.choices[0].message.content

    def _openai_compatible_client(self, provider: str, timeout=None):
        """指向某个 OpenAI 兼容提供商（base_url + 密钥）的 OpenAI 客户端。"""
        api_key = self._get_api_key(f'api_key_{provider}')
        base_url = OPENAI_COMPAT_PROVIDERS[provider]
        if provider == "omniroute":
            base_url = self.config.get("omniroute_url") or base_url
            api_key = api_key or "omniroute"
        if not api_key:
            raise AIError(f"{provider} API Key non configurata")
        timeout = timeout or self._int_config("ai_timeout_seconds", 120, 10, 600)
        return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)

    def _call_openai_compatible(self, provider, user_message, system_prompt, model, max_tokens: int = None, history=None):
        client = self._openai_compatible_client(provider)
        nak = self._normalize_history(history)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(nak)
        messages.append({"role": "user", "content": user_message})
        kwargs = dict(
            model=model,
            messages=messages,
        )
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if provider == "deepseek" and _requests_json_response(system_prompt, user_message):
            # 结构化提取需要的是正文内容，而不是冗长的推理过程——
            # 后者可能在返回任何 JSON 之前就耗尽输出预算。
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        response = client.chat.completions.create(**kwargs)
        self.last_usage = self._usage_to_dict(getattr(response, "usage", None))
        if isinstance(getattr(response, "model", None), str):
            self.last_model = response.model
        return response.choices[0].message.content

    def _call_anthropic(self, user_message, system_prompt, model, max_tokens: int = 4096, history=None):
        api_key = self._get_api_key('api_key_anthropic')
        if not api_key: raise AIError("Anthropic API Key non configurata")

        client = anthropic.Anthropic(api_key=api_key, timeout=self._int_config("ai_timeout_seconds", 120, 10, 600), max_retries=0)
        nak = self._normalize_history(history)
        messages = list(nak)
        messages.append({"role": "user", "content": user_message})
        kwargs = dict(
            model=model,
            system=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
        )
        thinking = self._anthropic_thinking(max_tokens)
        if thinking:
            kwargs["thinking"] = thinking
        response = client.messages.create(**kwargs)
        # 启用 thinking 时第一个块的类型是 "thinking"：取其中的文本块。
        for block in response.content:
            if getattr(block, "type", None) == "text" and getattr(block, "text", None):
                return block.text
        return getattr(response.content[0], "text", "")

    def _call_gemini(self, user_message, system_prompt, model, max_tokens: int = None, history=None):
        api_key = self._get_api_key('api_key_gemini')
        if not api_key: raise AIError("Gemini API Key non configurata")

        # google-genai SDK：每个实例一个客户端（不像旧版 genai.configure 那样使用全局状态），
        # 通过 HttpOptions 以毫秒为单位设置超时。
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=self._int_config("ai_timeout_seconds", 120, 10, 600) * 1000))
        contents = [types.Content(
            role="user" if turn["role"] == "user" else "model",
            parts=[types.Part.from_text(text=turn["content"])],
        ) for turn in self._normalize_history(history)]
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_message)]))
        # gemini 2.5/3 的 "thinking" 模型会把输出预算花在推理上：
        # max_tokens 较小时 response.text 会返回空。若启用了
        # no-thinking，则把推理预算清零（thinking_budget=0）。
        config_kwargs = {"system_instruction": system_prompt}
        if max_tokens:
            config_kwargs["max_output_tokens"] = max_tokens
        # thinking_budget 受推理计划约束：关闭 -> 0；开启 -> 显式上限
        # （max_output_tokens 已包含回复的预留空间）。
        plan = self._plan()
        if not plan.enabled:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        elif plan.reasoning_budget:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=int(plan.reasoning_budget))
        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        text = response.text
        if not text:
            # 空回复：通常是预算被推理耗尽了。
            raise AIError(
                "Gemini ha restituito una risposta vuota "
                "(possibile modello ritirato o budget token esaurito dal reasoning)."
            )
        return text

    def _call_mistral(self, user_message, system_prompt, model, max_tokens: int = None, history=None):
        api_key = self._get_api_key('api_key_mistral')
        if not api_key: raise AIError("Mistral API Key non configurata")

        client = Mistral(api_key=api_key)
        nak = self._normalize_history(history)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(nak)
        messages.append({"role": "user", "content": user_message})
        kwargs = dict(model=model, messages=messages)
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        response = client.chat.complete(**kwargs)
        return response.choices[0].message.content

    def _call_ollama(self, user_message, system_prompt, model, max_tokens: int = 8000, history=None, json_mode: bool | None = None):
        base_url = (self._get_api_key('ollama_ip') or "http://localhost:11434").rstrip('/')
        system_prompt = self._apply_ollama_thinking_directive(system_prompt)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self._normalize_history(history))
        messages.append({"role": "user", "content": user_message})
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.ollama_keep_alive,
            "options": {
                "num_ctx": context_profile(self.config, "ollama", model).get("context_tokens") or self.ollama_num_ctx,
                "num_predict": max_tokens,
            },
        }
        if self.temperature is not None:
            payload["options"]["temperature"] = self.temperature
        # JSON 提取（QSA 解析器）：绝不推理，否则 `think` 可能
        # 破坏/拖慢结构化输出。无论如何都强制关闭 think。
        is_json = json_mode if json_mode is not None else _requests_json_response(system_prompt, user_message)
        if self.disable_thinking or is_json:
            payload["think"] = False
        else:
            payload["think"] = self._plan().enabled
        if is_json:
            payload["format"] = "json"
        response = httpx.post(
            f"{base_url}/api/chat",
            json=payload,
            timeout=httpx.Timeout(self._int_config("ai_timeout_seconds", 120, 10, 600), connect=10.0),
        )
        response.raise_for_status()
        message = response.json().get("message", {}) or {}
        # 隔离「正在思考」内容：优先使用原生 `thinking` 字段；若没有，
        # 则提取正文中内联的 <think> 标签（与具体模型无关的回退方案）。
        from .chat_logic import split_thinking
        extracted, content = split_thinking(message.get("content", "") or "")
        thinking = "\n\n".join(part for part in (message.get("thinking"), extracted) if part)
        self.last_thinking = (thinking or "").strip() or None
        return content

    def _call_llamacpp(self, user_message, system_prompt, model, max_tokens: int = 8000, history=None):
        """llama.cpp / llama-server / llama-swap：位于 /v1 的 OpenAI 兼容端点。"""
        base_url = self._get_api_key('llamacpp_url') or "http://localhost:8080"
        base_url = base_url.rstrip('/')
        # 既接受裸主机地址，也接受已经以 /v1 结尾的 URL
        if not base_url.endswith('/v1'):
            base_url = f"{base_url}/v1"

        client = OpenAI(
            base_url=base_url,
            api_key="llamacpp",  # 客户端要求必须提供，但服务器并不使用
            timeout=httpx.Timeout(self._int_config("ai_timeout_seconds", 120, 10, 600), connect=10.0),
        )
        system_prompt = self._apply_no_think(system_prompt)
        nak = self._normalize_history(history)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(nak)
        messages.append({"role": "user", "content": user_message})
        kwargs = dict(
            model=model or "default",
            messages=messages,
        )
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    # ---------------------------------------------------------------------
    # 流式输出（Streaming）
    # ---------------------------------------------------------------------
    def stream_response(
        self,
        user_message: str,
        system_prompt: str,
        mode: str,
        conversation_summary: str = "",
        max_tokens: int = None,
        provider: str = None,
        model: str = None,
        history: list = None,
    ):
        """
        生成器，分块（token/delta）产出 LLM 回复。
        对 OpenAI 兼容提供商和 Anthropic 使用增量流式输出；
        对其他提供商则在单个块中产出完整回复。
        `history`（可选）：带角色标签的历史，以原生消息形式注入。
        `provider`/`model` 可选：强制指定提供商+模型（咨询机器人通过预设指定）。
        """
        if conversation_summary:
            user_message = f"CONTEXT OF PREVIOUS CONVERSATIONS:\n{conversation_summary}\n\n{user_message}"
        self.last_attempts = []
        last_error = None
        for selected_provider, selected_model in self._targets(provider, model):
            content_seen = False
            attempt = {"provider": selected_provider, "model": selected_model, "status": "failed"}
            self.last_attempts.append(attempt)
            try:
                entry, mt, msg, sys, hist, mapping = self._attempt_input(
                    selected_provider, selected_model, user_message, system_prompt, history, max_tokens, True)
                restorer = pii_ner.StreamRestorer(mapping) if mapping else None
                if entry['stream']:
                    output = entry['stream'](msg, sys, selected_model, max_tokens=mt, history=hist)
                else:
                    output = [entry['call'](msg, sys, selected_model, max_tokens=mt, history=hist)]
                for item in output:
                    if not item:
                        continue
                    item = item if isinstance(item, dict) else {"type": "content", "text": item}
                    if item.get("text"):
                        content_seen = content_seen or item.get("type") == "content"
                        if restorer and item.get("type") == "content":
                            item = {**item, "text": restorer.feed(item["text"])}
                        elif mapping:
                            item = {**item, "text": pii.restore(item["text"], mapping)}
                    yield item
                if not content_seen:
                    raise AIError("Il modello ha terminato senza contenuto")
                if restorer:
                    tail = restorer.flush()
                    if tail:
                        yield {"type": "content", "text": tail}
                attempt["status"] = "succeeded"
                return
            except Exception as exc:
                last_error = exc
                attempt["error_type"] = type(exc).__name__
                if self.personal_target:
                    raise self._personal_failure(exc) from None
                if self.chatgpt_model and isinstance(exc, AIError):
                    raise exc
                # 只有已经送达学生的正文内容才会阻止回退。推理内容走
                # 独立通道，不会进入回复，也不会被保存：一个只输出了
                # <think> 就中断的模型，可以被备用模型替换，
                # 而不会重复任何可见内容。
                if content_seen:
                    raise AIError("La risposta e stata interrotta dal modello; riprova il turno.") from exc
                logger.warning("AI stream %s/%s failed before output (%s)", selected_provider, selected_model, type(exc).__name__)
        raise AIError(f"Nessun modello configurato ha completato la risposta ({type(last_error).__name__}).") from last_error

    def _iter_chat_stream(self, client, model, system_prompt, user_message, extra_body=None, max_tokens: int = None, stream_options=None, history=None):
        """迭代 OpenAI 兼容客户端的流，逐个产出文本增量（delta）。"""
        nak = self._normalize_history(history)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(nak)
        messages.append({"role": "user", "content": user_message})
        kwargs = dict(
            model=model,
            messages=messages,
            stream=True,
        )
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if extra_body:
            kwargs["extra_body"] = extra_body
        if stream_options:
            kwargs["stream_options"] = stream_options
        stream = client.chat.completions.create(**kwargs)
        finish_reason = None
        try:
            for chunk in stream:
                if isinstance(getattr(chunk, "model", None), str):
                    self.last_model = chunk.model
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    usage_dict = self._usage_to_dict(usage)
                    self.last_usage = usage_dict
                    yield {"type": "usage", "usage": usage_dict}
                try:
                    delta = chunk.choices[0].delta
                    finish_reason = getattr(chunk.choices[0], "finish_reason", None) or finish_reason
                except (IndexError, AttributeError):
                    continue
                # 推理 / thinking：OpenRouter 使用 `reasoning` 字段，其他提供商使用 `reasoning_content`。
                reasoning = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield {"type": "reasoning", "text": reasoning}
                content = getattr(delta, "content", None)
                if content:
                    yield {"type": "content", "text": content}
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        if finish_reason == "length":
            raise AIError("The model reached its output token limit before completing the response.")

    def _stream_chatgpt(self, user_message, system_prompt, model, max_tokens=None, history=None):
        from .chatgpt_connections import ChatGPTError
        from .chatgpt_responses import stream
        from .chatgpt_i18n import error_message
        try:
            for item in stream(self.db, model, user_message, system_prompt, self._normalize_history(history),
                               timeout=self._int_config("ai_timeout_seconds", 120, 10, 600), effort=self.personal_reasoning):
                if isinstance(item, dict) and item.get("type") == "usage":
                    self.last_usage = item["usage"]
                yield item
        except ChatGPTError as exc:
            language = getattr(self.db, "info", {}).get("chatgpt_language", "it")
            raise AIError(error_message(exc.code, language)) from None

    def _call_chatgpt(self, user_message, system_prompt, model, max_tokens=None, history=None):
        return "".join(item for item in self._stream_chatgpt(user_message, system_prompt, model, history=history)
                       if isinstance(item, str))

    def _stream_openai(self, user_message, system_prompt, model, max_tokens: int = None, history=None):
        api_key = self._get_api_key('api_key_openai')
        if not api_key:
            raise AIError("OpenAI API Key non configurata")
        client = OpenAI(api_key=api_key, timeout=self._int_config("ai_timeout_seconds", 120, 10, 600), max_retries=0)
        yield from self._iter_chat_stream(client, model, system_prompt, user_message, max_tokens=max_tokens, history=history)

    def _stream_openrouter(self, user_message, system_prompt, model, max_tokens: int = None, history=None):
        api_key = self._get_api_key('api_key_openrouter')
        if not api_key:
            raise AIError("OpenRouter API Key non configurata")
        client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key, timeout=self._int_config("ai_timeout_seconds", 120, 10, 600), max_retries=0)
        # 有上限的推理：预算与元数据分开，回复的预留空间
        # 已包含在 max_tokens 中（见 reasoning_profiles）。
        extra = {"reasoning": self._openrouter_reasoning()}
        yield from self._iter_chat_stream(
            client,
            model,
            system_prompt,
            user_message,
            extra_body=extra,
            max_tokens=max_tokens,
            stream_options={"include_usage": True},
            history=history,
        )

    def _stream_ollama(self, user_message, system_prompt, model, max_tokens: int = None, history=None):
        base_url = (self._get_api_key('ollama_ip') or "http://localhost:11434").rstrip('/')
        system_prompt = self._apply_ollama_thinking_directive(system_prompt)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self._normalize_history(history))
        messages.append({"role": "user", "content": user_message})
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.ollama_keep_alive,
            "options": {
                "num_ctx": context_profile(self.config, "ollama", model).get("context_tokens") or self.ollama_num_ctx,
                "num_predict": max_tokens or 4096,
            },
        }
        is_json = _requests_json_response(system_prompt, user_message)
        if is_json:
            payload["think"] = False
            payload["format"] = "json"
        else:
            payload["think"] = (not self.disable_thinking) and self._plan().enabled
        from .chat_logic import ThinkStreamSplitter
        splitter = ThinkStreamSplitter()
        reasoning_acc: list[str] = []
        done_reason = None
        self.last_thinking = None
        with httpx.Client(timeout=httpx.Timeout(self._int_config("ai_timeout_seconds", 120, 10, 600), connect=10.0)) as client:
            with client.stream("POST", f"{base_url}/api/chat", json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    done_reason = event.get("done_reason") or done_reason
                    if event.get("error"):
                        raise RuntimeError(event["error"])
                    msg = event.get("message", {})
                    thinking = msg.get("thinking")
                    if thinking:
                        reasoning_acc.append(thinking)
                        yield {"type": "reasoning", "text": thinking}
                    content = msg.get("content")
                    if content:
                        # 标签回退：对把 <think> 内联在正文里的模型在此进行分离，
                        # 这样即使没有原生字段，可见文本也能保持干净。
                        for item in splitter.feed(content):
                            if item["type"] == "reasoning":
                                reasoning_acc.append(item["text"])
                            yield item
                for item in splitter.flush():
                    if item["type"] == "reasoning":
                        reasoning_acc.append(item["text"])
                    yield item
        self.last_thinking = "".join(reasoning_acc).strip() or None
        if done_reason == "length":
            raise AIError("The model reached its output token limit before completing the response.")

    def _stream_llamacpp(self, user_message, system_prompt, model, max_tokens: int = None, history=None):
        base_url = (self._get_api_key('llamacpp_url') or "http://localhost:8080").rstrip('/')
        if not base_url.endswith('/v1'):
            base_url = f"{base_url}/v1"
        client = OpenAI(base_url=base_url, api_key="llamacpp", timeout=httpx.Timeout(self._int_config("ai_timeout_seconds", 120, 10, 600), connect=10.0))
        system_prompt = self._apply_no_think(system_prompt)
        yield from self._iter_chat_stream(client, model or "default", system_prompt, user_message, max_tokens=max_tokens, history=history)

    def _stream_openai_compatible(self, provider, user_message, system_prompt, model, max_tokens: int = None, history=None):
        client = self._openai_compatible_client(provider)
        # include_usage：大多数 OpenAI 兼容提供商会在最后一个块中发送
        # token 用量（但不含费用：只有 OpenRouter 会返回费用）。
        yield from self._iter_chat_stream(
            client,
            model,
            system_prompt,
            user_message,
            max_tokens=max_tokens,
            stream_options={"include_usage": True},
            history=history,
        )

    def _stream_anthropic(self, user_message, system_prompt, model, max_tokens: int = 4096, history=None):
        api_key = self._get_api_key('api_key_anthropic')
        if not api_key:
            raise AIError("Anthropic API Key non configurata")
        client = anthropic.Anthropic(api_key=api_key, timeout=self._int_config("ai_timeout_seconds", 120, 10, 600), max_retries=0)
        nak = self._normalize_history(history)
        messages = list(nak)
        messages.append({"role": "user", "content": user_message})
        kwargs = dict(
            model=model,
            system=system_prompt,
            messages=messages,
            max_tokens=max_tokens,
        )
        thinking = self._anthropic_thinking(max_tokens)
        if thinking:
            kwargs["thinking"] = thinking
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                if text:
                    yield text

    # ---------------------------------------------------------------------
    # 嵌入向量（本地，通过 Ollama）—— 用于网站聊天机器人的 RAG。
    # ---------------------------------------------------------------------
    def embed_texts(self, texts, model: str = None):
        """返回 `texts`（字符串列表）对应的嵌入向量列表。

        使用 Ollama 的 /api/embed 端点（批量）。默认模型是
        `embedding_model`（bge-m3）。失败时抛出 AIError，与本服务的
        其余部分保持一致（绝不把错误字符串当作结果返回）。"""
        if not texts:
            return []
        base_url = (self._get_api_key('ollama_ip') or "http://localhost:11434").rstrip('/')
        model = model or self.embedding_model
        if not model:
            raise AIError("Modello di embedding non configurato (embedding_model)")

        vectors: list[list[float]] = []
        # 分批发送内容，以免超出服务器的请求体大小限制。
        BATCH = 32
        try:
            with httpx.Client(timeout=httpx.Timeout(self._int_config("ai_timeout_seconds", 120, 10, 600), connect=10.0)) as client:
                for start in range(0, len(texts), BATCH):
                    batch = texts[start:start + BATCH]
                    resp = client.post(
                        f"{base_url}/api/embed",
                        json={"model": model, "input": batch, "keep_alive": self.ollama_keep_alive},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    embeddings = data.get("embeddings")
                    if not embeddings or len(embeddings) != len(batch):
                        raise AIError(
                            f"Risposta embeddings inattesa da Ollama (modello '{model}'): "
                            f"attesi {len(batch)} vettori, ricevuti {len(embeddings or [])}"
                        )
                    vectors.extend(embeddings)
        except AIError:
            raise
        except Exception as e:
            raise AIError(f"Errore embeddings Ollama (modello '{model}'): {e}") from e
        return vectors

    def embed_query(self, text: str, model: str = None):
        """对单个字符串做嵌入（便捷方法）。返回一个向量。"""
        vecs = self.embed_texts([text], model=model)
        return vecs[0] if vecs else []

    def preload_ollama_model(self):
        """可选地预加载 Ollama 模型，并限制其上下文长度和驻留时长。"""
        import urllib.request, json as _json
        base_url = self._get_api_key('ollama_ip') or "http://localhost:11434"
        model = self.config.get('model_name', '')
        if not model:
            return
        try:
            payload = _json.dumps({
                "model": model,
                "keep_alive": self.ollama_keep_alive,
                "messages": [{"role": "user", "content": "ok"}],
                "stream": False,
                "options": {"num_ctx": context_profile(self.config, "ollama", model).get("context_tokens") or self.ollama_num_ctx, "num_predict": 1},
            }).encode()
            req = urllib.request.Request(
                f"{base_url}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=600) as r:
                r.read()
            logger.info(
                f"Ollama model '{model}' preloaded with keep_alive={self.ollama_keep_alive} "
                f"num_ctx={self.ollama_num_ctx}"
            )
        except Exception as e:
            logger.warning(f"Ollama preload fallito (non bloccante): {e}")
