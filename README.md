# 小鲸 2.0

一个会话、一直在转的 QQ 陪伴。现在做到**第 2 步**：有人找她时被叫醒，群里的普通消息攒一会儿再看，没人找她也会按心跳自己醒，夜里睡觉。

## 现在有什么

- **连 QQ**：走 OneBot v11 的 HTTP（NapCat 或 SnowLuma 的 HTTP 服务器 + HTTP 上报）
- **事件库**：`data/events.db`，所有消息永久保存，能全文检索
- **叫醒规则**：私聊、被 @ 马上醒；群里没 @ 她的消息攒 30 秒再叫，同一个群最快 3 分钟叫一次；正在想的时候不打断，想完再看
- **心跳和作息**：没人找她，按她自己 `rest` 定的分钟数醒（默认 30 分钟）；00:30–08:30 睡觉，只有你私聊能叫醒她，其他消息攒到早上
- **唯一会话**：`data/session.jsonl`；上一次调用的输入超过 6.4 万 token，就把最旧的三分之一圈整块移出，全文一直留在 `session-archive.jsonl`
- **工具**：`inbox` 看收件箱、`open` 看对话、`scroll` 往上翻、`search` 搜记录、`send` 发消息、`my_log` 翻自己哪天想过什么、`rest` 定下次几分钟醒、`sleep` 去睡觉
- **闸门**：先 open 才能 send、允许名单、每分钟发言上限、一条最多 500 字、暂停
- **花费**：每次调用模型都记账，管理台显示今天花了多少、缓存命中率
- **管理台**：http://127.0.0.1:5310/

只用 Python 自带的库，**不用装任何东西**。需要 Python 3.11 以上。

## 第一次跑起来

### 1. 给测试号准备 SnowLuma（手动）

SnowLuma 的条款不允许用脚本部署，这一步只能你自己来。

1. 单独开一个 SnowLuma，登录**测试 QQ 号**。别用小鲸现在那个号，不然新旧两个会抢着回。
2. 在 SnowLuma 面板里给这个号打开两样：
   - **HTTP 服务器**：`127.0.0.1`，端口 `3020`，设一个 access token
   - **HTTP 上报**（HTTP 客户端）：地址 `http://127.0.0.1:5311/`，设一个 secret
   - 消息格式选 array（string 也能用）
3. 把测试号拉进测试群。

端口别用 4300–4310，QQ 自己会占这一段。

### 2. 改配置

把 `config.example.toml` 复制一份，改名成 `config.toml`，至少填这几项：

- `gate.owner_qq`：你的 QQ 号（私聊发「暂停」「继续」只认这个号）
- `gate.allow_groups`：测试群号
- `gate.allow_users`：允许私聊她的号，至少填上你自己
- `llm.model`：DeepSeek 的模型名；价格按你用的模型改

### 3. 设置密钥

密钥只放环境变量，不写进任何文件。在 Windows 里搜「编辑账户的环境变量」，新建这三个：

| 变量名 | 填什么 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek 的 API key |
| `ONEBOT_TOKEN` | 上面 HTTP 服务器设的 access token |
| `ONEBOT_SECRET` | 上面 HTTP 上报设的 secret |

用图形界面设，不要在终端里敲 `setx`：终端会把命令连同密钥记进历史文件。设完要**重新开一个终端**才生效。

### 4. 启动

```
cd F:\bot\xiaojing-v2
python -m xiaojing
```

看到「管理台：http://127.0.0.1:5310/」就好了。去测试群 @ 测试号试试。

### 5. 管理台

- 打开 http://127.0.0.1:5310/ ，口令在 `data/console-token.txt`
- 只能在这台电脑上打开。想在手机上看，用 Tailscale 这类私有网络，别暴露到公网
- **急停**：管理台的「暂停」按钮，或者用你的 QQ 私聊测试号发「暂停」，发「继续」恢复
- SnowLuma 只能看状态，不能从这里启动（同样是条款的原因）

想先看看长什么样、不连 QQ：运行 `python -m tests.demo_console`，打开 http://127.0.0.1:5319/ ，口令 `demo`。

## 跑测试

```
python -m unittest discover -s tests -t .
```

全部用假 QQ 和假模型，不连真 QQ，不花钱。

## 文件

| 文件 | 管什么 |
|---|---|
| `xiaojing/agent.py` | 唯一会话、一圈怎么转、两个工具 |
| `xiaojing/gate.py` | 闸门 |
| `xiaojing/onebot.py` | 连 QQ |
| `xiaojing/store.py` | 事件库、花费、管理台动态 |
| `xiaojing/console.py`、`console.html` | 管理台 |
| `prompts/system.md` | 框架规矩：她怎么运转 |
| `persona.md` | 人设（临时版，等你改） |
| `corpus/` | 语料模板 |

## 还没做的

按动工顺序：

3. 记忆
4. 好奇：念头和思绪板
5. 语气：按场景挑语料
6. 子代理和信息源
7. 影子模式，然后上线
