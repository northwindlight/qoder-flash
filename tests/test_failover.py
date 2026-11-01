"""failover 的单测：用**假流**把各种失败注进来，不碰网络、不烧额度。

跑法（仓库根目录）：
    .venv/bin/python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import failover
from accounts import AccountConfig, AccountPool
from tests.test_accounts import write_auth


def delta(text: str) -> dict:
    return {"role": "assistant", "content": text, "reasoning": "",
            "tool_calls": None, "finish_reason": None}


class FakeQoder:
    """替掉 failover 依赖的两件事：

    - `load_credentials`：返回一个只有 `uid` 的假凭据，uid 就是凭据目录名 —— 剧本据此分账号。
    - `stream_chat`：按剧本演出，同时记下**到底试过哪些账号**（"绝不重试"那条要靠它证）。
    """

    def __init__(self, behaviors: dict[str, object]) -> None:
        self.behaviors = behaviors
        self.calls: list[str] = []

    def install(self) -> None:
        self._real_load = failover.load_credentials
        self._real_stream = failover.stream_chat
        failover.load_credentials = self._load
        failover.stream_chat = self._stream

    def restore(self) -> None:
        failover.load_credentials = self._real_load
        failover.stream_chat = self._real_stream

    def _load(self, auth_dir=None, region=None):
        return SimpleNamespace(uid=Path(auth_dir).parent.name)

    def _stream(self, messages, tools=None, cred=None, reasoning_effort="none",
                region="cn", model_key=None):
        name = str(cred.uid)
        self.calls.append(name)
        behavior = self.behaviors.get(name, "ok")

        async def gen():
            if behavior == "hang":
                await asyncio.sleep(3600)                    # 首字节永远不来（限流就是这个样子）
                return
            if behavior == "disconnect":
                raise httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message body (incomplete chunked read)"
                )
            if behavior == "empty":
                return
            if behavior == "ok_then_disconnect":
                yield delta("半截")
                raise httpx.RemoteProtocolError("mid-stream cut")
            if behavior == "cancel":
                raise asyncio.CancelledError()
            if isinstance(behavior, list):
                for chunk in behavior:
                    yield chunk
                return
            yield delta(f"来自{name}")

        return gen()


class FailoverFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.fake: FakeQoder | None = None

    def tearDown(self) -> None:
        if self.fake is not None:
            self.fake.restore()
        self._tmp.cleanup()

    def account(self, name: str, *, region: str = "cn", models=None) -> AccountConfig:
        config_dir = self.root / name
        write_auth(config_dir, uid=f"uid-{name}")
        return AccountConfig(name=name, region=region, config_dir=config_dir, models=models)

    def pool(self, accounts, **kwargs) -> AccountPool:
        kwargs.setdefault("state_path", self.root / "state.json")
        kwargs.setdefault("first_byte_timeout", 0.2)     # 单测里把超时压到很小
        kwargs.setdefault("suspect_timeout", 0.2)
        kwargs.setdefault("total_budget", 5)
        kwargs.setdefault("cooldown_base", 60)
        kwargs.setdefault("max_cooldown", 600)
        return AccountPool(accounts, **kwargs)

    def open(self, pool, behaviors, **kwargs):
        """只开流、不继续消费（给那些只检查 handle 的用例用）。"""
        self.fake = FakeQoder(behaviors)
        self.fake.install()
        kwargs.setdefault("model", "qwen3.8-flash")
        return asyncio.run(failover.open_stream(pool, [{"role": "user", "content": "hi"}], **kwargs))

    def open_and_drain(self, pool, behaviors, **kwargs) -> dict:
        """**在同一个事件循环里**开流 + 把流消费干净，返回 {'handle', 'items'[, 'error']}。

        ★ 必须同一个循环：`asyncio.run()` 收尾时会 `shutdown_asyncgens()`，
          把已经 prime 过、还没消费完的异步生成器一并关掉 —— 结果是"剩下的 chunk 凭空没了"。
          那是测试假象（生产里只有一个长命循环），但足以把"首块有没有丢""吐过之后会不会重试"
          这两条最要紧的断言变成假绿/假红。
        """
        self.fake = FakeQoder(behaviors)
        self.fake.install()
        kwargs.setdefault("model", "qwen3.8-flash")
        captured: dict = {}

        async def run():
            handle = await failover.open_stream(pool, [{"role": "user", "content": "hi"}], **kwargs)
            captured["handle"] = handle
            captured["items"] = [item async for item in failover.drain(handle)]

        try:
            asyncio.run(run())
        except BaseException as exc:            # noqa: BLE001 —— 让用例自己断言异常
            captured["error"] = exc
        return captured


class FailoverTest(FailoverFixture):
    def test_first_byte_timeout_falls_over_to_next_account(self):
        """★ 上游"静默挂死"（限流）就是这么被吃掉的：超时 → 换下一个账号。"""
        a, b = self.account("A"), self.account("B")
        pool = self.pool([a, b])
        handle = self.open(pool, {"A": "hang", "B": "ok"})
        self.assertEqual(handle.account, "B")
        self.assertEqual(handle.first["content"], "来自B")
        self.assertEqual(self.fake.calls, ["A", "B"])
        self.assertEqual([x.account for x in handle.attempts], ["A"])
        self.assertEqual(handle.attempts[0].kind, "timeout")
        self.assertIn("没吐首字节", handle.attempts[0].detail)
        # A 被冷却记账，B 记成功
        self.assertEqual(pool._state["A"].streak, 1)
        self.assertGreater(pool._state["A"].cooldown_until, time.time())
        self.assertEqual(pool._state["B"].ok, 1)

    def test_midstream_disconnect_is_classified_as_disconnect(self):
        a, b = self.account("A"), self.account("B")
        pool = self.pool([a, b])
        handle = self.open(pool, {"A": "disconnect", "B": "ok"})
        self.assertEqual(handle.account, "B")
        self.assertEqual(handle.attempts[0].kind, "disconnect")

    def test_no_retry_after_first_token(self):
        """★ 已经吐过内容再断流，**绝不许换账号重来**（会拼出前后矛盾的垃圾）。

        判据用"到底试过哪些账号"：B 一次都不该被碰。
        """
        a, b = self.account("A"), self.account("B")
        pool = self.pool([a, b])
        result = self.open_and_drain(pool, {"A": "ok_then_disconnect", "B": "ok"})
        self.assertEqual(result["handle"].account, "A")
        self.assertEqual(result["handle"].first["content"], "半截")
        self.assertIsInstance(result.get("error"), httpx.RemoteProtocolError)
        self.assertEqual(self.fake.calls, ["A"], "吐过内容之后不许再换账号")

    def test_empty_stream_moves_on_without_penalty(self):
        a, b = self.account("A"), self.account("B")
        pool = self.pool([a, b])
        handle = self.open(pool, {"A": "empty", "B": "ok"})
        self.assertEqual(handle.account, "B")
        self.assertEqual(handle.attempts[0].kind, "empty")
        self.assertEqual(pool._state["A"].cooldown_until, 0.0, "空流不该罚冷却")
        self.assertEqual(pool._state["A"].streak, 0, "空流不该算进连续失败")

    def test_all_failed_reports_every_reason(self):
        a, b = self.account("A"), self.account("B")
        pool = self.pool([a, b])
        with self.assertRaises(failover.AllAccountsFailed) as ctx:
            self.open(pool, {"A": "hang", "B": "disconnect"})
        self.assertEqual([x.account for x in ctx.exception.attempts], ["A", "B"])
        text = str(ctx.exception)
        self.assertIn("A: timeout", text)
        self.assertIn("B: disconnect", text)

    def test_cancellation_is_not_swallowed(self):
        """客户端断连（CancelledError）必须原样抛出去，不能当成"这个账号失败"记一笔。"""
        a, b = self.account("A"), self.account("B")
        pool = self.pool([a, b])
        with self.assertRaises(asyncio.CancelledError):
            self.open(pool, {"A": "cancel", "B": "ok"})
        self.assertEqual(self.fake.calls, ["A"])
        self.assertEqual(pool._state["A"].fail, 0, "取消不是账号的错")

    def test_prime_keeps_the_first_chunk(self):
        """★ prime 不能把首块吃掉：少一格 delta，客户端拼出来的答案就是残的。"""
        a = self.account("A")
        pool = self.pool([a])
        result = self.open_and_drain(pool, {"A": [delta("一"), delta("二"), delta("三")]})
        self.assertNotIn("error", result)
        self.assertEqual([item["content"] for item in result["items"]], ["一", "二", "三"])

    def test_model_is_filtered_by_the_account_region(self):
        a = self.account("A", region="intl", models=["qwen3.8-flash"])
        b = self.account("B", region="cn")
        pool = self.pool([a, b])
        handle = self.open(pool, {"A": "ok", "B": "ok"}, model="deepseek-v4-pro")
        self.assertEqual(handle.account, "B")
        self.assertEqual(self.fake.calls, ["B"], "intl 那个号没有这个模型，挑都不该挑它")

    def test_all_cooling_uses_one_probe(self):
        a, b = self.account("A"), self.account("B")
        pool = self.pool([a, b])
        pool.record_failure("A", "timeout")
        pool.record_failure("B", "timeout")
        pool.record_failure("B", "timeout")          # B 连败两次 → 冷却更久
        handle = self.open(pool, {"A": "ok", "B": "ok"})
        self.assertEqual(handle.account, "A", "全在冷却时放最早解冻的那个当探针")
        self.assertEqual(self.fake.calls, ["A"])

    def test_total_budget_caps_the_wait(self):
        """★ 别把 N 个账号的超时串起来：客户端 SDK 默认读超时 60 秒，串起来必爆。"""
        accounts = [self.account(n) for n in ("A", "B", "C")]
        pool = self.pool(accounts, first_byte_timeout=0.3, suspect_timeout=0.3, total_budget=1.5)
        started = time.monotonic()
        with self.assertRaises(failover.AllAccountsFailed) as ctx:
            self.open(pool, {"A": "hang", "B": "hang", "C": "hang"})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, f"超了总预算：{elapsed:.2f}s")
        # 账号顺序按对话指纹粘性偏移（缓存友好），这里只要求三个都试过。
        self.assertEqual(sorted(x.account for x in ctx.exception.attempts), ["A", "B", "C"])
        self.assertEqual(ctx.exception.attempts[-1].kind, "budget")


class StreamWithFailoverTest(FailoverFixture):
    def test_stream_with_failover_yields_everything(self):
        a = self.account("A")
        pool = self.pool([a])
        self.fake = FakeQoder({"A": [delta("一"), delta("二")]})
        self.fake.install()

        async def consume():
            return [item["content"] async for item in failover.stream_with_failover(
                pool, [{"role": "user", "content": "hi"}], model="qwen3.8-flash")]

        self.assertEqual(asyncio.run(consume()), ["一", "二"])

    def test_complete_with_failover_returns_assembled_message(self):
        a, b = self.account("A"), self.account("B")
        pool = self.pool([a, b])
        self.fake = FakeQoder({
            "A": "hang",
            "B": [delta("失败"), delta("也要"), {"role": "", "content": "", "reasoning": "",
                                                 "tool_calls": None, "finish_reason": "stop"}],
        })
        self.fake.install()

        message, handle = asyncio.run(failover.complete_with_failover(
            pool, [{"role": "user", "content": "hi"}], model="qwen3.8-flash"))
        self.assertEqual(handle.account, "B")
        self.assertEqual(message["content"], "失败也要")
        self.assertEqual(message["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
