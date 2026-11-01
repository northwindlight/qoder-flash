"""账号池的单测。**不碰网络、不碰真凭据**：每个用例自己在临时目录里造一份能解的 `.auth/`。

跑法（仓库根目录）：
    .venv/bin/python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from accounts import AccountConfig, AccountPool, build_pool, classify
import httpx
from qoder import QoderError


def write_auth(
    config_dir: Path,
    *,
    uid: str = "uid-0001",
    name: str = "tester",
    user_type: str = "personal_professional_trial",
    expires_at: float | None = None,
    machine_id: str = "0123456789abcdef-machine",
) -> Path:
    """在 config_dir 下造一份真能被 load_credentials 解开的凭据（AES-CBC，密钥=IV=machine_id 前 16 字节）。"""
    auth = config_dir / ".auth"
    auth.mkdir(parents=True, exist_ok=True)
    (auth / "machine_id").write_text(machine_id, encoding="utf-8")
    payload = {
        "uid": uid,
        "name": name,
        "user_type": user_type,
        "security_oauth_token": "dt-test-token",
        "refresh_token": "drt-test-token",
        "expire_time": int(expires_at if expires_at is not None else time.time() + 86400),
    }
    key = machine_id[:16].encode("ascii")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(json.dumps(payload).encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    blob = encryptor.update(padded) + encryptor.finalize()
    (auth / "user").write_text(base64.b64encode(blob).decode("ascii"), encoding="utf-8")
    return auth


class AccountConfigTest(unittest.TestCase):
    def test_auth_dir_and_expanduser(self):
        acct = AccountConfig(name="a", region="cn", config_dir="~/.qoder-cn")
        self.assertEqual(acct.auth_dir, Path.home() / ".qoder-cn" / ".auth")

    def test_model_list_follows_region_by_default(self):
        acct = AccountConfig(name="a", region="intl", config_dir="/tmp/x")
        self.assertEqual(acct.model_list(), ["qwen3.8-flash", "qwen3.8-max", "deepseek-flash"])

    def test_model_list_explicit_wins(self):
        acct = AccountConfig(name="a", region="cn", config_dir="/tmp/x", models=["qwen3.8-flash"])
        self.assertEqual(acct.model_list(), ["qwen3.8-flash"])
        self.assertTrue(acct.supports("qwen3.8-flash"))
        self.assertFalse(acct.supports("deepseek-v4-pro"))

    def test_supports_accepts_internal_key(self):
        """README 里说了可以直接发 Qoder 的内部 key，判据必须认得它。"""
        acct = AccountConfig(name="a", region="cn", config_dir="/tmp/x", models=["qwen3.8-flash"])
        self.assertTrue(acct.supports("qfmodel"))
        self.assertFalse(acct.supports("gmodel"))

    def test_supports_unknown_model_is_false_not_raise(self):
        acct = AccountConfig(name="a", region="cn", config_dir="/tmp/x")
        self.assertFalse(acct.supports("瞎编的模型"))

    def test_from_region_uses_region_table(self):
        acct = AccountConfig.from_region("cn")
        self.assertEqual(acct.region, "cn")
        self.assertEqual(acct.auth_dir, Path.home() / ".qoder-cn" / ".auth")


class ClassifyTest(unittest.TestCase):
    def test_timeout(self):
        self.assertEqual(classify(TimeoutError()), "timeout")
        self.assertEqual(classify(httpx.ReadTimeout("")), "timeout")

    def test_disconnect(self):
        """限流时上游是"中途掐断"，这个分类必须落到 disconnect。"""
        self.assertEqual(classify(httpx.RemoteProtocolError("peer closed")), "disconnect")
        self.assertEqual(classify(httpx.ConnectError("nope")), "disconnect")
        self.assertEqual(classify(ConnectionResetError()), "disconnect")

    def test_auth_and_http(self):
        req = httpx.Request("POST", "https://example.com")
        resp = httpx.Response(401, request=req)
        self.assertEqual(classify(httpx.HTTPStatusError("401", request=req, response=resp)), "auth")
        self.assertEqual(classify(QoderError("凭据被拒（HTTP 403）：nope")), "auth")
        self.assertEqual(classify(QoderError("HTTP 500 boom")), "http")


class PoolFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_path = self.root / "account-state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def make_account(self, name: str, *, region: str = "cn", uid: str | None = None,
                     expires_at: float | None = None, models=None) -> AccountConfig:
        config_dir = self.root / name
        write_auth(config_dir, uid=uid or f"uid-{name}", expires_at=expires_at)
        return AccountConfig(name=name, region=region, config_dir=config_dir, models=models)

    def make_pool(self, accounts, **kwargs) -> AccountPool:
        kwargs.setdefault("state_path", self.state_path)
        return AccountPool(accounts, **kwargs)


class SelectionTest(PoolFixture):
    def test_candidates_keep_config_order(self):
        pool = self.make_pool([self.make_account("first"), self.make_account("second")])
        self.assertEqual([a.name for a in pool.candidates()], ["first", "second"])

    def test_cooldown_account_is_skipped(self):
        a, b = self.make_account("a"), self.make_account("b")
        pool = self.make_pool([a, b])
        pool.record_failure("a", "timeout", "首字节超时")
        self.assertEqual([x.name for x in pool.candidates()], ["b"])
        self.assertIn("冷却中", pool.skip_reasons()["a"])

    def test_model_filter_skips_account_without_that_model(self):
        """★ 模型不支持必须在挑选阶段就过滤：让它抛 QoderError 会把好账号记成失败。"""
        a = self.make_account("a", region="intl", models=["qwen3.8-flash"])
        b = self.make_account("b", region="cn")
        pool = self.make_pool([a, b])
        self.assertEqual([x.name for x in pool.candidates("deepseek-v4-pro")], ["b"])
        self.assertEqual([x.name for x in pool.candidates("qwen3.8-flash")], ["a", "b"])
        self.assertIn("没有模型", pool.skip_reasons("deepseek-v4-pro")["a"])

    def test_missing_credentials_account_is_skipped(self):
        good = self.make_account("good")
        broken = AccountConfig(name="broken", region="cn", config_dir=self.root / "nope")
        pool = self.make_pool([broken, good])
        self.assertEqual([x.name for x in pool.candidates()], ["good"])
        self.assertIn("没找到凭据文件", pool.skip_reasons()["broken"])

    def test_expired_token_is_skipped_without_network(self):
        fresh = self.make_account("fresh")
        stale = self.make_account("stale", expires_at=time.time() - 600)
        pool = self.make_pool([stale, fresh])
        self.assertEqual([x.name for x in pool.candidates()], ["fresh"])
        self.assertIn("过期", pool.skip_reasons()["stale"])

    def test_fallback_probes_earliest_when_all_cooling(self):
        a, b = self.make_account("a"), self.make_account("b")
        pool = self.make_pool([a, b])
        pool.record_failure("a", "timeout")            # 冷却 base
        pool.record_failure("b", "timeout")
        pool.record_failure("b", "timeout")            # 连败两次 → 冷却到上限，比 a 晚
        self.assertEqual(pool.candidates(), [])
        self.assertEqual(pool.fallback().name, "a")

    def test_timeout_for_suspect_is_short(self):
        a = self.make_account("a")
        pool = self.make_pool([a], first_byte_timeout=30, suspect_timeout=10)
        self.assertEqual(pool.timeout_for(a), 30)
        pool.record_failure("a", "timeout")
        self.assertEqual(pool.timeout_for(a), 10)


class CooldownTest(PoolFixture):
    def test_escalation_and_cap(self):
        a = self.make_account("a")
        pool = self.make_pool([a], cooldown_base=120, max_cooldown=1800, dry_after_fails=2)
        now = time.time()
        pool.record_failure("a", "timeout")
        self.assertAlmostEqual(pool._state["a"].cooldown_until - now, 120, delta=5)
        pool.record_failure("a", "timeout")            # 连续两次 = 当"这号今天废了"
        self.assertAlmostEqual(pool._state["a"].cooldown_until - now, 1800, delta=5)
        pool.record_failure("a", "timeout")            # 继续失败还是上限，不无限涨
        self.assertAlmostEqual(pool._state["a"].cooldown_until - now, 1800, delta=5)

    def test_auth_goes_straight_to_cap(self):
        a = self.make_account("a")
        pool = self.make_pool([a], max_cooldown=1800)
        pool.record_failure("a", "auth", "401")
        self.assertAlmostEqual(pool._state["a"].cooldown_until - time.time(), 1800, delta=5)

    def test_empty_does_not_cool(self):
        """空流回得快，也不能证明这个号废了 —— 不该罚它冷却。"""
        a = self.make_account("a")
        pool = self.make_pool([a])
        pool.record_failure("a", "empty", "空流")
        self.assertEqual(pool._state["a"].cooldown_until, 0.0)
        self.assertEqual([x.name for x in pool.candidates()], ["a"])

    def test_success_clears_everything(self):
        a = self.make_account("a")
        pool = self.make_pool([a])
        pool.record_failure("a", "timeout")
        pool.record_failure("a", "timeout")
        pool.record_success("a")
        self.assertEqual(pool._state["a"].streak, 0)
        self.assertEqual(pool._state["a"].cooldown_until, 0.0)
        self.assertEqual(pool._state["a"].ok, 1)

    def test_clear_cooldown_manual(self):
        a, b = self.make_account("a"), self.make_account("b")
        pool = self.make_pool([a, b])
        pool.record_failure("a", "timeout")
        pool.record_failure("b", "timeout")
        self.assertEqual(pool.clear_cooldown("a"), ["a"])
        self.assertEqual([x.name for x in pool.candidates()], ["a"])

    def test_relogin_mtime_heals_the_account(self):
        """★ 自愈：`.auth/user` 被重写过 = 重新登录过 → 之前的失败结论失效。"""
        a = self.make_account("a")
        pool = self.make_pool([a])
        pool.candidates()                              # 先让它记下 mtime
        pool.record_failure("a", "timeout")
        pool.record_failure("a", "timeout")
        self.assertEqual(pool.candidates(), [])
        user_file = a.auth_dir / "user"
        os.utime(user_file, (time.time() + 5, time.time() + 5))   # 模拟重新登录
        self.assertEqual([x.name for x in pool.candidates()], ["a"])
        self.assertEqual(pool._state["a"].streak, 0)


class PersistenceTest(PoolFixture):
    def test_state_survives_restart(self):
        """★ 重启不能把撞了上限的账号又挑回来。"""
        a = self.make_account("a")
        pool = self.make_pool([a])
        pool.candidates()
        pool.record_failure("a", "timeout")
        pool.record_failure("a", "timeout")

        revived = self.make_pool([a])
        self.assertEqual(revived.candidates(), [])
        self.assertEqual(revived._state["a"].streak, 2)
        self.assertIn("timeout", revived._state["a"].last_error)

    def test_state_file_is_valid_json_and_has_no_tmp_leftover(self):
        a = self.make_account("a")
        pool = self.make_pool([a])
        pool.record_failure("a", "timeout")
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["accounts"]["a"]["streak"], 1)
        leftovers = list(self.state_path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_unknown_account_in_state_file_is_dropped(self):
        self.state_path.write_text(json.dumps({
            "version": 1,
            "accounts": {"ghost": {"streak": 9}, "a": {"streak": 1, "cooldown_until": time.time() + 60}},
        }), encoding="utf-8")
        a = self.make_account("a")
        pool = self.make_pool([a])
        self.assertEqual(list(pool._state), ["a"])
        self.assertEqual(pool._state["a"].streak, 1)

    def test_broken_state_file_does_not_crash(self):
        self.state_path.write_text("{ 这不是 json", encoding="utf-8")
        a = self.make_account("a")
        pool = self.make_pool([a])
        self.assertEqual(len(pool.candidates()), 1)


class VerifyTest(PoolFixture):
    def test_same_directory_twice_is_warned(self):
        config_dir = self.root / "shared"
        write_auth(config_dir, uid="uid-x")
        pool = self.make_pool([
            AccountConfig(name="one", region="cn", config_dir=config_dir),
            AccountConfig(name="two", region="cn", config_dir=config_dir),
        ])
        warnings = pool.verify()
        self.assertTrue(any("同一个凭据目录" in w for w in warnings), warnings)

    def test_same_uid_twice_is_warned(self):
        """★ 两个目录、同一个登录 —— 看着是两个候选，其实是一个，failover 毫无意义。"""
        pool = self.make_pool([
            self.make_account("one", uid="uid-same"),
            self.make_account("two", uid="uid-same"),
        ])
        warnings = pool.verify()
        self.assertTrue(any("同一个登录" in w for w in warnings), warnings)

    def test_missing_credentials_is_warned(self):
        pool = self.make_pool([AccountConfig(name="nope", region="cn", config_dir=self.root / "nope")])
        self.assertTrue(any("不可用" in w for w in pool.verify()))


class SnapshotTest(PoolFixture):
    def test_snapshot_reports_each_account(self):
        a = self.make_account("a")
        pool = self.make_pool([a])
        pool.record_failure("a", "timeout", "45 秒没吐字节")
        row = pool.snapshot()["a"]
        self.assertTrue(row["cooling"])
        self.assertGreater(row["cooldown_seconds"], 0)
        self.assertIn("45 秒", row["last_error"])
        self.assertEqual(row["credentials"]["uid"], "uid-a")
        self.assertIsNone(row["problem"])

    def test_all_models_is_union(self):
        a = self.make_account("a", region="intl")
        b = self.make_account("b", region="cn")
        pool = self.make_pool([a, b])
        models = pool.all_models()
        self.assertIn("qwen3.8-flash", models)
        self.assertIn("glm-5.3", models, "CN 的模型要在并集里")
        self.assertEqual(len(models), len(set(models)), "并集不能有重复")


class BuildPoolTest(unittest.TestCase):
    def test_backward_compatible_without_accounts(self):
        """不配 accounts 时行为与改造前一致：一个账号，region 说了算。"""
        pool = build_pool({"region": "cn"})
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool.accounts[0].name, "cn")
        self.assertEqual(pool.accounts[0].auth_dir, Path.home() / ".qoder-cn" / ".auth")

    def test_accounts_from_config(self):
        pool = build_pool({
            "accounts": [
                {"name": "cn-main", "region": "cn", "config_dir": "~/.qoder-cn"},
                {"name": "intl", "region": "intl", "config_dir": "~/.qoder", "models": ["qwen3.8-flash"]},
            ],
            "failover": {"first_byte_timeout": 12, "max_cooldown": 60},
        })
        self.assertEqual([a.name for a in pool.accounts], ["cn-main", "intl"])
        self.assertEqual(pool.first_byte_timeout, 12)
        self.assertEqual(pool.max_cooldown, 60)
        self.assertEqual(pool.accounts[1].model_list(), ["qwen3.8-flash"])

    def test_duplicate_names_rejected(self):
        with self.assertRaises(SystemExit):
            build_pool({"accounts": [
                {"name": "same", "region": "cn", "config_dir": "/tmp/a"},
                {"name": "same", "region": "cn", "config_dir": "/tmp/b"},
            ]})

    def test_missing_field_rejected(self):
        with self.assertRaises(SystemExit):
            build_pool({"accounts": [{"name": "x", "region": "cn"}]})


if __name__ == "__main__":
    unittest.main()
