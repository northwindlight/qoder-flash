"""账号池：多账号的挑选、冷却状态机、状态落盘。

**为什么需要它**：一个账号撞上 Qoder 的每日请求上限之后，上游**不返回 429/4xx**——
它会立刻回 `200 OK` 加响应头（实测 0.4 秒），然后**一个字节都不发**，直到约 60 秒被对端掐断
（`httpx.RemoteProtocolError`），或者干脆挂到读超时。实测健康账号的首字节是 **0.9 秒**。
所以"这个号还能不能用"**不能靠状态码判断**，只能靠首字节超时 / 中途断流 —— 判定与冷却就落在这一层。

这一层是**纯同步**的：不 import asyncio、不 import FastAPI、不发网络请求，
只读凭据文件（`.auth/`）和它自己的状态文件。异步的探测与切换在 `failover.py`。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from qoder import (
    REGION_MODELS,
    Credentials,
    QoderError,
    credential_summary,
    load_credentials,
    resolve_model_key,
    resolve_region,
)

# 失败类别。判据永远是"连接层发生了什么"，不是状态码。
#   timeout    首字节超时（上游收下了请求但一个字都不吐 —— 限流就是这个样子）
#   disconnect 中途被对端掐断（RemoteProtocolError / 连接层错误 / OSError）
#   auth       401/403，凭据死了，要重新登录
#   http       其他上游错误（5xx 等）
#   credential 本地凭据本身有问题（缺失、解密失败、已过期）—— 没发过请求
#   empty      干净的空流：连上了、正常结束、但一个字都没有
#   model      这个账号发不了这个模型（配置问题，不是账号的错）
KINDS = ("timeout", "disconnect", "auth", "http", "credential", "empty", "model")

# 不进冷却的类别：这些要么回得快、要么压根不是账号的错，罚它冷却只会误伤
NO_COOLDOWN_KINDS = ("empty", "model")


def classify(exc: BaseException) -> str:
    """把异常归到失败类别。

    ★ 顺序有讲究：`RemoteProtocolError` 和 `TimeoutException` 都是 `httpx.HTTPError` 的子类，
      先判具体、再判笼统；`QoderError` 只带了一个字符串状态码，只能翻文本。
    """
    if isinstance(exc, TimeoutError):            # 3.11 起 asyncio.TimeoutError 就是内建 TimeoutError
        return "timeout"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "disconnect"
    if isinstance(exc, httpx.HTTPStatusError):
        return "auth" if exc.response.status_code in (401, 403) else "http"
    if isinstance(exc, QoderError):
        text = str(exc)
        if "401" in text or "403" in text or "凭据被拒" in text:
            return "auth"
        return "http"
    if isinstance(exc, (httpx.HTTPError, OSError)):
        return "disconnect"
    return "http"


# ---------------------------------------------------------------------------
# 账号配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountConfig:
    """一个账号 = 一个配置目录（凭据在它下面的 `.auth/`）。

    配置目录就是 CLI 自己的 `config_dir`：`QODERCN_CONFIG_DIR=<目录> qoderclicn login` 登出来的
    就是一整份独立登录态，所以"一个账号一个目录"两边通用。
    """

    name: str
    region: str
    config_dir: Path
    models: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_dir", Path(str(self.config_dir)).expanduser())
        if self.models is not None:
            object.__setattr__(self, "models", tuple(self.models))

    @property
    def auth_dir(self) -> Path:
        return self.config_dir / ".auth"

    def model_list(self) -> list[str]:
        """这个账号能用的模型。显式配了就用配的，否则跟随 region。"""
        if self.models:
            return list(self.models)
        return list(REGION_MODELS.get(self.region, []))

    def supports(self, model: str | None) -> bool:
        """这个账号能不能发这个模型。

        ★ 复用 `resolve_model_key` / `model_list` 判，不另起一张表：模型名与内部 key 的换算口径
          全项目只有那一处。**必须在挑选阶段就过滤掉**，否则不支持的模型会让 `resolve_model_key`
          抛错，被记成"这个账号失败了"——好账号就这么被打进冷宫。
        """
        if not model:
            return True
        try:
            wanted = resolve_model_key(model, self.region)
            allowed = {resolve_model_key(m, self.region) for m in self.model_list()}
        except QoderError:
            return False
        return wanted in allowed

    @classmethod
    def from_region(cls, region: str) -> "AccountConfig":
        """没配 `accounts` 时，拿 region 合成一个账号 —— 完全复刻旧的单区行为。"""
        cfg = resolve_region(region)
        return cls(name=cfg["name"], region=cfg["name"], config_dir=Path(cfg["auth_dir"]).parent)


# ---------------------------------------------------------------------------
# 账号状态
# ---------------------------------------------------------------------------


@dataclass
class AccountState:
    name: str
    streak: int = 0                 # 连续失败次数（成功即清零）
    cooldown_until: float = 0.0     # 到这个 unix 时刻为止不再挑它
    last_error: str = ""
    last_fail: float = 0.0
    last_ok: float = 0.0
    ok: int = 0
    fail: int = 0
    auth_mtime: float = -1.0        # `.auth/user` 的 mtime：变了 = 重新登录过

    def to_json(self) -> dict[str, Any]:
        return {
            "streak": self.streak, "cooldown_until": self.cooldown_until,
            "last_error": self.last_error, "last_fail": self.last_fail, "last_ok": self.last_ok,
            "ok": self.ok, "fail": self.fail, "auth_mtime": self.auth_mtime,
        }

    @classmethod
    def from_json(cls, name: str, raw: dict[str, Any]) -> "AccountState":
        state = cls(name=name)
        for key in ("streak", "ok", "fail"):
            try:
                setattr(state, key, int(raw.get(key) or 0))
            except (TypeError, ValueError):
                pass
        for key in ("cooldown_until", "last_fail", "last_ok", "auth_mtime"):
            try:
                setattr(state, key, float(raw.get(key) or 0.0))
            except (TypeError, ValueError):
                pass
        state.last_error = str(raw.get("last_error") or "")[:300]
        return state


# ---------------------------------------------------------------------------
# 池
# ---------------------------------------------------------------------------


class AccountPool:
    """按配置顺序挑账号；挂了就冷却，成功就清零。

    阈值都是"能推出来的"，不是试出来的：
      - 首字节超时 30 秒：健康首字节实测 0.9 秒，挂掉时 45~90 秒都不吐字节，中间有 30 倍的空当。
      - 可疑账号用 10 秒探：一个已经失败过的账号，"还能不能用"只看它吐不吐首字节，
        而健康的那 0.9 秒远小于 10 秒，所以这个短超时不会误杀，却把再探一次的成本压到 10 秒。
      - 冷却上限 30 分钟：上游是**每天**限流（用户实测：credits 没烧完也会撞，且不提示），
        但没说是固定日切还是滚动 24 小时。30 分钟封顶对两种模型都成立 —— 恢复后最多 30 分钟被发现，
        代价只是每 30 分钟多一次 10 秒的探针。
    """

    def __init__(
        self,
        accounts: list[AccountConfig],
        *,
        first_byte_timeout: float = 30.0,
        suspect_timeout: float = 10.0,
        cooldown_base: float = 120.0,
        max_cooldown: float = 1800.0,
        dry_after_fails: int = 2,
        total_budget: float = 45.0,
        state_path: Path | None = None,
    ) -> None:
        self._accounts = list(accounts)
        self.first_byte_timeout = float(first_byte_timeout)
        self.suspect_timeout = float(suspect_timeout)
        self.cooldown_base = float(cooldown_base)
        self.max_cooldown = float(max_cooldown)
        self.dry_after_fails = max(1, int(dry_after_fails))
        self.total_budget = float(total_budget)
        self.state_path = Path(state_path) if state_path else None

        # ★ 单 worker 假设：uvicorn 单进程多协程，临界区里没有 await，用 threading.Lock 就够
        #   （与 main.py 的 _USAGE_LOCK 同风格）。要开多 worker 得先换掉这里。
        self._lock = threading.Lock()
        self._state: dict[str, AccountState] = {a.name: AccountState(name=a.name) for a in self._accounts}
        self._creds: dict[str, tuple[float, Credentials | None, str]] = {}   # name -> (mtime, 凭据, 原因)
        self.load_state()

    # ---- 基本属性 ----

    @property
    def accounts(self) -> list[AccountConfig]:
        return list(self._accounts)

    def __len__(self) -> int:
        return len(self._accounts)

    def all_models(self) -> list[str]:
        """所有账号模型表的并集（`/models` 用），保持首次出现的顺序。"""
        seen: list[str] = []
        for acct in self._accounts:
            for model in acct.model_list():
                if model not in seen:
                    seen.append(model)
        return seen

    # ---- 凭据 ----

    def _credentials(self, acct: AccountConfig) -> tuple[Credentials | None, str]:
        """读（并缓存）一个账号的凭据，返回 (凭据, 失败原因)。

        按 `.auth/user` 的 mtime 缓存：没重新登录就不重复解密。解密失败一律吞成"原因"，
        因为调用方（挑账号、/health）都不该被一个坏账号带崩。
        """
        path = acct.auth_dir / "user"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None, f"没找到凭据文件 {path}（先在该目录登录一次）"
        with self._lock:
            cached = self._creds.get(acct.name)
            if cached and cached[0] == mtime:
                return cached[1], cached[2]
        try:
            cred: Credentials | None = load_credentials(auth_dir=acct.auth_dir)
            problem = ""
        except Exception as exc:                     # 解密/解析怎么坏都算"本地不可用"，不是账号的错
            cred, problem = None, f"凭据读取失败：{type(exc).__name__}: {exc}"
        with self._lock:
            self._creds[acct.name] = (mtime, cred, problem)
        return cred, problem

    def credentials_of(self, acct: AccountConfig) -> Credentials | None:
        return self._credentials(acct)[0]

    def _unusable(self, acct: AccountConfig) -> str:
        """这个账号现在能不能发请求；不能的话给一个能读的原因（空串 = 能用）。"""
        cred, problem = self._credentials(acct)
        if problem:
            return problem
        if cred and cred.expires_at and cred.expires_at < time.time():
            stamp = time.strftime("%Y-%m-%d", time.localtime(cred.expires_at))
            return f"凭据已在 {stamp} 过期，重新登录一次（token 不自动刷新）"
        return ""

    # ---- 选择 ----

    def _heal(self) -> list[str]:
        """★ 自愈：`.auth/user` 的 mtime 变了 = 这个账号重新登录过 → 清掉连续失败并解冻。

        这是**纯逻辑可推**的一条：凭据都换了一份，之前"这个号挂了"的结论自然失效。
        """
        healed = []
        for acct in self._accounts:
            try:
                mtime = (acct.auth_dir / "user").stat().st_mtime
            except OSError:
                continue
            with self._lock:
                state = self._state[acct.name]
                if state.auth_mtime < 0:
                    state.auth_mtime = mtime
                elif mtime != state.auth_mtime:
                    state.auth_mtime = mtime
                    if state.streak or state.cooldown_until:
                        state.streak = 0
                        state.cooldown_until = 0.0
                        state.last_error = ""
                        healed.append(acct.name)
        if healed:
            self.save()
        return healed

    def candidates(self, model: str | None = None, affinity: int | None = None) -> list[AccountConfig]:
        """可用的账号，按配置顺序。跳过：冷却中的 / 不支持这个模型的 / 本地凭据不可用的。

        `affinity` 是调用方给的**对话指纹**：给了就把它落到的账号提到最前（粘性绑定），
        这样同一段对话始终打同一个账号——上游前缀缓存按账号隔离，随便换号会把缓存打散。
        其余账号仍按原顺序跟在后面，失败切换照旧。
        """
        now = time.time()
        self._heal()
        with self._lock:
            picked = []
            for acct in self._accounts:
                state = self._state[acct.name]
                if state.cooldown_until > now:
                    continue
                if not acct.supports(model):
                    continue
                picked.append(acct)
        usable = [a for a in picked if not self._unusable(a)]
        if affinity is not None and len(usable) > 1:
            i = affinity % len(usable)
            usable = usable[i:] + usable[:i]
        return usable

    def fallback(self, model: str | None = None) -> AccountConfig | None:
        """**所有账号都在冷却时**的探针：只放最早解冻的那一个。

        比"拒绝一切请求"强的地方在于：上游一恢复就能自己发现，不用等人手动解冻。
        只放一个是为了不把 N 个账号 × 超时串起来，撑爆客户端的读超时。
        """
        now = time.time()
        with self._lock:
            best: AccountConfig | None = None
            for acct in self._accounts:
                if not acct.supports(model):
                    continue
                if best is None or self._state[acct.name].cooldown_until < self._state[best.name].cooldown_until:
                    best = acct
        if best is None or self._unusable(best):
            return None
        return best

    def timeout_for(self, acct: AccountConfig) -> float:
        """这个账号这次该等多久首字节：没失败过给正常值，失败过给短值。"""
        with self._lock:
            streak = self._state[acct.name].streak
        return self.suspect_timeout if streak >= 1 else self.first_byte_timeout

    def skip_reasons(self, model: str | None = None) -> dict[str, str]:
        """每个账号为什么没被选中（给报错信息和 /health 用）。"""
        now = time.time()
        out: dict[str, str] = {}
        for acct in self._accounts:
            with self._lock:
                state = self._state[acct.name]
                cooldown = state.cooldown_until - now
                streak, last_error = state.streak, state.last_error
            if cooldown > 0:
                out[acct.name] = f"冷却中（还有 {int(cooldown)} 秒）" + (f"，上次：{last_error}" if last_error else "")
                continue
            if not acct.supports(model):
                out[acct.name] = f"这个账号没有模型 {model}"
                continue
            problem = self._unusable(acct)
            if problem:
                out[acct.name] = problem
        return out

    # ---- 记账 ----

    def record_success(self, name: str) -> None:
        with self._lock:
            state = self._state.get(name)
            if state is None:
                return
            state.ok += 1
            state.last_ok = time.time()
            state.streak = 0
            state.cooldown_until = 0.0
            state.last_error = ""
            self._save_locked()

    def record_failure(self, name: str, kind: str, detail: str = "") -> None:
        with self._lock:
            state = self._state.get(name)
            if state is None:
                return
            now = time.time()
            state.fail += 1
            state.last_error = f"{kind}: {detail}"[:300]
            state.last_fail = now
            if kind in NO_COOLDOWN_KINDS:
                # 不计入连续失败：`timeout_for` 靠 streak 决定"要不要用短超时探"，
                # 而"空流/模型不支持"都不是"这个号吐不出首字节"
                self._save_locked()
                return
            state.streak += 1
            seconds = self._cooldown(state.streak, kind)
            state.cooldown_until = (now + seconds) if seconds else 0.0
            self._save_locked()

    def _cooldown(self, streak: int, kind: str) -> float:
        """冷却多久。

        - `auth`：直接给上限 —— 凭据死了，等到天荒地老也不会自己好，只能重新登录（登录会触发 `_heal`）。
        - 连续失败到 `dry_after_fails`：也直接给上限，不再逐级爬 —— 上游的限流是按天的，
          逐级爬只会在"已经确定撞墙了"之后多试探好几次。
        """
        if kind in NO_COOLDOWN_KINDS:
            return 0.0
        if kind == "auth" or streak >= self.dry_after_fails:
            return self.max_cooldown
        return min(self.cooldown_base, self.max_cooldown)

    def clear_cooldown(self, name: str | None = None) -> list[str]:
        """手动解冻（你觉得上游该重置了 / 刚重新登录过）。返回被清的账号名。"""
        cleared = []
        with self._lock:
            for acct in self._accounts:
                if name is not None and acct.name != name:
                    continue
                state = self._state[acct.name]
                if state.cooldown_until or state.streak:
                    state.cooldown_until = 0.0
                    state.streak = 0
                    state.last_error = ""
                    cleared.append(acct.name)
            if cleared:
                self._save_locked()
        return cleared

    # ---- 观测 ----

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """给 `/health` 的每账号概览。**不做网络探测**——探活是客户端的事。"""
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        for acct in self._accounts:
            with self._lock:
                state = self._state[acct.name]
                row: dict[str, Any] = {
                    "region": acct.region,
                    "config_dir": str(acct.config_dir),
                    "models": acct.model_list(),
                    "ok": state.ok,
                    "fails": state.fail,
                    "fail_streak": state.streak,
                    "cooling": state.cooldown_until > now,
                    "cooldown_seconds": max(0, int(state.cooldown_until - now)),
                    "last_error": state.last_error or None,
                }
            cred, problem = self._credentials(acct)
            row["credentials"] = credential_summary(cred) if cred else None
            row["problem"] = problem or None
            out[acct.name] = row
        return out

    def verify(self) -> list[str]:
        """启动时自检，返回要打给人看的警告（不抛异常）。

        两个都指向同一个账号是最容易配出来的错：**"failover"到了同一个号**，
        看着有两个候选，其实是一个。所以既按路径去重，也按解密出来的 uid 去重。
        """
        warnings: list[str] = []
        seen_paths: dict[str, str] = {}
        seen_uids: dict[str, str] = {}
        for acct in self._accounts:
            try:
                real = str(acct.auth_dir.resolve())
            except OSError:
                real = str(acct.auth_dir)
            if real in seen_paths:
                warnings.append(f"账号 {acct.name} 与 {seen_paths[real]} 指向同一个凭据目录（{real}）")
            else:
                seen_paths[real] = acct.name
            cred, problem = self._credentials(acct)
            if problem:
                warnings.append(f"账号 {acct.name} 不可用：{problem}")
                continue
            if cred and cred.uid:
                if cred.uid in seen_uids:
                    warnings.append(f"账号 {acct.name} 与 {seen_uids[cred.uid]} 是同一个登录（uid 相同）")
                else:
                    seen_uids[cred.uid] = acct.name
        return warnings

    # ---- 落盘 ----

    def load_state(self) -> None:
        """从状态文件恢复冷却/失败计数。

        ★ 必须落盘的理由：撞了上限的账号要冷却几十分钟，进程一重启就忘掉的话，
          第一发请求又要等它超时一次。
        """
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[账号池] 状态文件读不出来，当作全新开始：{exc}", flush=True)
            return
        saved = raw.get("accounts") if isinstance(raw, dict) else None
        if not isinstance(saved, dict):
            return
        with self._lock:
            for name, row in saved.items():
                if name in self._state and isinstance(row, dict):
                    self._state[name] = AccountState.from_json(name, row)

    def _save_locked(self) -> None:
        """原子落盘。**调用方必须已经持有 `self._lock`。**

        写文件放在锁内：状态只有几百字节、`os.replace` 亚毫秒，比"锁外写"引出的丢更新简单得多。
        ★ 临时文件名必须唯一（带 pid + uuid）：固定名字在并发下必坏。
        """
        if self.state_path is None:
            return
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "accounts": {name: state.to_json() for name, state in self._state.items()},
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(
                f"{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:                        # 落盘失败不能影响转发
            print(f"[账号池] 状态落盘失败（不影响转发）：{exc}", flush=True)

    def save(self) -> None:
        with self._lock:
            self._save_locked()


# ---------------------------------------------------------------------------
# 从配置建池
# ---------------------------------------------------------------------------


def build_pool(config: dict[str, Any], *, state_path: Path | None = None) -> AccountPool:
    """从 `config.json` 的内容建池。

    **向后兼容**：没配 `accounts` 就用 `region` 合成一个账号 —— 行为和改造前完全一样。
    """
    raw_accounts = config.get("accounts")
    accounts: list[AccountConfig] = []
    if isinstance(raw_accounts, list) and raw_accounts:
        for index, item in enumerate(raw_accounts):
            if not isinstance(item, dict):
                raise SystemExit(f"config.json 的 accounts[{index}] 必须是一个对象")
            name = str(item.get("name") or "").strip()
            region = str(item.get("region") or "").strip()
            config_dir = str(item.get("config_dir") or "").strip()
            if not name or not region or not config_dir:
                raise SystemExit(f"config.json 的 accounts[{index}] 需要 name / region / config_dir 三个字段")
            models = item.get("models")
            accounts.append(AccountConfig(
                name=name,
                region=region,
                config_dir=Path(config_dir),
                models=tuple(str(m) for m in models) if isinstance(models, list) and models else None,
            ))
        names = [a.name for a in accounts]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise SystemExit(f"config.json 里账号重名：{'、'.join(sorted(duplicates))}")
    else:
        accounts.append(AccountConfig.from_region(str(config.get("region") or "intl").strip().lower()))

    failover = config.get("failover") if isinstance(config.get("failover"), dict) else {}
    return AccountPool(
        accounts,
        first_byte_timeout=float(failover.get("first_byte_timeout", 30)),
        suspect_timeout=float(failover.get("suspect_timeout", 10)),
        cooldown_base=float(failover.get("cooldown_base", 120)),
        max_cooldown=float(failover.get("max_cooldown", 1800)),
        dry_after_fails=int(failover.get("dry_after_fails", 2)),
        total_budget=float(failover.get("total_budget", 45)),
        state_path=state_path,
    )
