# 逆向记录：这个网关是怎么调通的

> 2026-09-20 · 对象：`@qoder-ai/qodercli` 1.1.58（国际版，Node bundle）与 CN 版 `qoderclicn` 1.1.58
> （Bun 编译的 aarch64 单文件二进制）· 平台：Raspberry Pi 5 / Debian 13
>
> 结论先说：**没有破解 TLS，也没有反混淆那个 33MB 的 bundle。** 真正解决问题的是在
> **客户端进程内部、加密发生之前的那一层**，把客户端自己成功发出的那条请求抄了下来。

---

## 0. 要解决的问题

目标很朴素：把本机 Qoder 的登录态变成一个 OpenAI/DeepSeek 兼容接口，跑 Qwen3.8-Flash。

难点不在写 HTTP 服务，而在**不知道请求该怎么构造**。这条路完全是黑盒：

- 有官方的 CLI 客户端（能正常用 Flash）
- 没有协议文档
- 客户端是 minify 过的单文件 bundle
- 抓包看到的是 TLS 密文

于是问题变成：**「模型名/模型 key 到底是什么」这个信息，存在在哪里？**

---

## 1. 先分清：代码 vs 数据

这是整件事里最先要做的判断，也是后面所有死路的根源。

| 信息 | 性质 | 能不能静态拿到 |
|---|---|---|
| 端点 URL、签名算法、包体字段名 | **代码** | 能（bundle 里 grep + 实测） |
| 模型 key（`qfmodel`） | **数据** | **不能**——运行时从服务端 catalog 拉 |
| catalog 缓存 | 数据 | 不能（缓存文件本身是加密的） |

模型 key 是数据。所以静态分析再久也拿不到它，唯一的办法是**在它出现明文的那一刻守株待兔**。
而它出现明文的地方，只有客户端自己发请求的那一瞬间。

---

## 2. 已知的两代协议

先把实测到的事实摆出来（后面所有推理都建立在这张表上）：

| | 老版（api3） | 新版（api2-v2） |
|---|---|---|
| 端点 | `api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation` | `api2-v2.qoder.sh/model/v1/chat/completions` |
| 鉴权 | `Authorization: Bearer COSY.<payload>.<md5>` + 一组 `cosy-*` 头 | 纯 `Authorization: Bearer dt-...` |
| 包体 | JSON → **Qoder 自定义 base64** | 裸 JSON（OpenAI 格式） |
| 模型怎么传 | 请求头 `X-Model-Key` + 包体里的 `model_config.key` | 包体 `model` 字段 |
| 免费账号（0 credits） | ✅ Flash 可用 | ❌ 只放行 `lite`，其余一律 `402 quota exceeded` |

关键的不对称：**能免费用到 Flash 的只有老版这条路**，而 CLI 走的正是它。
新版端点虽然更"现代"，但对免费账号是个死胡同。

---

## 3. 三条死路（照实记，这部分比结论有用）

### 3.1 黑盒猜 —— 只有一次「报错即答案」

拿一份现成的协议模板直接打 api3（裸 JSON），服务端回了：

```
event:error
data:{"stackTrace":[{"methodName":"decode0","className":"com.alibaba.force.ai.utils.CustomBase64Util$CustomBase64", ...
```

Java 堆栈把类名和方法名都给了：**包体在服务端是要过自定义 base64 解码的**，裸 JSON 连门都进不去。
这是全程唯一一次「错误信息直接把答案送上门」。补上编码后立刻变 200 —— 方向确认。

> 教训：错误信息是最便宜的 oracle。先花五分钟把服务的报错逼出来，比读一小时源码有用。

### 3.2 猜到模型名也没用 —— 响应里没有反馈信号

编码问题解决后能 200 了，但换哪个模型名（`qmodel_38flash`、`Qwen3.8-Flash`、catalog key……）
都不对：要么 402，要么 200 之后**一个字节的内容都不出**。

最要命的是：**回包里的 `model` 字段永远写着 `"auto"`**。

```
data:{"headers":{...},"body":"{\"choices\":[{\"delta\":{\"content\":\"...\"}}],\"model\":\"auto\",...}","statusCode":"OK"}
```

也就是说，服务端**不会告诉你它实际用了哪个模型**。哪怕请求成功了，你也无法从响应里判断
「我到底有没有用上 Flash」—— 这条路没有任何可优化的反馈信号。**这一步是最容易让人放弃的地方，
因为失败和成功看起来一模一样。**

### 3.3 静态分析 bundle —— 字段名能拿到，字段值拿不到

在 33MB 的 `qodercli.js` 里确实挖到了关键字段名：

- `chat_task: "FREE_INPUT"`、`agent_id: "agent_common"`
- `model_config` / `custom_model` 两个模型相关字段
- 传输层映射里有 `customModel: A.custom_model, modelConfig: A.model_config`
- 还有一处判断逻辑：某个 task 下会强制 `model: Zni, customModel: undefined, modelConfig.key: Zni, reasoningEffort: "none"`

**但 `Zni` 是运行时算出来的常量，模型 key 也一样 —— 值在数据里，不在代码里。**
到这里静态分析已经到顶了。（顺带：那 33MB 里 `agent_chat_generation` 只出现一次，还是在诊断日志代码里，
按字符串搜请求构造是搜不到的。）

---

## 4. 转折：不要「推导请求」，要「抄请求」

既然信息只在客户端手里出现一次明文，那就去那一层守着。

### 4.1 第一钩：挂错了层

用 `NODE_OPTIONS=--require` 注入一个钩子，先挂最常见的两个面：

```js
// 钩 globalThis.fetch / http.request / https.request
```

**只抓到了遥测（`api2.qoder.sh/otel/v1/logs`），真正那条 chat 请求影子都没有。**
原因：CLI 的 chat 走的是 bundle 内部自带的 undici 副本，根本不经过全局对象。

这个「失败」本身是有效信息：**说明钩错了层，得往下挪。**

### 4.2 往下挪一层：`net.Socket.prototype.write`

关键认知：**TLS 是在 socket 内部完成加密的**，所以任何数据在交给 `write()` 的那一刻**还是明文 HTTP 报文**。

```js
// sniff.cjs —— 用法：NODE_OPTIONS="--require /tmp/sniff.cjs" qodercli -p "hi" -m <模型>
const fs = require('fs');
const KEY = /agent_chat_generation|chat\/completions/;
function toStr(c){ try { return Buffer.isBuffer(c) ? c.toString('utf8') : String(c); } catch(e){ return null; } }
for (const mod of ['tls', 'net']) {
  const proto = require(mod)[mod === 'tls' ? 'TLSSocket' : 'Socket'].prototype;
  for (const fn of ['write', '_write']) {
    if (!proto[fn]) continue;
    const orig = proto[fn];
    proto[fn] = function(chunk, ...rest) {
      try {
        const s = toStr(chunk);
        if (s && KEY.test(s)) fs.appendFileSync('/tmp/sniff.log', JSON.stringify({tag: mod + '.' + fn, s: s.slice(0, 12000)}) + '\n');
      } catch(e){}
      return orig.apply(this, [chunk, ...rest]);
    };
  }
}
```

这一钩，请求头整条出来了 —— 包括那个找了半天的答案：

```
POST /algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1 HTTP/1.1
host: api3.qoder.sh
Authorization: Bearer COSY.<...>.<md5>
Cosy-Version: 1.1.58
Cosy-ClientType: 5
X-Model-Key: qfmodel          ← 就是它
X-Model-Source: system
content-length: 148260
```

### 4.3 包体为什么漏了、怎么补的

第一次只抓到头部。因为包体被重编码过，里面**没有任何可 grep 的明文关键词**，而我当时是按内容过滤的。

改成**按连接过滤**：看到请求行就在那条 socket 上打标记，之后所有写入原样落盘。

```js
if (s.includes('agent_chat_generation')) { marked.add(this); }
else if (marked.has(this)) { fs.appendFileSync('/tmp/sniff-body.bin', chunk); }
```

抓到的包体形态（未完全解开，见下）是一串逗号分隔的 ASCII 码，等价于对自定义 base64 又套了一层。

> **老实说：那个 148KB 的包体我没解到底。**
> 拿到 `X-Model-Key` 之后就不需要了 —— 我们自己的包体形状（来自老版模板）已经能跑通并出内容。
> 解码失败的细节记在这：按 `content-length` 切 148260 字节、把逗号分隔的数字转成字符后，
> 长度对 4 取余是 1（自定义 base64 应该是 4 的倍数），说明切片边界有偏移，没再往下查。

---

## 4.4 补记：CN 版是 Bun 二进制，Node 钩子无效

CN 版 CLI 是**单个 aarch64 ELF**（184MB，Bun 编译，内嵌 JS）。同一个 socket 钩子在这里完全不触发：
`NODE_OPTIONS=--require` 不生效（Bun 不认），`LD_PRELOAD` 也拦不到（BoringSSL 静态链接、
`SSLKEYLOGFILE` 同样被编译掉）。**能用的是 `BUN_OPTIONS=--preload`**：

```bash
BUN_OPTIONS="--preload /tmp/sniff-bun.js" qodercn -p hi -m "GLM-5.3"
# /tmp/sniff-bun.log 里就有明文请求头：X-Model-Key: gmodel
```

同一个钩子脚本（钩 `node:net` 的 `Socket.prototype.write`）在 Bun 下照样有效，因为明文同样要在
**加密之前**经过 socket 写入。

## 5. 拿到之后必须验证 —— 抄来的只是假设

抓到的 header 只是**假设**。真正的确认是重放：

1. 把 `qfmodel` 塞进我们自己的签名器 + 包体，打 api3 → **HTTP 200 且真出内容**。
2. 用 `lite` 作对照 → 也 200（服务端两条路都收，这解释了为什么单看响应分不出对错）。

### 5.1 顺带挖到的第二个大坑：`reasoning_effort` 必须显式传

矩阵实测（同一句「说三个字」）：

| 传参 | 耗时 |
|---|---|
| 完全不传 | 34.7s |
| `parameters.reasoning_effort = "none"` | **0.8s** |
| 顶层 `reasoningEffort = "none"` | 23.1s（无效，传错层了） |

六档 `none/low/medium/high/xhigh/max` 服务端全认。**这个参数不显式给，服务端默认长思考** ——
一句「说三个字」要 30 秒以上。

> **订正（后被推翻的一半）**：最初单次实测看起来"档位越高思考越长"，据此写过一张漂亮的表。
> 后来重复测发现**档位不精确控深**：同一档连测三次，思考量差 5~20 倍，`high` 甚至比 `medium` 还短。
> 唯一稳定的是 `none`（确定 0 字思考、亚秒级）。所以正确的说法是：
> **`reasoning_effort` 是个「开/关 + 粗略倾向」，不是可调旋钮。**
> 教训：单次样本 + 无重复 = 会得到看起来很有道理的错误结论。

关思考确实会答错（这条重复验证过）：

| effort | 耗时 | 思考字数 | 「9.11 和 9.9 哪个大」 |
|---|---|---|---|
| `none` | 1.2s | 0 | ❌ 答成 9.11 大 |
| `low` | 5.5s | 1235 | ✅ |

---

## 5.2 真正的坑（最贵的一条）：模型名必须发 key，发显示名会**静默回落**

这是整件事里最贵的教训，也是最容易得出错误结论的地方。

CLI 的 `--list-models` 给人看的是**显示名**（`GLM-5.3`、`Kimi-K3`…），而线上发出去的是**内部 key**
（`gmodel`、`kmodel_latest`…）。两者不是一回事：

| 你发的东西 | 服务端行为 |
|---|---|
| 正确的 key（`gmodel`） | 真的用 GLM |
| 显示名（`GLM-5.3`） | **不报错**，静默回落到 `auto` |
| 自己编的 key | 可能直接**挂住不返回**（不报错，等到超时） |

而且回包里的 `model` 字段恒为 `"auto"`，**永远不告诉你实际用了哪个模型**。两条叠加的结果是：

> 发错值 → 不报错 → 回包说 auto → 你以为"这服务就是只能 auto"。

我们一度就得出了「国际版根本不是 qwen3.8-flash、档位是假的」这个结论，直到用 **"你是哪家训练的模型"**
去问才知道：把 key 发对（`qfmodel`），国际版跑的确实就是 Qwen3.8-Flash；发 `gmodel` 会答「智谱/Z.ai」；
发 `kmodel_latest` 会答「Moonshot AI」—— **模型选择是真的，只是我们用错了参数**。

可用的判据只有一条：**换 key 之后，行为或自报身份有没有变**。别信回包字段，也别信延迟。

实测对照（同一句话问身份）：

| 发出的 key | 回答 |
|---|---|
| `qfmodel` | 通义千问 |
| `gmodel` | Z.ai（且思考过程直接泄进正文，这是 GLM 的怪癖） |
| `dmodel` | 深度求索 |
| `kmodel_latest` | Moonshot AI |

---

## 6. 版本更新后怎么重来一遍

Qoder 换个版本、改个协议，上面这套可能要重跑。顺序是固定的：

1. **先跑通官方客户端**（`qodercli` / `qodercn` 加 `-p "hi" -m <模型>`），确认它此刻是好的。
2. **挂钩子**把它的请求抄下来 —— 按客户端类型选：
   ```bash
   # 国际版（Node）
   NODE_OPTIONS="--require /tmp/sniff.cjs" qodercli -p "hi" -m Qwen3.8-Flash
   # CN 版（Bun 单文件二进制）
   BUN_OPTIONS="--preload /tmp/sniff-bun.js" qodercn -p "hi" -m "GLM-5.3"
   ```
   看 `X-Model-Key`、`Cosy-Version`、包体编码方式有没有变。
3. **重放验证**：用我们自己的签名器打一发，确认 200 且出内容。
4. **别忘了 `reasoning_effort`**：不给就等着 30 秒。

两个备选手段（这次没用上，但比钩 socket 更"重"也更通用）：

- `SSLKEYLOGFILE=/tmp/keys.log` + tcpdump + tshark：从**网线**上解密。要装 tshark，能对付非 Node 客户端。
- Frida / gdb：进程内注入。适合静态编译的原生二进制。

---

## 7. 这招什么时候不灵

- 客户端换成**静态编译的原生二进制**（Rust/Go，无 Node/V8）→ 钩不着 JS 层，得走 SSLKEYLOGFILE 或 Frida。
- 客户端做了**内存内自校验或字符串加密** → 钩子可能被检测或钩到的是密文。
- 服务端开始**校验机器指纹来源**（现在是随机的，服务端不校验）→ 这条路会收紧。
- 服务端把免费模型也挪到新版端点、或对新版端点放开 Flash → 那老版这条路就没必要了（对我们反而是好事）。

---

## 8. 边界

这套东西只做一件事：**把你自己账号的登录态，转成你本机的一个接口。**

- 凭据是 `qodercli login` 你自己登出来的，本程序**只读不写**，也不做 token 刷新
  （刷新会轮换掉 CLI 自己那份凭据，把 CLI 弄掉线）。
- 不做批量注册、不做多账号轮换、不绕过额度。使用的是账号本来就能免费用到的模型。
- 用的是官方 CLI 自己的协议行为，不是伪造客户端身份。
