"""Qoder Flash Gateway —— 把本机 Qoder 登录态变成一个 OpenAI / DeepSeek 兼容接口，只跑 Qwen3.8-Flash。

启动：
    python main.py                        # 默认 127.0.0.1:5050
    python main.py --port 8080            # 换端口
    QODER_API_KEY=sk-xxx python main.py   # 设了就要带 Bearer 才能调

接口（DeepSeek 与 OpenAI 两种习惯都照顾到了）：
    POST /chat/completions      DeepSeek 官方 base_url 形式
    POST /v1/chat/completions   OpenAI SDK 形式
    GET  /models, /v1/models
    GET  /health
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from qoder import (
    MODEL_DISPLAY,
    MODEL_NAME,
    Credentials,
    QoderError,
    complete_chat,
    credential_summary,
    load_credentials,
    stream_chat,
)

app = FastAPI(title="Qoder Flash Gateway", version="0.1.0")

API_KEY = os.getenv("QODER_API_KEY", "").strip()

# 对外报的模型名。本网关背后只有一个模型（Qwen3.8-Flash），这些名字都指向它，
# 请求里写哪个就回显哪个，方便把 DeepSeek/OpenAI 客户端直接指过来。
MODELS = [MODEL_NAME]
SYSTEM_FINGERPRINT = "qoder-flash-gateway"

# Qoder 侧的思考强度档位（DeepSeek 的 low/medium/high 都落在这个集合里）
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


def resolve_effort(payload: dict[str, Any]) -> str:
    """把 DeepSeek 风格的思考参数翻成 Qoder 的 reasoning_effort。

    - `reasoning_effort`: 直接给档位，照用（`none` 即关思考）
    - `thinking: {"type": "enabled"}`: 只开不指定强度时给 medium
    - 两者都没给：默认 none（关思考最快；开了要多等十几秒到几十秒）
    """
    raw = str(payload.get("reasoning_effort") or "").strip().lower()
    if raw in EFFORTS:
        return raw
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and str(thinking.get("type") or "").strip().lower() == "enabled":
        return "medium"
    return "none"


def check_api_key(authorization: str | None) -> None:
    if not API_KEY:
        return
    token = authorization[len("Bearer "):].strip() if authorization and authorization.startswith("Bearer ") else ""
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def get_credentials() -> Credentials:
    try:
        return load_credentials()
    except QoderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def usage_payload() -> dict[str, int]:
    """DeepSeek 的 usage 多两个缓存计数字段；老版协议拿不到计数，一律给 0。"""
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        cred = load_credentials()
    except QoderError as exc:
        return {"ready": False, "error": str(exc)}
    return {"ready": True, "model": MODEL_NAME, "models": MODELS, "credentials": credential_summary(cred)}


@app.get("/models")
@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": name, "object": "model", "created": 0, "owned_by": "qoder"} for name in MODELS],
    }


def _chunk(completion_id: str, created: int, model: str, delta: dict[str, Any] | None = None, finish_reason: str | None = None, usage: dict[str, int] | None = None, choices: list[Any] | None = None) -> str:
    payload: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "system_fingerprint": SYSTEM_FINGERPRINT,
        "choices": choices if choices is not None else [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any], authorization: str | None = Header(default=None)):
    check_api_key(authorization)
    cred = get_credentials()

    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else None
    effort = resolve_effort(payload)
    model = str(payload.get("model") or MODELS[0])  # 只用于回显，背后永远是 Flash
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    if not payload.get("stream"):
        try:
            message = await complete_chat(messages, tools, cred, reasoning_effort=effort)
        except QoderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "system_fingerprint": SYSTEM_FINGERPRINT,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                }
            ],
            "usage": usage_payload(),
        }

    include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))

    async def event_stream() -> AsyncIterator[str]:
        emitted_role = False
        try:
            async for delta in stream_chat(messages, tools, cred, reasoning_effort=effort):
                out: dict[str, Any] = {}
                if not emitted_role:
                    out["role"] = "assistant"
                    emitted_role = True
                if delta["reasoning"]:
                    out["reasoning_content"] = delta["reasoning"]  # DeepSeek 的思考字段
                if delta["content"]:
                    out["content"] = delta["content"]
                if delta["tool_calls"]:
                    out["tool_calls"] = delta["tool_calls"]
                if out or delta["finish_reason"]:
                    yield _chunk(completion_id, created, model, out, delta["finish_reason"])
            yield _chunk(completion_id, created, model, {}, "stop")
            if include_usage:
                # DeepSeek/OpenAI 的约定：usage 单独一个 chunk，choices 为空
                yield _chunk(completion_id, created, model, usage=usage_payload(), choices=[])
        except QoderError as exc:
            yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Qoder Flash Gateway")
    parser.add_argument("--host", default=os.getenv("QODER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("QODER_PORT", "5050")))
    args = parser.parse_args()

    print(f"Qoder Flash Gateway -> http://{args.host}:{args.port}  (背后模型: {MODEL_DISPLAY} / {MODEL_NAME})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
