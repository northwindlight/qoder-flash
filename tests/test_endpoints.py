"""端到端（进程内）的接口测试：用 FastAPI 的 TestClient 打真接口，用假流喂上游。

盯的是**只有接口层才看得见**的那两条行为：
  - 全账号都挂时回 502 JSON，而不是"200 的 SSE 立刻报错"（后者 SDK 会当成空回复）
  - 已经吐过内容才断流时，只发错误帧、**绝不再发 `[DONE]`**（发了 SDK 就把半截答案当完整答案）

跑法（仓库根目录）：
    .venv/bin/python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import failover
import main
from accounts import AccountConfig, AccountPool
from tests.test_accounts import write_auth
from tests.test_failover import FakeQoder, delta


class EndpointFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.fake: FakeQoder | None = None
        self._saved = (main.POOL, main.MODELS, main.REGION, main.API_KEY, main.LEDGER_PATH)
        main.API_KEY = ""                      # 单测不校验 key
        main.LEDGER_PATH = self.root / "ledger.jsonl"
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        if self.fake is not None:
            self.fake.restore()
        main.POOL, main.MODELS, main.REGION, main.API_KEY, main.LEDGER_PATH = self._saved
        self._tmp.cleanup()

    def install_pool(self, specs: list[tuple], behaviors: dict[str, object], **kwargs) -> AccountPool:
        """specs = [(账号名, region[, models]), ...]；behaviors 是假流的剧本。"""
        accounts = []
        for spec in specs:
            name, region = spec[0], spec[1]
            models = spec[2] if len(spec) > 2 else None
            config_dir = self.root / name
            write_auth(config_dir, uid=f"uid-{name}")
            accounts.append(AccountConfig(name=name, region=region, config_dir=config_dir, models=models))
        kwargs.setdefault("state_path", self.root / "state.json")
        kwargs.setdefault("first_byte_timeout", 0.2)
        kwargs.setdefault("suspect_timeout", 0.2)
        kwargs.setdefault("total_budget", 5)
        pool = AccountPool(accounts, **kwargs)
        self.fake = FakeQoder(behaviors)
        self.fake.install()
        main.POOL = pool
        main.MODELS = pool.all_models()
        main.REGION = accounts[0].region
        return pool

    def ledger_rows(self) -> list[dict]:
        if not main.LEDGER_PATH.exists():
            return []
        return [json.loads(line) for line in main.LEDGER_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]

    def post(self, **body):
        body.setdefault("model", "qwen3.8-flash")
        body.setdefault("messages", [{"role": "user", "content": "hi"}])
        return self.client.post("/v1/chat/completions", json=body)


class StreamEndpointTest(EndpointFixture):
    def test_all_accounts_failing_returns_502_not_a_200_stream(self):
        """★ 全挂必须是 502 JSON：200 的 SSE 立刻报错会被 SDK 当成"成功但没内容"。"""
        pool = self.install_pool([("A", "cn"), ("B", "cn")], {"A": "hang", "B": "hang"})
        response = self.post(stream=True)
        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertIn("A: timeout", detail)
        self.assertIn("B: timeout", detail)
        self.assertEqual(sorted(n for n, st in pool._state.items() if st.fail), ["A", "B"])

    def test_error_after_first_token_sends_no_done(self):
        """★ 吐过内容才断流：发错误帧就断，**不发 [DONE]**（发了就等于骗 SDK 说答完了）。"""
        self.install_pool([("A", "cn")], {"A": "ok_then_disconnect"})
        response = self.post(stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("半截", response.text)
        self.assertIn("upstream_error", response.text)
        self.assertNotIn("[DONE]", response.text, "断流之后不许发 [DONE]")

    def test_successful_stream_is_well_formed(self):
        self.install_pool([("A", "cn")], {"A": [delta("你"), delta("好")]})
        response = self.post(stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"content":"你"', response.text)
        self.assertIn('"content":"好"', response.text)
        self.assertIn('"finish_reason":"stop"', response.text)
        self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))
        # 收尾只能有一个：上游给了 stop 就不许再补一个（重复的终局 chunk 是纯噪声）
        self.assertEqual(response.text.count('"finish_reason":"stop"'), 1)

    def test_finish_reason_is_synthesized_when_upstream_omits_it(self):
        """上游没给收尾时要补一个 —— 否则客户端会一直等一个永远不来的终局。"""
        tail = {"role": "", "content": "", "reasoning": "", "tool_calls": None, "finish_reason": None}
        self.install_pool([("A", "cn")], {"A": [delta("好"), tail]})
        response = self.post(stream=True)
        self.assertEqual(response.text.count('"finish_reason":"stop"'), 1)

    def test_failover_is_invisible_to_the_client(self):
        """★ A 挂死、B 正常 —— 客户端只该看到一份完整答案，外加台账里留下的痕迹。"""
        self.install_pool([("A", "cn"), ("B", "cn")], {"A": "hang", "B": [delta("来自B")]})
        response = self.post(stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("来自B", response.text)
        self.assertEqual(self.fake.calls, ["A", "B"])
        rows = self.ledger_rows()
        self.assertEqual([r["account"] for r in rows], ["A", "B"])
        self.assertIn("timeout", rows[0]["error"])
        self.assertNotIn("error", rows[1])

    def test_unknown_model_is_400(self):
        self.install_pool([("A", "cn")], {"A": "ok"})
        response = self.post(model="瞎编的模型", stream=True)
        self.assertEqual(response.status_code, 400)

    def test_model_that_no_account_has_is_400_not_502(self):
        """模型名认得出、但没有账号发得了 —— 是**客户端的错**，别报成上游挂了。

        阳性对照：同一个账号发它自己有的模型必须通，否则这条断言可能只是"什么都通不过"。
        """
        self.install_pool([("A", "intl", ("qwen3.8-flash",))], {"A": "ok"})
        response = self.post(model="glm-5.3", stream=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn("没有账号能发", response.json()["detail"])
        self.assertEqual(self.fake.calls, [], "400 就不该往上游发请求")
        self.assertEqual(self.post(model="qwen3.8-flash", stream=True).status_code, 200)
        self.assertEqual(self.fake.calls, ["A"])


class NonStreamEndpointTest(EndpointFixture):
    def test_non_stream_fails_over(self):
        self.install_pool([("A", "cn"), ("B", "cn")], {"A": "hang", "B": [delta("来自B")]})
        response = self.post(model="qwen3.8-flash")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "来自B")
        self.assertEqual(self.fake.calls, ["A", "B"])

    def test_non_stream_all_failed_is_502(self):
        self.install_pool([("A", "cn")], {"A": "disconnect"})
        response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertIn("disconnect", response.json()["detail"])

    def test_usage_is_reported(self):
        """上游报的用量要透出来（含 credits），别退回全 0。"""
        self.install_pool([("A", "cn")], {"A": [
            delta("好"),
            {"role": "", "content": "", "reasoning": "", "tool_calls": None, "finish_reason": None,
             "usage": {"prompt_tokens": 31, "completion_tokens": 5,
                       "prompt_tokens_details": {"cached_tokens": 7}, "credits": 0.0012}},
        ]})
        body = self.post().json()
        self.assertEqual(body["usage"]["prompt_tokens"], 31)
        self.assertEqual(body["usage"]["prompt_cache_hit_tokens"], 7)
        self.assertEqual(body["usage"]["credits"], 0.0012)
        self.assertEqual(self.ledger_rows()[0]["account"], "A")


class ObservabilityTest(EndpointFixture):
    def test_health_lists_every_account(self):
        self.install_pool([("A", "cn"), ("intl", "intl")], {"A": "ok", "intl": "ok"})
        body = self.client.get("/health", headers={"Authorization": "Bearer "}).json()
        self.assertTrue(body["ready"])
        self.assertEqual(sorted(body["accounts"]), ["A", "intl"])
        self.assertEqual(body["accounts"]["A"]["credentials"]["uid"], "uid-A")
        # 向后兼容：老字段还在，且指向第一个账号
        self.assertEqual(body["credentials"]["uid"], "uid-A")
        self.assertIn("region", body)

    def test_health_reports_cooling_after_a_failure(self):
        pool = self.install_pool([("A", "cn"), ("B", "cn")], {"A": "hang", "B": "ok"})
        self.post(stream=True)
        body = self.client.get("/health", headers={"Authorization": "Bearer "}).json()
        self.assertTrue(body["accounts"]["A"]["cooling"])
        self.assertIn("timeout", body["accounts"]["A"]["last_error"])
        self.assertFalse(body["accounts"]["B"]["cooling"])
        self.assertTrue(body["ready"], "冷却不影响 ready：那只是暂时的")
        # 兼容字段要指向**能用的**那个：排第一的 A 正在冷却，报它等于误导
        self.assertEqual(body["credentials"]["uid"], "uid-B")

    def test_health_without_key_hides_details(self):
        """配了 key 之后，不带 key 只能看到 ready（uid/姓名/冷却都不给）。

        注意：`API_KEY` 为空时 `authorized()` 恒真 —— 那是"本机不校验 key"的旧语义，
        所以这条必须先把 key 配上才测得到。
        """
        self.install_pool([("A", "cn")], {"A": "ok"})
        main.API_KEY = "test-key"
        self.assertEqual(self.client.get("/health").json(), {"ready": True})
        body = self.client.get("/health", headers={"Authorization": "Bearer test-key"}).json()
        self.assertIn("accounts", body)

    def test_models_is_the_union_of_accounts(self):
        self.install_pool([("A", "intl"), ("B", "cn")], {"A": "ok", "B": "ok"})
        ids = {row["id"] for row in self.client.get("/models").json()["data"]}
        self.assertIn("qwen3.8-flash", ids)
        self.assertIn("glm-5.3", ids)

    def test_admin_reset_clears_cooldown(self):
        pool = self.install_pool([("A", "cn"), ("B", "cn")], {"A": "hang", "B": "ok"})
        self.post(stream=True)
        self.assertTrue(pool.snapshot()["A"]["cooling"])
        body = self.client.post("/admin/accounts/A/reset", headers={"Authorization": "Bearer "}).json()
        self.assertEqual(body["cleared"], ["A"])
        self.assertFalse(body["accounts"]["A"]["cooling"])

    def test_admin_reset_all(self):
        pool = self.install_pool([("A", "cn"), ("B", "cn")], {"A": "hang", "B": "hang"})
        self.post(stream=True)
        body = self.client.post("/admin/accounts/reset", headers={"Authorization": "Bearer "}).json()
        self.assertEqual(sorted(body["cleared"]), ["A", "B"])
        self.assertFalse(any(row["cooling"] for row in pool.snapshot().values()))

    def test_admin_reset_unknown_name_is_404(self):
        self.install_pool([("A", "cn")], {"A": "ok"})
        response = self.client.post("/admin/accounts/没有这个/reset", headers={"Authorization": "Bearer "})
        self.assertEqual(response.status_code, 404)

    def test_stats_groups_by_account(self):
        self.install_pool([("A", "cn"), ("B", "cn")], {"A": "hang", "B": "ok"})
        self.post(stream=True)
        body = self.client.get("/stats", headers={"Authorization": "Bearer "}).json()
        self.assertIn("B", body["ledger"]["by_account"])
        self.assertEqual(body["ledger"]["by_account"]["B"]["calls"], 1)
        self.assertEqual(body["ledger"]["by_account"]["A"]["errors"], 1)
        self.assertIn("B", body["session"]["by_account"])
        # 合计得是真的：台账行里不写 total_tokens 的话这里会恒为 0
        totals = body["ledger"]["totals"]
        self.assertGreater(totals["total_tokens"], 0)
        self.assertEqual(totals["total_tokens"], totals["prompt_tokens"] + totals["completion_tokens"])
        self.assertIn("accounts", body)


if __name__ == "__main__":
    unittest.main()
