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

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from accounts import AccountPool, build_pool
from failover import (
    AllAccountsFailed,
    Attempt,
    complete_with_failover,
    drain,
    open_stream,
)
from qoder import DEFAULT_MODEL, QoderError, resolve_model_key

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

# 账号池状态（冷却/失败计数）落盘位置：撞了上限的账号要冷却几十分钟，
# 进程一重启就忘掉的话，第一发请求又要白等它超时一次。
ACCOUNT_STATE_PATH = Path(
    os.getenv("QODER_ACCOUNT_STATE") or (Path(__file__).with_name("account-state.json"))
)

# ★ 多账号：`config.json` 里的 `accounts` 有几个就是几个。没配的话
#   `build_pool` 会拿 `region` 合成一个 —— 与改造前的单区行为完全一致。
POOL: AccountPool = build_pool(CONFIG, state_path=ACCOUNT_STATE_PATH)

# 对外报的模型名 = 各账号模型表的**并集**（同一个模型名可能只有部分账号能用，
# 真正发不发得出去由挑选阶段按账号的 region 判）。
MODELS = POOL.all_models() or [DEFAULT_MODEL]
# 向后兼容：老客户端会读 `/health` 的 `region`。配了 accounts 就以第一个账号为准。
REGION = str(CONFIG.get("region") or POOL.accounts[0].region).strip().lower()
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


def any_account_supports(model: str) -> bool:
    """有没有任何一个账号发得了这个模型。

    没有的话是**客户端的错**（400），不是上游的错（502）—— 别让"模型名写错了"
    看起来像"服务挂了"。
    """
    return any(acct.supports(model) for acct in POOL.accounts)


def accounts_problem() -> str:
    """一个账号都用不了时，拼一句能读的原因出来。"""
    reasons = POOL.skip_reasons()
    return "；".join(f"{name}: {text}" for name, text in reasons.items()) or "没有配置任何账号"


# ★ 用量台账：每完成一次调用追加一行 JSONL，并把累计值给 /stats。
#   为什么值得记：上游**每一块都带真实计数**（含 cached_tokens 与 credits），
#   老版本全丢掉了（2026-10-02 之前 usage 恒为 0）。记下来才能回答
#   「这一局到底花了多少 token / 多少 credits」这种问题。
LEDGER_PATH = Path(os.getenv("QODER_LEDGER") or (Path(__file__).with_name("usage-ledger.jsonl")))

# 进程内累计（不读盘，给 /stats 的 session 段）。uvicorn 单进程跑，锁只是防并发请求。
_USAGE_LOCK = threading.Lock()
_USAGE: dict[str, dict[str, Any]] = {}          # 按模型（老口径）
_USAGE_ACCOUNT: dict[str, dict[str, Any]] = {}  # 按账号（多账号之后才有的口径）


def _blank_usage() -> dict[str, Any]:
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_tokens": 0, "reasoning_tokens": 0, "credits": 0.0, "errors": 0}


def _fold_into(target: dict[str, dict[str, Any]], key: str, rec: dict[str, Any]) -> None:
    """把一条台账记录累加进 `target[key]`。会话内累计与台账全量重算共用这一份。"""
    slot = target.setdefault(str(key or "?"), _blank_usage())
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


def _accumulate(model: str, account: str | None, rec: dict[str, Any]) -> None:
    """一次调用同时记进"按模型"和"按账号"两个口径。"""
    with _USAGE_LOCK:
        _fold_into(_USAGE, str(model or "?"), rec)
        _fold_into(_USAGE_ACCOUNT, str(account or "?"), rec)


def _group(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    """把台账行按某个字段分组。老行没有 `account` 字段 → 归到 `"?"`。"""
    acc: dict[str, dict[str, Any]] = {}
    for rec in rows:
        _fold_into(acc, str(rec.get(field) or "?"), rec)
    return acc


def _record_attempts(created: int, model: str, stream: bool, attempts: list[Attempt]) -> None:
    """把"试过但失败的账号"也记进台账。

    只记成功那一次的话，按账号根本看不出抖动来 —— 换了哪个号在顶、为什么换，
    都得在台账里留痕。
    """
    for attempt in attempts:
        _accumulate(model, attempt.account, {"error": 1})
        _write_ledger({"ts": created, "model": model, "account": attempt.account, "stream": stream,
                       "error": f"{attempt.kind}: {attempt.detail}"[:200]})


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
    """探活不需要 key；但账号详情（uid/姓名/是否过期/冷却）只给带 key 的人看。

    `ready` 的口径**刻意不含冷却**：它只回答"本地凭据配好且没过期吗"。
    一个账号撞了每日上限是**暂时**的，把它算成"没就绪"会让客户端直接放弃，
    而冷却情况已经在 `accounts[*].cooling` 里如实报出来了。
    """
    snapshot = POOL.snapshot()
    ready = any(row["problem"] is None for row in snapshot.values())
    if not authorized(authorization):
        return {"ready": ready}
    # 向后兼容：老客户端读的是 `credentials` 这个单账号字段。指向**第一个能用的**账号 ——
    # 排在第一位那个可能正好撞了上限在冷却，那时候报它反而是误导。
    usable = [a for a in POOL.accounts
              if snapshot[a.name]["problem"] is None and not snapshot[a.name]["cooling"]]
    preferred = (usable or POOL.accounts)[0]
    body: dict[str, Any] = {
        "ready": ready,
        "region": REGION,
        "default_model": DEFAULT_MODEL,
        "models": MODELS,
        "credentials": snapshot[preferred.name]["credentials"],
        "accounts": snapshot,
    }
    if not ready:
        body["error"] = accounts_problem()
    return body


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
    按模型和**按账号**两个口径都有：多账号之后"哪个号在供、各花了多少"就靠 `by_account`。
    老台账行没有 `account` 字段，归到 `"?"`。
    """
    check_api_key(authorization)

    with _USAGE_LOCK:
        session_model = {k: dict(v) for k, v in _USAGE.items()}
        session_account = {k: dict(v) for k, v in _USAGE_ACCOUNT.items()}
    rows = _read_ledger()
    ledger_model = _group(rows, "model")
    ledger_account = _group(rows, "account")

    def _finish(acc: dict[str, dict[str, Any]]) -> dict[str, Any]:
        grand = _blank_usage()
        for slot in acc.values():
            for k in grand:
                grand[k] += slot[k]
        grand["credits"] = round(grand["credits"], 6)
        for slot in acc.values():
            slot["credits"] = round(slot["credits"], 6)
            inp = slot["prompt_tokens"]
            slot["cache_hit_rate"] = round(slot["cached_tokens"] / inp, 4) if inp else None
        grand["cache_hit_rate"] = round(grand["cached_tokens"] / grand["prompt_tokens"], 4) if grand["prompt_tokens"] else None
        return {"totals": grand}

    return {"ledger_path": str(LEDGER_PATH),
            "state_path": str(ACCOUNT_STATE_PATH),
            "accounts": POOL.snapshot(),
            "session": {"by_model": session_model, "by_account": session_account,
                        **_finish(session_model)},
            "ledger": {"by_model": ledger_model, "by_account": ledger_account,
                       **_finish(ledger_model)}}


@app.post("/admin/accounts/reset")
@app.post("/admin/accounts/{name}/reset")
async def reset_accounts(name: str | None = None,
                         authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """手动解冻账号（你觉得上游该重置了 / 刚重新登录过 / 刚改完配置）。

    没有它就只能重启服务 —— 而重启会把没落盘的东西一起丢掉，没必要。
    `name` 不填就是全部解冻。
    """
    check_api_key(authorization)
    names = [a.name for a in POOL.accounts]
    if name is not None and name not in names:
        raise HTTPException(status_code=404,
                            detail=f"没有这个账号：{name}；现有：{'、'.join(names)}")
    return {"cleared": POOL.clear_cooldown(name), "accounts": POOL.snapshot()}


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

    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else None
    effort = resolve_effort(payload)
    model = str(payload.get("model") or DEFAULT_MODEL)
    try:
        resolve_model_key(model, REGION)   # 只做"认不认识这个名字"的校验（换算口径只有这一处）
    except QoderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not any_account_supports(model):
        # 名字认得出、但没有任何账号发得了 —— 同样是**客户端的错**，别报成 502
        raise HTTPException(status_code=400, detail=(
            f"没有账号能发 {model!r}；当前可用模型：{'、'.join(MODELS)}"))
    completion_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    if not payload.get("stream"):
        try:
            message, handle = await complete_with_failover(
                POOL, messages, tools, model=model, effort=effort
            )
        except AllAccountsFailed as exc:
            _record_attempts(created, model, False, exc.attempts)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (QoderError, httpx.HTTPError, OSError) as exc:
            # 首块已经吐过之后才断的流：**不重试**（重来会拼出前后矛盾的垃圾），如实报错。
            # 顺带补掉改造前那个裸 500：RemoteProtocolError 不是 QoderError，原来会穿透成 traceback
            _accumulate(model, "?", {"error": 1})
            _write_ledger({"ts": created, "model": model, "stream": False, "error": str(exc)[:200]})
            raise HTTPException(status_code=502, detail=f"上游流中途断开（已产出部分内容，不重试）：{exc}") from exc
        account = handle.account
        _record_attempts(created, model, False, handle.attempts)
        # ★ `usage` 是拼装时顺手带回来的上游真数，取走后别留在 message 里
        up = message.pop("usage", None) or {}
        det = up.get("prompt_tokens_details") or {}
        prompt = int(up.get("prompt_tokens") or 0)
        completion = int(up.get("completion_tokens") or 0)
        hit = int(det.get("cached_tokens") or 0)
        if not prompt:                       # 上游没报 ⇒ 估，并如实记 reported=False
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
        rec = {"ts": created, "model": model, "account": account, "stream": False,
               "prompt_tokens": prompt, "completion_tokens": completion,
               "total_tokens": prompt + completion,   # ★ 不写它 /stats 的合计就恒为 0
               "cached_tokens": hit,
               "prompt_cache_miss_tokens": max(0, prompt - hit),
               "reasoning_tokens": reason,
               "credits": float(credits) if credits is not None else None,
               "reported": bool(up)}
        _accumulate(model, account, rec)
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

    try:
        # ★ 先把首块 prime 出来再返回响应：全账号都挂的时候这里就抛了 → 回一个正经的
        #   502 JSON，而不是"200 的 SSE 立刻报错"（后者多数 SDK 会当成一次成功但内容为空的调用）
        handle = await open_stream(POOL, messages, tools, model=model, effort=effort)
    except AllAccountsFailed as exc:
        _record_attempts(created, model, True, exc.attempts)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    account = handle.account
    _record_attempts(created, model, True, handle.attempts)

    async def event_stream() -> AsyncIterator[str]:
        emitted_role = False
        saw_finish = False
        up: dict[str, Any] = {}          # 上游累计用量（收尾那条带）
        text_acc: list[str] = []
        reason_acc: list[str] = []
        try:
            async for delta in drain(handle):     # drain 会把 prime 出来的首块一起吐出来
                if delta.get("usage"):   # 只带用量的收尾 delta：不进 choices，只留着记账
                    up = delta["usage"]
                if delta["reasoning"]:
                    reason_acc.append(delta["reasoning"])
                if delta["content"]:
                    text_acc.append(delta["content"])
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
                if delta["finish_reason"]:
                    saw_finish = True
                if out or delta["finish_reason"]:
                    yield _chunk(completion_id, created, model, out, delta["finish_reason"])
            if not saw_finish:
                # 上游没给收尾就补一个 —— 但**别重复发**：上游给的收尾（上面那条）已经发过了，
                # 再补一个客户端会看到两个 `stop`（纯噪声，也让"最后一个 chunk 才是终局"的假设失效）
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
            rec = {"ts": created, "model": model, "account": account, "stream": True,
                   "prompt_tokens": prompt, "completion_tokens": completion,
                   "total_tokens": prompt + completion,   # ★ 不写它 /stats 的合计就恒为 0
                   "cached_tokens": hit,
                   "prompt_cache_miss_tokens": max(0, prompt - hit),
                   "reasoning_tokens": reason,
                   "credits": float(credits) if credits is not None else None,
                   "reported": bool(up), "wall": round(time.time() - created, 3)}
            _accumulate(model, account, rec)
            _write_ledger(rec)

            if include_usage:
                # DeepSeek/OpenAI 的约定：usage 单独一个 chunk，choices 为空
                yield _chunk(completion_id, created, model,
                             usage=usage_payload(prompt, completion, hit, credits=credits,
                                                 reasoning=reason), choices=[])
        except (QoderError, httpx.HTTPError, OSError) as exc:
            # ★ 首块已经吐过 ⇒ **不换账号**（换个号从头再来会把两段答案拼在一起）。
            #   而且**不发 [DONE]**：多数 SDK 把 [DONE] 当正常收尾，会把半截答案当完整答案
            #   —— 那比直接报错更坑。这里只发一个错误帧就断流，让 SDK 自己认出这是断的。
            _accumulate(model, account, {"error": 1})
            _write_ledger({"ts": created, "model": model, "account": account, "stream": True,
                           "error": str(exc)[:200]})
            yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n"
            return
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
    # ★ 全部 flush：这些行是打给"出事时的人"看的，stdout 一被重定向就是块缓冲，
    #   不 flush 的话它们会卡在缓冲区里，等真要看的时候一个字都看不到
    #   （systemd 那边有 PYTHONUNBUFFERED=1 兜着，手动重定向跑就没有）
    print(f"Qoder Flash Gateway -> http://{args.host}:{args.port}  (region: {REGION}；默认模型: {DEFAULT_MODEL}；{auth})", flush=True)
    print(f"可用模型: {'、'.join(MODELS)}", flush=True)
    # 启动自检：账号配错了（路径写重、uid 是同一个登录、凭据缺失）在这里就说清楚，
    # 别等第一发请求挂在那儿才让人去猜
    print(f"账号（按挑选顺序）: {'、'.join(f'{a.name}[{a.region}]' for a in POOL.accounts)}", flush=True)
    for warning in POOL.verify():
        print(f"[账号池] 警告：{warning}", flush=True)
    for name, row in POOL.snapshot().items():
        cooling = f"，冷却 {row['cooldown_seconds']}s（{row['last_error']}）" if row["cooling"] else ""
        print(f"  - {name}: ok={row['ok']} fails={row['fails']}{cooling}", flush=True)
    print(f"状态文件: {ACCOUNT_STATE_PATH}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
