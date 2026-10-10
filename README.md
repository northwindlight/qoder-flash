# Qoder Gateway

把本机的 Qoder 登录态包成一个 OpenAI / DeepSeek 兼容接口，**可选模型**（Qwen / GLM / DeepSeek / Kimi …）。
Windows / Linux 通用（纯 Python，无平台相关代码）。

两个区都支持，用 `region` 配置切换：

| region | 端点 | 凭据目录 | 账号能用的模型 |
|---|---|---|---|
| `cn` | `gateway.qoder.com.cn` | `~/.qoder-cn/.auth/` | 一整套（Qwen 3.7/3.8 全系、GLM、DeepSeek、Kimi…） |
| `intl` | `api3.qoder.sh` | `~/.qoder/.auth/` | 免费号只有 `qwen3.8-flash` 可用（Max 会挂） |

## 它是怎么工作的

1. 读 CLI 登录后写下的两个文件：`machine_id`（明文）和 `user`（base64 的 AES-CBC 密文，
   密钥与 IV 都取 `machine_id` 前 16 字节）。**只读，不写、不改**，不会动到 CLI 自己的登录态。
2. 用这套凭据打 Qoder 的老版 SSE 端点 `…/algo/api/v2/service/pro/sse/agent_chat_generation`：
   鉴权是 COSY 签名头，包体要过一遍 Qoder 自定义 base64，**模型放在请求头 `X-Model-Key` 里**。
3. 把回来的流翻译成 OpenAI 的 `chat.completion.chunk`。

> 协议怎么逆出来的、走过哪些死路、版本更新后怎么重来 —— 见
> [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md)。

### ⚠️ 两个必须知道的坑

- **`model` 要传 key，不能传显示名。** 显示名（`GLM-5.3`）服务端不认，而且**不报错** ——
  它会静默回落到 `auto`，你收到的就是另一个模型。key 见下表。
- **回包里的 `model` 字段恒为 `"auto"`，不反映真实模型。** 别拿它判断你在用哪个模型，
  唯一的判据是换 key 之后行为/自报身份有没有变。

## 安装与运行

```bash
python -m venv .venv
# Linux/macOS
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
# Windows
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

默认监听 `127.0.0.1:5050`。前置条件是本机登录过对应区的 CLI：国际版 `qodercli login`（写 `~/.qoder/.auth/`），
CN 版 `qodercn login`（写 `~/.qoder-cn/.auth/`）。

## 接口

请求/响应形状按 DeepSeek 那套（DeepSeek 本身是 OpenAI 兼容，所以 OpenAI 客户端也能直接用）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat/completions` | DeepSeek 官方 base_url 形式 |
| POST | `/v1/chat/completions` | OpenAI SDK 形式 |
| GET | `/models`、`/v1/models` | 当前 region 可用的模型名 |
| GET | `/health` | 是否就绪 + region + 账号概览（详情需带 key） |

```bash
curl http://127.0.0.1:5050/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer <key>' \
  -d '{
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": false
      }'
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:5050/v1", api_key="<key>")
client.chat.completions.create(model="kimi-k3", messages=[{"role": "user", "content": "你好"}])
```

## 模型

`model` 传**左边**的名字（网关翻成右边的 key 发出去）。认不出的名字直接 400 报错并列出可用项 ——
**不会**静默回落。

| 模型名 | Qoder key | 自报身份（实测） |
|---|---|---|
| `qwen3.8-flash` | `qfmodel` | 通义千问 |
| `qwen3.8-max` | `qmodel_38max` | — |
| `qwen` | `qmodel` | 通义千问 |
| `glm-5.3` | `gmodel` | Z.ai |
| `deepseek-v4-pro` | `dmodel` | 深度求索 |
| `kimi-k3` | `kmodel_latest` | Moonshot AI |
| `auto` | `auto` | 服务端自选 |

也接受直接传 key（`"model": "gmodel"`）。**要加新模型**：跑一次官方 CLI 并抓 `X-Model-Key`：

```bash
BUN_OPTIONS="--preload /tmp/sniff-bun.js" qodercn -p hi -m "模型显示名"
grep -a -A20 agent_chat_generation /tmp/sniff-bun.log | grep -i x-model-key
```

（CN 版 CLI 是 Bun 编译的单文件二进制，Node 的 `--require` 钩子对它无效，得用 `BUN_OPTIONS=--preload`。）

## 思考强度

| 参数 | 作用 |
|---|---|
| `reasoning_effort` | `none` / `low` / `medium` / `high` / `xhigh` / `max` |
| `thinking: {"type": "enabled"}` | 只开不定档，等于 `medium`；`disabled` 等于 `none` |
| 都不给 | 默认 `none`（关思考最快） |

思维链走 DeepSeek 的 `reasoning_content` 字段回来（流式是逐块 delta）。

**但别指望它精确控深。** 实测：`none` 是唯一确定的（稳定 0 字思考、亚秒级）；`medium`/`high` 之间
**没有稳定顺序**（同一档连测三次，思考量差 5~20 倍，甚至 `high` 比 `medium` 还短）。把它当成
「开/关 + 粗略倾向」用，不要当成可调旋钮。

## 配置

两种方式，**环境变量优先于配置文件**。`config.json`（与 `main.py` 同目录，已 gitignore，建议 600）：

```json
{
  "api_key": "qf-...",
  "region": "cn",
  "host": "127.0.0.1",
  "port": 5050
}
```

| 键 / 环境变量 | 默认 | 说明 |
|---|---|---|
| `api_key` / `QODER_API_KEY` | 空 | 设了之后调用必须带 `Authorization: Bearer <key>` |
| `region` / `QODER_REGION` | `intl` | `cn` 或 `intl`，决定端点和凭据目录 |
| `host` / `QODER_HOST` | `127.0.0.1` | 监听地址 |
| `port` / `QODER_PORT` | `5050` | 端口（也可用 `--port`） |

```bash
python main.py --gen-key      # 生成/轮换 key 写入 config.json（600），然后重启服务
```

`/health` 不需要 key（探活用，只回 `{"ready":true}`）；**账号详情**（region/模型表/uid/姓名/是否过期）
要带对 key。

### 多账号（`accounts`）

配了 `accounts` 就有几个账号用几个，一个撞墙自动换下一个。**不配就还是单区**，行为与以前完全一样。

```json
{
  "api_key": "qf-...",
  "accounts": [
    {"name": "cn-main", "region": "cn",   "config_dir": "~/.qoder-cn"},
    {"name": "cn-old",  "region": "cn",   "config_dir": "~/.qoder-accounts/cn-old"},
    {"name": "intl",    "region": "intl", "config_dir": "~/.qoder"}
  ],
  "failover": {"first_byte_timeout": 30, "max_cooldown": 1800}
}
```

一个账号 = 一个配置目录（凭据在它下面的 `.auth/`）。**「一个账号一个目录」和 CLI 自己是同一件事**，
所以第二个账号这么登，互不影响：

```bash
QODERCN_CONFIG_DIR=$HOME/.qoder-accounts/cn-old qoderclicn login
# 等价： qoderclicn --config-dir ~/.qoder-accounts/cn-old login
# 顺手看一眼这个号能用哪些模型，填进上面的 models 字段：
QODERCN_CONFIG_DIR=$HOME/.qoder-accounts/cn-old qoderclicn --list-models
```

| 键（`failover`） | 默认 | 说明 |
|---|---|---|
| `first_byte_timeout` | `30` | 首字节超时（秒）。健康账号实测 0.9 秒，挂掉时 45~90 秒不吐字节 |
| `suspect_timeout` | `10` | 已经失败过的账号改用这个短超时再探（0.9 秒 ≪ 10 秒，不会误杀） |
| `cooldown_base` | `120` | 第一次失败冷却多久（秒） |
| `max_cooldown` | `1800` | 冷却上限（秒）。连续失败到 `dry_after_fails` 次就直接给上限 |
| `dry_after_fails` | `2` | 连续失败几次算"这个号今天废了" |
| `total_budget` | `45` | 一次请求在所有账号上总共最多耗多久（秒），防止把客户端的读超时串爆 |

每个账号还能加 `models: [...]` 覆盖 region 默认的模型表（比如国际版免费号只有 Flash）。

判活**不看状态码**，看的是"首字节来没来 / 流有没有被掐断"——原因见
[已知限制](#已知限制) 里的第一条。挂了就进冷却，成功就清零；`.auth/user` 被重写过
（= 重新登录过）会自动清掉失败记录，不用重启。

观测：`/health` 有每个账号的 uid/是否过期/冷却到几点/最近错误，`/stats` 的 `by_account`
看每个号各花了多少，台账每行都带 `account`。手动解冻（觉得上游该重置了）：

```bash
curl -X POST http://127.0.0.1:5050/admin/accounts/reset -H "Authorization: Bearer <key>"          # 全部
curl -X POST http://127.0.0.1:5050/admin/accounts/cn-old/reset -H "Authorization: Bearer <key>"    # 指定一个
```

## 已知限制

- **撞了每日请求上限时，上游不报错，只是"静默挂死"**：它照样回 `200 OK` 加响应头（实测 0.4 秒），
  然后**一个字节都不发**，直到约 60 秒被对端掐断（`RemoteProtocolError`）。所以**别用状态码判活**。
  另外 credits 没烧完也会撞这个上限，且上游不提示。多账号的自动切换就是冲着它做的（见上文）。
- **自动切换的收益被客户端的读超时封顶**：一次请求里第一个账号的失败要等满 `first_byte_timeout`
  才会换下一个。多数 SDK 的默认读超时是 60 秒，所以 `total_budget` 默认压到 45 秒 ——
  两个账号都是新鲜的挂账号时，客户端可能在切换完成前就先超时了。想再快只能调小
  `first_byte_timeout`，但那会开始误杀正常的慢请求。
- **不做 token 自动刷新**：过期后（`/health` 的 `token_expired: true`）重新登录一次即可。
  刻意不刷新是因为刷新会轮换掉 CLI 自己那份凭据，把 CLI 弄掉线。
- **GLM 会把思考混进正文**：`glm-5.3` 的思考过程直接写在 `content` 里（不是 `reasoning_content`），
  偶尔还会看到它自言自语。这是模型/服务端的怪癖，网关层改不了。
- **intl 免费号只有 Flash 能用**：`qwen3.8-max` 在那边会**挂住不返回**（不是报错，是超时）。
- 协议是逆向来的，Qoder 改服务端就可能失效；失效时先看 `/health` 和返回的错误详情。
