"""多账号 failover：把"逐个账号试、只在还没吐出任何东西时才换下一个"收敛到这一处。

只有这个模块碰 asyncio —— 于是可以用假流把各种失败注进来做单测，不用碰网络也不烧额度。

★ 两条铁律（写错任何一条，客户端都会拿到垃圾而不是错误）：

1. **先 prime 首块再返回**。`open_stream()` 会在返回之前把第一个 delta 拉出来。
   这样"所有账号都挂了"能变成一个正经的 **502 JSON**，而不是"200 的 SSE 立刻报错"——
   后者大多数 SDK 会当成一次成功但内容为空的调用。

2. **只有"还没吐出任何 item"时才允许换账号**。已经吐过内容再断流就**不重试**：
   客户端手上已经有半截答案了，换个账号从头再来会把两段拼在一起，出一篇前后矛盾的垃圾。
   这种情况只能给一个明确的错误（流式：发一个 error 帧且**不发 `[DONE]`**）。

判活为什么不能看状态码：撞上限时上游回的是 `200 OK` + 一个字节都不发（详见 accounts.py 顶部）。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from accounts import AccountConfig, AccountPool, classify
from qoder import (
    QoderError,
    assemble_message,
    load_credentials,
    resolve_model_key,
    stream_chat,
)


@dataclass
class Attempt:
    """一次失败的尝试（成功的那个账号不会出现在这里）。"""

    account: str
    kind: str
    detail: str = ""


class AllAccountsFailed(QoderError):
    """一个都没成 —— 带上每个账号各自的原因，别让客户端只看到一句无信息的 500。"""

    def __init__(self, attempts: list[Attempt], reason: str = "") -> None:
        self.attempts = list(attempts)
        self.reason = reason
        super().__init__(self.describe())

    def describe(self) -> str:
        if not self.attempts:
            return self.reason or "没有可用账号"
        parts = "；".join(f"{a.account}: {a.kind}（{a.detail[:80]}）" for a in self.attempts)
        head = "所有账号都失败了" if not self.reason else f"所有账号都失败了（{self.reason}）"
        return f"{head} —— {parts}"


@dataclass
class StreamHandle:
    """一次成功的开流：已被 prime 出来的首块 + 还活着的上游流。"""

    stream: AsyncIterator[dict[str, Any]]
    first: dict[str, Any]
    account: str
    region: str
    model_key: str
    attempts: list[Attempt] = field(default_factory=list)   # 成功之前试挂的那些账号（记账用）


async def _aclose(stream: AsyncIterator[dict[str, Any]]) -> None:
    """关掉上游流。

    一定要显式关：消费方弃流时（Starlette 在客户端断开时只取消 task，不会 aclose 这个
    body 迭代器），靠 GC 收尾会让 httpx 的连接悬着，测试里还会炸
    `coroutine ignored GeneratorExit`。关不上也不能盖住真正的错因。
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:                # noqa: BLE001 —— 关连接失败无所谓，别盖住错因
        pass


def _no_candidate_reason(pool: AccountPool, model: str | None) -> str:
    reasons = pool.skip_reasons(model)
    if not reasons:
        return "没有配置任何账号"
    return "没有可用账号 —— " + "；".join(f"{name}: {text}" for name, text in reasons.items())


async def open_stream(
    pool: AccountPool,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    model: str | None = None,
    effort: str = "none",
) -> StreamHandle:
    """按配置顺序逐个账号试，返回第一个吐得出东西的。

    **返回之前首块已经到手**（prime 语义），全挂则抛 `AllAccountsFailed`。
    """
    attempts: list[Attempt] = []
    candidates = pool.candidates(model)
    if not candidates:
        # 全在冷却：放一个探针，让上游一恢复就能自己被发现（而不是等人手动解冻）
        probe = pool.fallback(model)
        if probe is None:
            raise AllAccountsFailed(attempts, _no_candidate_reason(pool, model))
        candidates = [probe]

    deadline = time.monotonic() + pool.total_budget
    for acct in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            attempts.append(Attempt(acct.name, "budget", "总预算用完了，没轮到它"))
            break
        attempt = await _try_account(
            acct, messages, tools, model=model, effort=effort,
            # 可疑账号用短超时，但**绝不超过总预算**：别把 N 个账号的超时串起来撑爆客户端
            timeout=min(pool.timeout_for(acct), remaining),
        )
        if isinstance(attempt, Attempt):
            attempts.append(attempt)
            pool.record_failure(acct.name, attempt.kind, attempt.detail)
            continue
        pool.record_success(acct.name)
        return StreamHandle(
            stream=attempt.stream, first=attempt.first, account=acct.name,
            region=acct.region, model_key=attempt.model_key, attempts=list(attempts),
        )
    raise AllAccountsFailed(attempts)


@dataclass
class _Opened:
    """一个账号成功开流（且首块已经到手）的内部结果。"""

    stream: AsyncIterator[dict[str, Any]]
    first: dict[str, Any]
    model_key: str


async def _try_account(
    acct: AccountConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    model: str | None,
    effort: str,
    timeout: float,
) -> _Opened | Attempt:
    """试一个账号：成功返回已 prime 的 `_Opened`，失败返回一条 `Attempt`。"""
    try:
        # 用**这个账号自己的 region** 换算模型 key（两区的可用模型不一样）
        model_key = resolve_model_key(model, acct.region)
    except QoderError as exc:            # 挑选阶段已按 supports() 过滤过，走到这里说明配置有问题
        return Attempt(acct.name, "model", str(exc))

    try:
        cred = load_credentials(auth_dir=acct.auth_dir)
    except Exception as exc:             # noqa: BLE001 —— 凭据坏了是本地问题，不该把整个请求带崩
        return Attempt(acct.name, "credential", f"{type(exc).__name__}: {exc}")

    stream = stream_chat(
        messages, tools, cred, reasoning_effort=effort,
        region=acct.region, model_key=model_key,
    )
    try:
        # ★ 判据只有"吐没吐过 item"：超时取消在栈展开时，httpx 的 aclose 可能用
        #   RemoteProtocolError 顶掉 CancelledError，所以指望异常类型干净是不现实的。
        async with asyncio.timeout(timeout):
            first = await stream.__anext__()
    except StopAsyncIteration:
        # 干净的空流：连上了、正常结束、一个字都没有。不冷却（回得快，也不是"这个号废了"的证据）
        await _aclose(stream)
        return Attempt(acct.name, "empty", "连上了但一个字都没吐")
    except (TimeoutError, httpx.HTTPError, QoderError, OSError) as exc:
        await _aclose(stream)
        kind = classify(exc)
        detail = f"{timeout:.0f} 秒没吐首字节" if kind == "timeout" else str(exc)
        return Attempt(acct.name, kind, detail)
    except BaseException:
        # 客户端断连（asyncio.CancelledError）之类：先把连接关掉，再原样抛，**绝不吞**
        await _aclose(stream)
        raise
    return _Opened(stream=stream, first=first, model_key=model_key)


async def drain(handle: StreamHandle) -> AsyncIterator[dict[str, Any]]:
    """按顺序吐出这次开流的全部 delta —— **包含已经被 prime 出来的首块**（少了它就少一格）。"""
    try:
        yield handle.first
        async for delta in handle.stream:
            yield delta
    finally:
        await _aclose(handle.stream)


async def stream_with_failover(
    pool: AccountPool,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    model: str | None = None,
    effort: str = "none",
) -> AsyncIterator[dict[str, Any]]:
    """开流 + 短路版：直接吐 delta。全挂时抛 `AllAccountsFailed`（此时**还没吐过任何东西**，
    调用方可以安全地回一个 502）。"""
    handle = await open_stream(pool, messages, tools, model=model, effort=effort)
    async for delta in drain(handle):
        yield delta


async def complete_with_failover(
    pool: AccountPool,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    model: str | None = None,
    effort: str = "none",
) -> tuple[dict[str, Any], StreamHandle]:
    """非流式：把 failover 后的流拼成一条完整回复，返回 (message, handle)。"""
    handle = await open_stream(pool, messages, tools, model=model, effort=effort)
    message = await assemble_message(drain(handle))
    return message, handle
