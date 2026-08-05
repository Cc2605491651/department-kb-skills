#!/usr/bin/env python3
"""LLM 提供方分发层：codex CLI 或 OpenAI 兼容 API（硅基流动/Kimi）。

需要大模型的脚本统一调 run_llm_structured()，由 provider 决定走 codex 订阅额度
还是第三方 API key：
- provider 为空或 "codex"：原样转发 kb_common.run_codex_structured（codex CLI 子进程）。
- provider 为 "siliconflow"/"kimi"：openai SDK 直连 OpenAI 兼容端点，
  response_format=json_object + schema 注入 system prompt。

API key 只从环境变量读取，永不落盘、不进台账、不进 job 目录。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as common


OPENAI_COMPATIBLE = {
    # 注意：provider "deepseek" 指 DeepSeek 官方 API（api.deepseek.com），
    # 与硅基流动平台上的 deepseek-ai/DeepSeek-V4-Flash（provider "siliconflow"）是不同计费渠道
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-v4-flash",
        "temperature": 0.1,
        "default_api_key_env": "DEEPSEEK_API_KEY",
        "note": "DeepSeek 官方 API（api.deepseek.com），模型名 deepseek-v4-flash；区别于硅基流动的 deepseek-ai/DeepSeek-V4-Flash",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "deepseek-ai/DeepSeek-V4-Flash",
        "temperature": 0.1,
        "default_api_key_env": "SILICONFLOW_API_KEY",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "kimi-k2.6",
        # k2 系列服务端仅允许 temperature=1，传其它值直接报错
        "temperature": 1,
        "default_api_key_env": "KIMI_API_KEY",
    },
}

DEFAULT_MAX_TOKENS = 32000
BACKEND_TAG = "openai-compatible"


def is_codex(provider: str) -> bool:
    return provider in ("", "codex")


def default_model(provider: str) -> str:
    if is_codex(provider):
        return ""
    return OPENAI_COMPATIBLE.get(provider, {}).get("default_model", "")


def default_key_env(provider: str) -> str:
    return OPENAI_COMPATIBLE.get(provider, {}).get("default_api_key_env", "")


def resolve(provider: str, model: str, api_key_env: str) -> tuple[str, str, str]:
    """返回 (provider, 实际模型名, api_key)。api_key 仅第三方需要，从环境变量读取。"""
    provider = provider or "codex"
    resolved_model = model or default_model(provider)
    key_env = api_key_env or default_key_env(provider)
    api_key = os.environ.get(key_env, "") if not is_codex(provider) else ""
    return provider, resolved_model, api_key


def _extract_usage(response: Any) -> dict:
    usage = getattr(response, "usage", None) or {}
    details = getattr(usage, "prompt_tokens_details", None) or {}
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(details, "cached_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _call_openai_compatible(
    client: Any, resolved_model: str, schema_text: str, prompt: str,
    temperature: float, timeout: int,
) -> tuple[dict, dict]:
    """单次第三方 API 调用，带指数退避重试；返回 (payload, usage)。"""
    max_retries = 3
    last_error: BaseException | None = None
    for retry in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=resolved_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是一个知识库蒸馏系统。你必须返回严格匹配以下 JSON Schema 的有效 JSON。"
                            "只输出 JSON，不要有任何解释、markdown 代码块标记或额外文字。\n\n"
                            f"Schema:\n{schema_text}"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=DEFAULT_MAX_TOKENS,
                timeout=timeout,
            )
            if not response or not response.choices:
                raise RuntimeError(f"{resolved_model} 返回空响应")
            content = response.choices[0].message.content or ""
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                content = "\n".join(lines)
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise RuntimeError(f"{resolved_model} 返回非 dict：{type(payload).__name__}")
            return payload, _extract_usage(response)
        except json.JSONDecodeError as error:
            last_error = error
            if retry < max_retries - 1:
                print(f"LLM_JSON_RETRY attempt={retry + 1} error={error}", flush=True)
                time.sleep(2 ** retry)
            else:
                raise
        except Exception as error:
            last_error = error
            message = str(error)[:200]
            if retry < max_retries - 1 and any(
                marker in message.lower()
                for marker in ("rate", "timeout", "overloaded", "empty response", "429", "503")
            ):
                print(f"LLM_RETRY attempt={retry + 1} error={message}", flush=True)
                time.sleep(2 ** retry)
            else:
                raise
    raise last_error  # type: ignore[misc]


def run_llm_structured(
    *,
    prompt: str,
    schema_path: Path,
    cwd: Path,
    provider: str = "",
    model: str = "",
    api_key_env: str = "",
    timeout: int = 1200,
    attempts: int = 2,
) -> tuple[dict, dict]:
    """统一 LLM 结构化调用入口。返回 (payload, metadata)；
    metadata 含 provider、实际模型名、usage（输入/输出/缓存分开）、attempts。"""
    provider, resolved_model, api_key = resolve(provider, model, api_key_env)
    started = common.now_iso()

    if is_codex(provider):
        return common.run_codex_structured(
            prompt=prompt, schema_path=schema_path, cwd=cwd,
            model=resolved_model, timeout=timeout, attempts=attempts,
        )

    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError("openai 包未安装，无法调用第三方 API；请先 pip install openai") from error
    if not api_key:
        raise RuntimeError(f"缺少 API key：请先设置环境变量 {api_key_env or 'LLM_API_KEY'}")

    config = OPENAI_COMPATIBLE.get(provider)
    if not config:
        raise RuntimeError(f"未知 LLM 提供方：{provider}")

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_text = json.dumps(schema, ensure_ascii=False, indent=2)
    client = OpenAI(api_key=api_key, base_url=config["base_url"], timeout=float(timeout))

    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            payload, usage = _call_openai_compatible(
                client, resolved_model, schema_text, prompt,
                config["temperature"], timeout,
            )
            metadata = {
                "attempts": attempt,
                "started_at": started,
                "finished_at": common.now_iso(),
                "codex_cli_version": f"{BACKEND_TAG}:{provider}",
                "model": resolved_model,
                "provider": provider,
                "usage": usage,
                "event_chars": 0,
            }
            return payload, metadata
        except Exception as error:
            last_error = error
    raise RuntimeError(f"第三方 LLM 结构化任务失败：{common.safe_error(last_error or RuntimeError('unknown'))}")


def backend_tag(provider: str) -> str:
    """台账用后端标识：codex 返回 codex CLI 版本，第三方返回 openai-compatible:<provider>。"""
    if is_codex(provider):
        return common.codex_version()
    return f"{BACKEND_TAG}:{provider}"
