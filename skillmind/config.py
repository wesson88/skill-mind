"""全局配置与路径管理"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# 默认目录
# ---------------------------------------------------------------------------

SKILLMIND_HOME = Path(os.environ.get("SKILLMIND_HOME", Path.home() / ".skillmind"))
CACHE_DIR = SKILLMIND_HOME / "cache"
REPOS_DIR = CACHE_DIR / "repos"
RAW_DIR = CACHE_DIR / "raw"
DRAFTS_DIR = SKILLMIND_HOME / "drafts"
VAULT_DIR = SKILLMIND_HOME / "vault"
VAULT_DRAFTS_DIR = VAULT_DIR / "_drafts"
VAULT_PUBLISHED_DIR = VAULT_DIR / "skills"
PROMPTS_DIR = SKILLMIND_HOME / "prompts"
HASHES_FILE = SKILLMIND_HOME / "hashes.yaml"
CONFIG_FILE = SKILLMIND_HOME / "config.yaml"

# 提取缓存：同一 source_hash + prompt_version → 直接复用
EXTRACT_CACHE_DIR = CACHE_DIR / "extract_cache"

CURRENT_PROMPT_VERSION = "extract_v4"

# ---------------------------------------------------------------------------
# 确保目录存在
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    for d in [
        CACHE_DIR, REPOS_DIR, RAW_DIR,
        DRAFTS_DIR,
        VAULT_DIR, VAULT_DRAFTS_DIR, VAULT_PUBLISHED_DIR,
        PROMPTS_DIR,
        EXTRACT_CACHE_DIR,
    ]:
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 配置文件加载/保存
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict = {
    "llm": {
        # auth_mode: "api_key" | "claude_code_max"
        "auth_mode": "api_key",
        "model": "anthropic/claude-3-5-haiku-20241022",
        "api_key": "",       # 直接填写 Key（优先级最高）
        "api_key_env": "",   # 从环境变量读取 Key 的变量名
        "api_base": "",      # 自定义 API 地址（可选）
        "qpm": 10,
        # 单次 LLM 调用的最大原文字符预算（含 <<CHUNK N>> 标记）
        # v2 起从 6000 提到 30000，支持 Claude 200K / GPT-4 128K 等长上下文模型
        "max_content_chars": 30000,
    },
    # chunker 配置：extractor 把原文交给 LLM 前的语义切分参数
    "chunker": {
        "chunk_size_tokens": 1500,
        "chunk_overlap_tokens": 150,
        "chars_per_token": 3,        # 混合中英文取 3；纯英文文档可改 4
        "min_chunk_size_tokens": 100,
    },
    # 多 provider Key 存储区，格式: { "anthropic": "sk-ant-xxx", "openai": "sk-xxx", ... }
    "api_keys": {},
    "vault_dir": str(VAULT_DIR),
    "auto_approve": False,
}

# provider 前缀 → 对应的默认环境变量名
_PROVIDER_ENV_MAP: dict[str, str] = {
    "anthropic":  "ANTHROPIC_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "gemini":     "GEMINI_API_KEY",
    "google":     "GOOGLE_API_KEY",
    "deepseek":   "DEEPSEEK_API_KEY",
    "groq":       "GROQ_API_KEY",
    "mistral":    "MISTRAL_API_KEY",
    "cohere":     "COHERE_API_KEY",
    "together":   "TOGETHER_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "azure":      "AZURE_API_KEY",
    "bedrock":    "AWS_ACCESS_KEY_ID",
}


def load_config() -> dict:
    ensure_dirs()
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                return data
            # 文件内容不是 dict（损坏或格式错误），使用默认值
        except Exception:
            pass
    save_config(_DEFAULT_CONFIG)
    return _DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    """原子写 config.yaml，防止崩溃损坏。"""
    ensure_dirs()
    tmp = CONFIG_FILE.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True)
    tmp.replace(CONFIG_FILE)


def get_vault_dir(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    return Path(cfg.get("vault_dir", VAULT_DIR))


# ---------------------------------------------------------------------------
# LLM 认证解析
# ---------------------------------------------------------------------------

def resolve_llm_credentials(cfg: dict | None = None) -> dict:
    """
    解析 LLM 认证信息，返回可直接传给 litellm 的参数字典。

    Key 查找优先级（api_key 模式）：
      1. llm.api_key（配置文件直接写入）
      2. llm.api_key_env 指定的环境变量
      3. api_keys.<provider> 多 provider 存储区
      4. 根据 model 前缀自动匹配对应环境变量（如 ANTHROPIC_API_KEY）
      5. 依次尝试所有已知 provider 的环境变量
    """
    cfg = cfg or load_config()
    llm_cfg = cfg.get("llm", {})
    auth_mode = llm_cfg.get("auth_mode", "api_key")
    model = llm_cfg.get("model", "anthropic/claude-3-5-haiku-20241022")

    result: dict = {"model": model, "qpm": llm_cfg.get("qpm", 10)}

    api_base = llm_cfg.get("api_base", "").strip()
    if api_base:
        result["api_base"] = api_base

    if auth_mode == "claude_code_max":
        token = _get_claude_code_token()
        if not token:
            raise RuntimeError(
                "未能从 Claude Code 读取认证 Token。\n"
                "请确认：\n"
                "  1. 已安装并登录 Claude Code（claude login）\n"
                "  2. 订阅了 Claude Code Max 或 Pro 计划\n"
                "  3. 在终端执行过 `claude` 命令至少一次"
            )
        result["api_key"] = token
        # Claude Code Max 默认走 Anthropic API，但允许用户通过 api_base 覆盖
        anthropic_base = llm_cfg.get("claude_code_api_base", "https://api.anthropic.com")
        if not api_base:
            result["api_base"] = anthropic_base
        if "/" not in model:
            result["model"] = f"anthropic/{model}"
        return result

    # --- api_key 模式 ---
    api_key = ""

    # 1. 配置文件直接写入的 key
    api_key = llm_cfg.get("api_key", "").strip()

    # 2. api_key_env 指定的环境变量
    if not api_key:
        env_name = llm_cfg.get("api_key_env", "").strip()
        if env_name:
            api_key = os.environ.get(env_name, "").strip()

    # 3. api_keys.<provider> 多 provider 存储区
    if not api_key:
        provider = _provider_from_model(model)
        stored_keys: dict = cfg.get("api_keys", {})
        api_key = stored_keys.get(provider, "").strip()

    # 4. 根据 model 前缀自动匹配对应环境变量
    if not api_key:
        provider = _provider_from_model(model)
        env_var = _PROVIDER_ENV_MAP.get(provider, "")
        if env_var:
            api_key = os.environ.get(env_var, "").strip()

    # 5. 遍历所有已知环境变量兜底
    if not api_key:
        for env_var in _PROVIDER_ENV_MAP.values():
            api_key = os.environ.get(env_var, "").strip()
            if api_key:
                break

    if not api_key:
        raise RuntimeError(
            "未配置 LLM API Key。请运行以下任一命令：\n\n"
            "  # 使用 Anthropic（Claude）\n"
            "  skillmind config --provider anthropic --api-key sk-ant-xxx\n\n"
            "  # 使用 OpenAI\n"
            "  skillmind config --provider openai --api-key sk-xxx\n\n"
            "  # 使用 DeepSeek\n"
            "  skillmind config --provider deepseek --api-key sk-xxx --model deepseek/deepseek-chat\n\n"
            "  # 使用 Claude Code Max 订阅（免 Key）\n"
            "  skillmind config --auth claude_code_max"
        )

    result["api_key"] = api_key
    return result


def _provider_from_model(model: str) -> str:
    """从 model 字符串提取 provider 前缀，如 'anthropic/claude-xxx' → 'anthropic'"""
    if "/" in model:
        return model.split("/")[0].lower()
    # 无前缀时按模型名猜测
    model_lower = model.lower()
    if "claude" in model_lower:
        return "anthropic"
    if "gpt" in model_lower or "o1" in model_lower or "o3" in model_lower:
        return "openai"
    if "gemini" in model_lower:
        return "gemini"
    if "deepseek" in model_lower:
        return "deepseek"
    if "llama" in model_lower or "mixtral" in model_lower:
        return "groq"
    return "openai"


def _get_claude_code_token() -> str:
    """
    从 Claude Code 的本地凭证文件中读取 OAuth access token。

    各平台凭证路径：
      - Windows: %APPDATA%\\claude\\.credentials.json 或 ~/.claude/.credentials.json
      - macOS/Linux: ~/.claude/.credentials.json
    """
    import json

    # 候选路径列表（优先级从高到低）
    candidates = [Path.home() / ".claude" / ".credentials.json"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.insert(0, Path(appdata) / "claude" / ".credentials.json")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            candidates.insert(0, Path(localappdata) / "claude" / ".credentials.json")

    for cred_file in candidates:
        if cred_file.exists():
            try:
                data = json.loads(cred_file.read_text(encoding="utf-8"))
                token = (
                    data.get("claudeAiOauth", {}).get("accessToken")
                    or data.get("access_token")
                    or data.get("token")
                )
                if token:
                    return token
            except Exception:
                continue

    # 备用：尝试通过 claude CLI 命令获取（部分版本支持）
    try:
        # creationflags 只在 Windows 下传入，非 Windows 不支持该参数
        popen_kwargs: dict = {
            "capture_output": True,
            "text": True,
            "timeout": 5,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        result = subprocess.run(["claude", "auth", "token"], **popen_kwargs)
        token = result.stdout.strip()
        if token and len(token) > 20:
            return token
    except Exception:
        pass

    return ""
