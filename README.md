# claude-chatgpt-bridge

**在 Claude Code 里直接调用你 Mac 上的 ChatGPT 桌面版，让 ChatGPT Pro 审代码、Claude 核实和修改，循环到没问题为止。**

不走 API、不另外花钱，用的就是你已经付费的两个订阅。

> ⚠️ 非官方项目，与 OpenAI、Anthropic 无关。它通过 macOS 辅助功能模拟人工操作 ChatGPT 界面；OpenAI 的使用条款限制自动化获取输出，使用风险（包括账号风险）由你自己承担。请只在你自己的账号上、为你自己的工作使用。

---

## 适合谁

同时满足这三条：

| | |
|---|---|
| 🍎 **Mac** | macOS 15 及以上 |
| 🟢 **ChatGPT Pro 订阅**（Codex 20x 那一档） | 能在 ChatGPT 桌面版里用 Pro 模型 |
| 🟠 **Claude 订阅**，日常用 Claude Code 写代码 | |

你可能遇到过这种情况：Pro 审代码很强，能连续工作二三十分钟，在沙箱里写脚本复现问题；但它在网页 / App 里，没法直接改你的代码。Claude Code 在你的项目里，能读、能改、能跑测试。两边之间只能靠你手动打包、上传、复制结果、再贴回来。

这个项目把这一来一回自动化了。

## 你会得到什么

在 Claude Code 里说一句「用 ChatGPT 审一下 `web/api` 和 `src`」，接下来：

1. **Claude 打包代码**，自动排除 `.env`、私钥、`node_modules`、构建产物，先给你看打包摘要再上传；
2. **ChatGPT Pro 严格审查**：解压、扫描、沿调用链阅读、写复现脚本实际运行，三轮交叉复核去掉误报；
3. **Claude 逐条核实**：打开源码确认每条问题是否真实存在，标出「成立 / 不成立 / 存疑」，必要时调整严重级别；
4. **你拿到一份 HTML 报告**：要改的点一条一条列好，带位置、证据代码、修改建议、ChatGPT 怎么验证的、Claude 的核实意见；
5. **你选要改哪些**（或者说「自动」），Claude 修改并跑测试；
6. **送回同一个 ChatGPT 对话复审**：它核实修复是否到位、回应你不改的理由、再找新问题 → 新一份报告；
7. 循环，直到没有 medium 以上的问题。

> 实测：一个 192 个文件的项目，Pro 连续工作 21 分钟，报告 19 条问题，Claude 核实后全部成立，并把其中 4 条 critical 按实际触发条件下调了级别。

除了审代码，也可以单纯把一个问题交给 ChatGPT Pro 深度思考，再把完整回答拿回 Claude Code 里继续用。

---

## 快速开始

### 1. 准备（一次性）

**ChatGPT 桌面版**：安装并登录，在模型菜单里选好 Pro，窗口保持打开（不必在前台）。界面语言需要是 **English**。

**Peekaboo**：负责「看屏幕、点按钮」。

```bash
brew install openclaw/tap/peekaboo
```

打开 **Peekaboo.app**，在「系统设置 → 隐私与安全性」里给它 **屏幕录制** 和 **辅助功能** 权限。检查一下：

```bash
peekaboo permissions status --bridge-socket ~/Library/Application\ Support/Peekaboo/bridge.sock
```

三项都显示 `Granted` 就可以了。

### 2. 安装

```bash
git clone https://github.com/Furinaaa-Cancan/claude-chatgpt-bridge.git
cd claude-chatgpt-bridge
./install.sh          # 装到 ~/.claude/skills/；想跟着仓库更新就用 ./install.sh --link
```

只需要系统自带的 Python 3.9+，没有其他依赖。

### 3. 验证

```bash
python3 ~/.claude/skills/ask-chatgpt/chatgpt_bridge.py status
```

能看到当前 ChatGPT 窗口的标题、是否在生成等信息，就说明链路通了。这一步只读，不会操作界面。

---

## 怎么用

安装后 Claude Code 会自动识别两个 skill，直接说人话就行。

### 审代码

```text
用 ChatGPT 审一下这个项目，只看 web/api 和 src
/chatgpt-review ~/code/my-project
/chatgpt-review ~/code/my-project 自动          ← 每轮自动修所有核实成立的 medium 以上问题
```

流程中 Claude 会在这几个点停下来问你：

- **上传前**：给你看打包了哪些文件、排除了什么，确认后才上传；
- **每轮报告出来后**：问你要改哪些（自动模式除外）。

无论哪种模式，Claude 都不会自己 commit 或 push。

### 问问题

```text
/ask-chatgpt 帮我比较一下这两种缓存失效策略的优缺点：……
问问 ChatGPT Pro：这段 SQL 在 PostgreSQL 16 下会不会有锁升级问题？
```

### 运行时会发生什么

- **上传、发送、复制结果这几秒**，ChatGPT 会被切到前台、鼠标会动一下。这时候别点鼠标或打字，否则那一步可能失败（脚本会报错停下，不会乱点）。
- **中间的等待都在后台**，可能二三十分钟。你可以照常用电脑，甚至用 ChatGPT 的其他对话，脚本会等你空闲后再切回审查用的那个对话。
- **可以并行**：ChatGPT 本身能同时生成多个对话。多个任务（包括不同 Claude Code 会话发起的）各开各的对话、同时生成；只有点击、上传、发送、复制这几秒钟会互相让一下，不会抢着点。
- **不会打断你打字**：如果输入框里有你没发的草稿，它会等草稿消失 1 分钟后再动窗口。
- 用的是 ChatGPT 里**当前选中的模型**，审代码前记得选好 Pro。

### 报告长什么样

- 顶部：待修改数量、各严重级别计数、已修复数量
- Claude 的总结 + ChatGPT 的概述 + 覆盖情况（深读了多少文件、只扫描了多少）+ 对话上下文用量
- **要改的点**：按严重程度编号，每条有位置、问题、证据代码、修改建议、ChatGPT 的验证方式、Claude 的核实意见；可按级别筛选，可一键复制修改清单
- 折叠区：已修复（含 ChatGPT 对修复的核实）、不修改 / 存疑（含理由和 ChatGPT 的回应）
- 浅色 / 深色自适应，手机上也能看

---

## 注意事项

**上传了什么**：只上传你指定范围内的代码。以下内容自动排除，被排除的疑似密钥文件会在上传前列给你看：`.env*`（`.env.example` 除外）、私钥和证书、`credentials*.json` 之类的凭据文件、内容里含 API Key / Token 特征的文件、`node_modules` / 构建产物 / 缓存、二进制文件和超过 512 KB 的文件。

**存在哪里**：审查状态、每轮的 zip、提示词、ChatGPT 原始回答和报告都在 `~/.chatgpt-review/` 下；对话用量账本在 `~/.chatgpt-bridge/`。**审查本身不会往你的项目目录里写任何东西**；改代码只发生在你确认要改哪些之后，或者你开了「自动」模式。

**剪贴板**：取回答案要借用剪贴板，脚本会先备份、用完恢复。

**上下文**：每个 ChatGPT 对话都记一本用量账（中文约 1 字 1 token，故意估高；附件按文本量计、封顶 3 万，因为 Pro 是在沙箱里用工具处理 zip，不会整包读进上下文）。默认预算 9 万（官方公布的 128K 输入上限的七成），续问会超出时自动开新对话，并把之前的结论作为交接摘要带过去。可以用 `CHATGPT_BRIDGE_BUDGET`、`CHATGPT_BRIDGE_ATTACH_TOKENS` 调整。

## 已知限制

- **依赖 ChatGPT 界面**：按钮名（Send、Copy、New chat……）或布局一改就可能失效；目前只适配英文界面。在 ChatGPT 桌面版 155.x、Peekaboo 4.2.2、macOS 26.2 上测试。
- 只支持 macOS。多个任务可以并行生成，点击、发送、复制这几秒钟会排队。
- 只取回 ChatGPT 的文字回答；它在沙箱里生成的文件（比如它自己的报告、证据包）不会自动下载。
- token 数是估算值，不是 ChatGPT 的真实计数。

## 随时停下

- `python3 ~/.claude/skills/ask-chatgpt/chatgpt_bridge.py stop`：总开关。所有会话里的任务都在下一次点击前停下，正在等待的也会退出；等它们真的全部结束后再退出 Peekaboo，还有没停的会逐个列出来（`stop --force` 强制结束）。关着时任何命令都不会碰屏幕，退出码 5。`resume` 重新打开。
- `status`：显示开关状态、Peekaboo 是否在运行、哪些任务正在用屏幕（进程号、做什么、从何时开始）。只是查看时不会启动 Peekaboo。
- 任务只在需要时才启动 Peekaboo.app，最后一个任务结束时把它关掉。
- 键盘鼠标空闲满 30 秒任务才会点击或打字，无人值守的任务是 120 秒，所以你在用电脑时它不会抢前台。可以用 `CHATGPT_BRIDGE_USER_IDLE` 调整。
- 任务找不到自己的对话时，最多搜索 3 轮就放弃并报告。搜索时只看其他对话，绝不复制它们的内容。

## 故障排查

| 现象 | 处理 |
|---|---|
| `status` 报 No ChatGPT window | 打开 ChatGPT 桌面版并保持窗口（可以不在前台） |
| 权限报错，或读到的元素很少 | 确认 Peekaboo.app 的权限 `permissions status` 三项都是 Granted（任务需要时会自己启动它） |
| 退出码 5 / `switched off` | 总开关关着，`resume` 打开 |
| 日志显示 `someone is using the computer` | 正常：你在用电脑，任务等键盘鼠标空闲后再继续 |
| 日志显示 `another run is using the ChatGPT window` / `a draft is open` | 正常：另一个任务正在点击或复制（几秒钟），或者你的输入框里有草稿。它会等一下再继续，不会打断 |
| `Clicked Copy but the clipboard never changed` | 复制那一刻鼠标或焦点被占用，或者 ChatGPT 的复制按钮卡在「已复制」。桥接会改用回答里代码块自己的复制按钮；还不行就等一下再 `read-last` |
| 超时，或进程中途退出 | 先 `status`。还在生成就用 `wait --conversation "标题"` 接管；已经答完就用 `read-last` |
| `is not visible in the sidebar` | 要续问的对话不在侧边栏可见范围内，改用新对话 |
| 退出码 3 | 对话快满了，Claude 会生成交接提示词并新开对话 |

**出错时不要直接重发**：失败的那次可能已经发出去了，重发会重复。

---

<details>
<summary><b>命令行参考</b>（不通过 Claude Code，直接用脚本）</summary>

### chatgpt_bridge.py：和 ChatGPT 对话

```bash
B=~/.claude/skills/ask-chatgpt/chatgpt_bridge.py

python3 $B status                                          # 开关、Peekaboo、正在运行的任务；当前窗口、是否在生成
python3 $B stop                                            # 总开关：停下所有任务并退出 Peekaboo（resume 重新打开）
python3 $B ask - --new-chat --out answer.md < prompt.md    # 新对话提问（提示词从文件读）
python3 $B ask - --new-chat --attach code.zip --timeout 10800 --poll 10 --out answer.md < prompt.md
python3 $B ask - --conversation "对话标题" --out answer2.md < followup.md   # 在指定对话里续问
python3 $B wait --conversation "对话标题" --out answer.md  # 接管一个还在生成的回答
python3 $B read-last --out answer.md                       # 重新复制当前对话最后一条回答
python3 $B usage                                           # 查看所有对话的用量账本
```

`--out` 会同时写出 `answer.md.meta.json`（对话标题、已用 token、预算、剩余）。退出码：`0` 成功，`1` 出错，`3` 续问会超出该对话的预算。

### review.py：审查循环

```bash
RV=~/.claude/skills/chatgpt-review/review.py
P=~/code/my-project

# 建立审查（三选一）
python3 $RV init --root $P --context "这是一个……的服务，技术栈……"
python3 $RV init --root $P --include web/api src --brief 审查说明.md   # 只审指定目录，用你自己的审查说明
python3 $RV init --root $P --source-zip 审查包.zip                    # 审你打好的快照包（只适合单轮）

# 每一轮
python3 $RV pack --root $P            # 按范围从工作区打包；后续轮次只重发有变化的部分
python3 $RV next --root $P            # 输出要发的 zip / prompt / out / conversation
python3 $B ask - --new-chat --attach <zip> --timeout 10800 --poll 10 --out <out> < <prompt>
                                      # 第二轮起改用 --conversation "<conversation>"
python3 $RV ingest <out> --root $P    # 解析 ChatGPT 的结论
python3 $RV set F1 --verdict confirmed --note "核实理由，引用文件:行号" --root $P
python3 $RV report --root $P --summary "几条成立、最大风险、建议先改什么"

# 改完代码后记录处理结果，再开始下一轮
python3 $RV set F1 --status fixed   --note "怎么改的" --root $P
python3 $RV set F5 --status wontfix --note "不改的理由（下一轮会给 ChatGPT 看）" --root $P
```

- `--include` 接目录、文件或通配符（相对项目根目录），每一轮都按这个范围从工作区重新打包，所以下一轮能看到修改。
- `--source-zip` 审的是解压出来的快照，改动不会反映到下一轮。包里的 `00_审查说明.md` / `REVIEW_BRIEF.md` 会作为审查说明，`改动.diff` / `CHANGES.diff` 会作为改动一起发送。
- 默认整个范围打成一个包、在一个对话里审（Pro 能自己处理几百个文件，也看得到跨模块的问题）。如果 ChatGPT 报告大片文件没读到，可以加 `pack --chunk-tokens 60000` 按目录切块、每块一个对话。

想先试试流程，可以拿仓库里的 `examples/demo_project` 跑：它有两个故意埋的 bug，第一轮应该全部找出来，修好后第二轮应确认修复。

</details>

<details>
<summary><b>实现原理与踩过的坑</b></summary>

```
Claude Code ──Bash──▶ chatgpt_bridge.py ──Peekaboo（辅助功能树 + 截图 + 点击）──▶ ChatGPT 桌面版
     ▲                       │
     └──── Markdown ◀── 回答下方的 Copy 按钮 → 剪贴板
review.py：打包 → 提示词 → 解析 JSON → 核实状态 → HTML 报告 → 下一轮
```

| 问题 | 处理 |
|---|---|
| ChatGPT 桌面版是 Chromium 内核，默认辅助功能树只有十几个元素 | 每次读取都加大遍历深度，才能拿到输入框、按钮和可见文字 |
| 辅助功能树只暴露屏幕上可见的部分，长回答读不全 | 用回答下方的 Copy 按钮复制完整 Markdown；前后备份 / 恢复剪贴板，并用哨兵值确认真的复制到了 |
| 对 Chromium 按钮执行 AXPress 会卡死 Peekaboo Bridge | 改用真实鼠标点击；点击前确认 ChatGPT 在最前面，免得点到盖在上面的窗口 |
| CLI 可能连到一个没有权限的临时 daemon | 固定使用 Peekaboo.app 的 Bridge socket |
| 回答很长时视图停在中间，Copy 按钮不在屏幕上 | 在后台滚动到底再找 |
| 上传附件后按钮行会移位；空的新对话里没有 Send 按钮 | 以「Add files and more」那一行为锚点识别按钮；生成中以出现 `Stop` 按钮判定 |
| `peekaboo dialog file` 识别不到上传用的文件选择框 | 把文件单独放进一个新文件夹，用 ⌘⇧G 前往后双击；「前往」框的补全加载慢，回车会重试 |
| ChatGPT 会弹出「Continue in ChatGPT Work」并倒计时 | 自动点「Stay in Chat」；提示词里也要求在当前对话完成 |
| 新对话刚发送时页面标题是提示词本身，之后还会改名一到两次 | 只认生成中、且与提示词不同的标题；之后如果旧标题从侧边栏消失、新标题出现，就判定为改名并跟着走 |
| 长时间等待要轮询上千次，Peekaboo 偶尔读取失败 | 每次读取失败自动重试，不会中断整个等待 |

</details>

## 目录结构

```
skills/
  ask-chatgpt/            # 和 ChatGPT 对话
    SKILL.md
    chatgpt_bridge.py
  chatgpt-review/         # 审查循环
    SKILL.md
    review.py
    prompts/round1.md     # 首轮审查提示词
    prompts/followup.md   # 复审提示词
examples/demo_project/    # 埋了 bug 的示例项目
install.sh
```

## License

MIT
