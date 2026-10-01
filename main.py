"""Qoder Gateway —— 把本机 Qoder 登录态变成一个 OpenAI / DeepSeek 兼容接口，可选模型。

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
import secrets
import threading
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from qoder import (
    DEFAULT_MODEL,
    DEFAULT_REGION,
    REGION_MODELS,
    Credentials,
    QoderError,
    complete_chat,
    credential_summary,
    load_credentials,
    resolve_model_key,
    resolve_region,
    stream_chat,
)

app = FastAPI(title="Qoder Flash Gateway", version="0.1.0")

CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config() -> dict[str, Any]:
    """配置文件 + 环境变量；环境变量优先。

    config.json（与 main.py 同目录，建议 600，已在 .gitignore 里）：
        {"api_key": "qf-...", "region": "cn", "host": "127.0.0.1", "port": 5050}
    """
    config: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"config.json 解析失败：{exc}")
        if not isinstance(config, dict):
            raise SystemExit("config.json 顶层必须是一个对象")
    for env_name, field in (("QODER_API_KEY", "api_key"), ("QODER_HOST", "host"),
                            ("QODER_PORT", "port"), ("QODER_REGION", "region")):
        value = os.getenv(env_name)
        if value:
            config[field] = value
    return config


CONFIG = load_config()
API_KEY = str(CONFIG.get("api_key") or "").strip()


def generate_key() -> str:
    return "qf-" + secrets.token_urlsafe(24)


def write_api_key(key: str) -> None:
    data = {}
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    data["api_key"] = key
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CONFIG_PATH.chmod(0o600)

# 对外报的模型名：跟随 region 决定（国际版免费号只有 2 个，CN 号有一整套）。
REGION = str(CONFIG.get("region") or DEFAULT_REGION).strip().lower()
MODELS = REGION_MODELS.get(REGION, [DEFAULT_MODEL])
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


def authorized(authorization: str | None) -> bool:
    if not API_KEY:
        return True
    token = authorization[len("Bearer "):].strip() if authorization and authorization.startswith("Bearer ") else ""
    return secrets.compare_digest(token, API_KEY)


def check_api_key(authorization: str | None) -> None:
    if not authorized(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")


def get_credentials() -> Credentials:
    try:
        return load_credentials(region=REGION)
    except QoderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ★ 用量台账：每完成一次调用追加一行 JSONL，并把累计值给 /stats。
#   为什么值得记：上游**每一块都带真实计数**（含 cached_tokens 与 credits），
#   老版本全丢掉了（2026-10-02 之前 usage 恒为 0）。记下来才能回答
#   「这一局到底花了多少 token / 多少 credits」这种问题。
LEDGER_PATH = Path(os.getenv("QODER_LEDGER") or (Path(__file__).with_name("usage-ledger.jsonl")))

# 进程内累计（不读盘，给 /stats 的 session 段）。uvicorn 单进程跑，锁只是防并发请求。
_USAGE_LOCK = threading.Lock()
_USAGE: dict[str, dict[str, Any]] = {}


def _accumulate(model: str, rec: dict[str, Any]) -> None:
    with _USAGE_LOCK:
        slot = _USAGE.setdefault(str(model or "?"), {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0,
            "credits": 0.0, "errors": 0})
        slot["calls"] += 1
        if rec.get("error"):
            slot["errors"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                  "cached_tokens", "reasoning_tokens"):
            try:
                slot[k] += int(rec.get(k) or 0)
            except (TypeError, ValueError):
                pass
        try:
            slot["credits"] += float(rec.get("credits") or 0.0)
        except (TypeError, ValueError):
            pass


def usage_payload(prompt: int = 0, completion: int = 0, hit: int = 0, miss: int | None = None,
                  credits: float | None = None, reasoning: int | None = None) -> dict[str, Any]:
    """把上游真实计数翻成 DeepSeek 拼法的 usage（客户端认这一套）。

    - `prompt_tokens` 是**输入总量**，`prompt_cache_hit_tokens` 是其中命中前缀缓存的
      那一部分，所以 `miss = prompt - hit`。客户端就是这么反推 miss 的。
    - 命中的那部分**单价便宜得多**（实测 11.6k 前缀：冷 0.3225 credits、全命中 0.0260，
      差 12.4 倍），所以 hit 必须如实报，不能省。
    - `reasoning`（思考 token）走 `completion_tokens_details.reasoning_tokens`，
      与 `prompt_tokens_details.cached_tokens` 是**兄弟字段**。★ 曾经漏读这个兄弟，
      结果客户端只拿到 `completion_tokens=132` 却看到"思考 0"——比不报还坑
      （实测上游确实给：132 输出里 128 是思考）。给了才写，没给就不写这个键，
      免得用一个假的 0 盖掉客户端自己的估算。
    - miss 允许显式给（上游给了就用它的），否则按 prompt - hit 算。
    """
    prompt = max(0, int(prompt))
    hit = max(0, min(int(hit), prompt))
    miss = max(0, prompt - hit) if miss is None else max(0, int(miss))
    out: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": max(0, int(completion)),
        "total_tokens": prompt + max(0, int(completion)),
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
    }
    if reasoning is not None:
        out["completion_tokens_details"] = {"reasoning_tokens": max(0, int(reasoning))}
    if credits is not None:
        out["credits"] = round(float(credits), 6)
    return out


def _reasoning_tokens(up: dict[str, Any], reasoning_text: str) -> int | None:
    """思考 token：优先上游真数，没有就按思考文本本地估。

    估的时候要**限制在 completion_tokens 以内**——本地估是字符数近似，可能比上游的
    总输出还大，那种数看着就不对（思考不可能超过全部输出）。上游连 completion 都没报
    时才不限制。返回 None 表示"连估都估不出"（没有思考文本），此时调用方不写这个键。
    """
    det = up.get("completion_tokens_details") or {}
    reported = det.get("reasoning_tokens")
    if reported is not None:
        return int(reported)
    if not reasoning_text:
        return None
    est = _est_tokens(reasoning_text)
    cap = int(up.get("completion_tokens") or 0)
    return min(est, cap) if cap else est


def _write_ledger(rec: dict[str, Any]) -> None:
    try:
        with LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:                       # 记账失败不能影响转发
        print(f"[台账] 写入失败（不影响转发）：{exc}", flush=True)


def _read_ledger(limit: int = 200000) -> list[dict[str, Any]]:
    if not LEDGER_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with LEDGER_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError:
        return rows
    return rows[-limit:]


@app.get("/health")
async def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """探活不需要 key；但账号详情（uid/姓名/是否过期）只给带 key 的人看。"""
    try:
        cred = load_credentials(region=REGION)
    except QoderError as exc:
        return {"ready": False, "error": str(exc)} if authorized(authorization) else {"ready": False}
    if not authorized(authorization):
        return {"ready": True}
    return {
        "ready": True,
        "region": REGION,
        "default_model": DEFAULT_MODEL,
        "models": MODELS,
        "credentials": credential_summary(cred),
    }


@app.get("/models")
@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": name, "object": "model", "created": 0, "owned_by": "qoder"} for name in MODELS],
    }


def _est_tokens(text: str) -> int:
    """只在**上游没报**时兜底：CJK 按字、其余按 ~4 字符/token 估。

    宁可标"估"也不装准——但真流里上游一直在报，这条基本走不到（见 chat_completions）。
    """
    s = str(text or "")
    if not s:
        return 0
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    rest = len(s) - cjk
    return cjk + max(0, (rest + 3) // 4)


@app.get("/stats")
async def stats(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """累计用量台账（进程内累计 + 落盘 JSONL 的全量重算）。

    `ledger` 是**全量**（跨重启），`calls`/`totals` 是本次进程启动以来的。
    按模型分组，附命中率与 credits 合计——"这一局花了多少"就看这里。
    """
    check_api_key(authorization)

    def _blank() -> dict[str, Any]:
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "cached_tokens": 0, "reasoning_tokens": 0, "credits": 0.0, "errors": 0}

    def _fold(acc: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
        for rec in rows:
            model = str(rec.get("model") or "?")
            slot = acc.setdefault(model, _blank())
            slot["calls"] += 1
            if rec.get("error"):
                slot["errors"] += 1
            for src, dst in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                             ("total_tokens", "total_tokens"), ("cached_tokens", "cached_tokens"),
                             ("reasoning_tokens", "reasoning_tokens")):
                try:
                    slot[dst] += int(rec.get(src) or 0)
                except (TypeError, ValueError):
                    pass
            try:
                slot["credits"] += float(rec.get("credits") or 0.0)
            except (TypeError, ValueError):
                pass
        return acc

    with _USAGE_LOCK:
        session = {k: dict(v) for k, v in _USAGE.items()}
    ledger = _fold({}, _read_ledger())

    def _finish(acc: dict[str, dict[str, Any]]) -> dict[str, Any]:
        grand = _blank()
        for slot in acc.values():
            for k in grand:
                grand[k] += slot[k]
        grand["credits"] = round(grand["credits"], 6)
        for slot in acc.values():
            slot["credits"] = round(slot["credits"], 6)
            inp = slot["prompt_tokens"]
            slot["cache_hit_rate"] = round(slot["cached_tokens"] / inp, 4) if inp else None
        grand["cache_hit_rate"] = round(grand["cached_tokens"] / grand["prompt_tokens"], 4) if grand["prompt_tokens"] else None
        return {"by_model": acc, "totals": grand}

    return {"ledger_path": str(LEDGER_PATH),
            "session": {"by_model": session, **_finish(session)},
            "ledger": {"by_model": ledger, **_finish(ledger)}}


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
    model = str(payload.get("model") or DEFAULT_MODEL)
    try:
        model_key = resolve_model_key(model, REGION)
    except QoderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    if not payload.get("stream"):
        try:
            message = await complete_chat(
                messages, tools, cred, reasoning_effort=effort, region=REGION, model_key=model_key
            )
        except QoderError as exc:
            _accumulate(model, {"error": 1})
            _write_ledger({"ts": created, "model": model, "stream": False, "error": str(exc)[:200]})
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # ★ `usage` 是 qoder.complete_chat 顺手带回来的上游真数，取走后别留在 message 里
        up = message.pop("usage", None) or {}
        det = up.get("prompt_tokens_details") or {}
        prompt = int(up.get("prompt_tokens") or 0)
        completion = int(up.get("completion_tokens") or 0)
        hit = int(det.get("cached_tokens") or 0)
        if not prompt:                       # 上游没报 ⇒ 估，并如实记 estimated
            prompt = sum(_est_tokens(m.get("content")) for m in messages if isinstance(m, dict))
            for m in messages:
                if isinstance(m, dict):
                    for tc in (m.get("tool_calls") or []):
                        prompt += _est_tokens(((tc.get("function") or {}).get("arguments")))
            prompt += _est_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
        if not completion:
            completion = _est_tokens(message.get("content")) + _est_tokens(message.get("reasoning_content"))
        credits = up.get("credits")
        reason = _reasoning_tokens(up, str(message.get("reasoning_content") or ""))
        rec = {"ts": created, "model": model, "stream": False, "prompt_tokens": prompt,
               "completion_tokens": completion, "cached_tokens": hit,
               "prompt_cache_miss_tokens": max(0, prompt - hit),
               "reasoning_tokens": reason,
               "credits": float(credits) if credits is not None else None,
               "reported": bool(up)}
        _accumulate(model, rec)
        _write_ledger(rec)
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
            "usage": usage_payload(prompt, completion, hit, credits=credits, reasoning=reason),
        }

    include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))

    async def event_stream() -> AsyncIterator[str]:
        emitted_role = False
        up: dict[str, Any] = {}          # 上游累计用量（收尾那条带）
        text_acc: list[str] = []
        reason_acc: list[str] = []
        tool_acc: list[str] = []
        try:
            async for delta in stream_chat(
                messages, tools, cred, reasoning_effort=effort, region=REGION, model_key=model_key
            ):
                if delta.get("usage"):   # 只带用量的收尾 delta：不进 choices，只留着记账
                    up = delta["usage"]
                if delta["reasoning"]:
                    reason_acc.append(delta["reasoning"])
                if delta["content"]:
                    text_acc.append(delta["content"])
                for tc in (delta["tool_calls"] or []):
                    tool_acc.append(str(((tc or {}).get("function") or {}).get("arguments") or ""))
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

            # ---- 记账：优先上游真数，缺哪格才估哪格 ----
            det = up.get("prompt_tokens_details") or {}
            prompt = int(up.get("prompt_tokens") or 0)
            completion = int(up.get("completion_tokens") or 0)
            hit = int(det.get("cached_tokens") or 0)
            if not prompt:
                prompt = sum(_est_tokens(m.get("content")) for m in messages if isinstance(m, dict))
                for m in messages:
                    if isinstance(m, dict):
                        for tc in (m.get("tool_calls") or []):
                            prompt += _est_tokens(((tc.get("function") or {}).get("arguments")))
                if tools:
                    prompt += _est_tokens(json.dumps(tools, ensure_ascii=False))
            if not completion:
                completion = _est_tokens("".join(text_acc)) + _est_tokens("".join(reason_acc))
            credits = up.get("credits")
            reason = _reasoning_tokens(up, "".join(reason_acc))
            rec = {"ts": created, "model": model, "stream": True, "prompt_tokens": prompt,
                   "completion_tokens": completion, "cached_tokens": hit,
                   "prompt_cache_miss_tokens": max(0, prompt - hit),
                   "reasoning_tokens": reason,
                   "credits": float(credits) if credits is not None else None,
                   "reported": bool(up), "wall": round(time.time() - created, 3)}
            _accumulate(model, rec)
            _write_ledger(rec)

            if include_usage:
                # DeepSeek/OpenAI 的约定：usage 单独一个 chunk，choices 为空
                yield _chunk(completion_id, created, model,
                             usage=usage_payload(prompt, completion, hit, credits=credits,
                                                 reasoning=reason), choices=[])
        except QoderError as exc:
            _accumulate(model, {"error": 1})
            _write_ledger({"ts": created, "model": model, "stream": True, "error": str(exc)[:200]})
            yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Qoder Flash Gateway")
    parser.add_argument("--host", default=str(CONFIG.get("host") or "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(CONFIG.get("port") or 5050))
    parser.add_argument("--gen-key", action="store_true", help="生成一个新 API key 写入 config.json 后退出")
    args = parser.parse_args()

    if args.gen_key:
        key = generate_key()
        write_api_key(key)
        print(f"已写入 {CONFIG_PATH}（600）:\n  api_key = {key}")
        print("重启服务生效：sudo systemctl restart qoder-flash")
        return

    auth = "需要 Bearer key" if API_KEY else "不校验 key（只监听本机时可用）"
    print(f"Qoder Flash Gateway -> http://{args.host}:{args.port}  (region: {REGION}；默认模型: {DEFAULT_MODEL}；{auth})")
    print(f"可用模型: {'、'.join(MODELS)}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
