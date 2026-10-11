# claude-chatgpt-bridge

让 **Claude Code** 直接调用你电脑上的 **ChatGPT 桌面版**（包括 Pro 模型）：发问题、上传 zip、等它想完、把完整回答拿回来。在此之上做了一套**代码审查循环**：ChatGPT Pro 负责长时间、多轮、带复现的严格评审，Claude 负责逐条对照源码核实、出 HTML 报告、改代码，再送回去复审，直到没有问题。

不需要 OpenAI API Key，用的就是你自己的 ChatGPT 订阅和桌面 App。

> ⚠️ 非官方项目，与 OpenAI、Anthropic 无关。它通过 macOS 辅助功能模拟人工操作 ChatGPT 界面，**OpenAI 的使用条款限制自动化获取输出**，使用风险（包括账号风险）由你自己承担。请只在你自己的账号上、为你自己的工作使用。

```
┌──────────────┐  Bash   ┌───────────────────┐  Peekaboo（辅助功能 + 截图 + 点击）  ┌─────────────────┐
│ Claude Code  │ ──────▶ │ chatgpt_bridge.py │ ─────────────────────────────────▶ │ ChatGPT 桌面版   │
│  · 规划/改码  │ ◀────── │  · 发送 / 上传     │ ◀──────── 复制按钮 → 剪贴板 ──────── │  · Pro 长时间评审 │
│  · 核实结论   │  Markdown│  · 等待 / 读取     │                                    │  · 沙箱里跑复现   │
└──────────────┘         └───────────────────┘                                    └─────────────────┘
        │  review.py：打包 → 提示词 → 解析 JSON → 核实状态 → HTML 报告 → 下一轮
```

## 能做什么

- **`/ask-chatgpt`**：把一个问题（可附文件）交给 ChatGPT，拿回完整的 Markdown 回答。适合要第二意见、要 Pro 深度思考的场景。
- **`/chatgpt-review`**：代码审查循环。
  1. 把项目打成 zip（自动排除 `.env`、私钥、`node_modules`、构建产物等），连同精心设计的审查提示词发给 ChatGPT Pro；
  2. Pro 在沙箱里解压、扫描、沿调用链阅读、写最小复现脚本实际运行，三轮交叉复核后给出结构化结论（可以连续工作 30–60 分钟以上）；
  3. Claude 对每条结论打开源码核实：成立 / 不成立 / 存疑，写明理由；
  4. 生成 HTML 报告，**要改的点一条一条列好**（严重级别、位置、证据、修改建议、ChatGPT 怎么验证的、Claude 的核实意见），可筛选、可一键复制修改清单；
  5. 你确认要改哪些（或开自动模式），Claude 修改并跑测试，把「已修复 / 不修改及理由」带回同一个 ChatGPT 对话复审；
  6. ChatGPT 核实修复、复核分歧、找新问题 → 下一份报告 …… 直到没有 medium 以上的问题。
- **上下文账本**：每个 ChatGPT 对话的用量都会记账，快到上限时自动换新对话，并把之前的结论作为交接摘要带过去。

## 环境要求

| 组件 | 说明 |
|---|---|
| macOS | 15 或更新（在 macOS 26.2 上测试） |
| ChatGPT 桌面版 | 登录你的账号，窗口保持打开；界面语言为 **English**（脚本按英文按钮名定位，见「已知限制」） |
| [Peekaboo](https://github.com/openclaw/Peekaboo) | `brew install openclaw/tap/peekaboo`，并**运行 Peekaboo.app**，在「系统设置 → 隐私与安全性」里给它**屏幕录制**和**辅助功能**权限（在 4.2.2 上测试） |
| Claude Code | 用 skill 的方式调用（命令行脚本也可以单独用） |
| Python 3.9+ | 只用标准库，无需安装依赖 |

检查 Peekaboo 权限：

```bash
peekaboo permissions status --bridge-socket ~/Library/Application\ Support/Peekaboo/bridge.sock
```

三项都显示 Granted 即可。

## 安装

```bash
git clone https://github.com/Furinaaa-Cancan/claude-chatgpt-bridge.git
cd claude-chatgpt-bridge
./install.sh            # 复制到 ~/.claude/skills/；用 ./install.sh --link 则建软链接，方便跟随仓库更新
```

验证（只读，不会操作界面）：

```bash
python3 ~/.claude/skills/ask-chatgpt/chatgpt_bridge.py status
```

能看到当前 ChatGPT 窗口的标题、输入框状态等，就说明链路通了。

## 在 Claude Code 里使用

安装后 Claude Code 会自动识别这两个 skill，直接说人话或用斜杠命令都行：

```text
/ask-chatgpt 帮我比较一下这两种缓存失效策略的优缺点：……
问问 ChatGPT Pro：这段 SQL 在 PostgreSQL 16 下会不会有锁升级问题？

/chatgpt-review ~/code/my-project
用 ChatGPT 严格审一下这个项目的代码，多轮，审完把要改的列成 HTML
/chatgpt-review ~/Downloads/待审代码.zip 自动
```

- Claude 每次操作 ChatGPT 之前会先告诉你。**上传、发送、复制这几个瞬间**它会把 ChatGPT 切到前台、移动鼠标，大约几秒；中间漫长的等待都在后台，你可以照常用电脑，甚至照常用 ChatGPT 的其他对话（脚本会等你空闲后再切回审查对话取结果）。
- 首次上传你的代码前，Claude 会给你看打包摘要（多少文件、多大、排除了哪些疑似密钥文件）并征求同意。
- 审查默认每轮停下来问你要改哪些；说「自动」则会自动修所有核实成立、medium 及以上的问题。无论哪种模式都不会自行 commit / push。

## 直接用命令行

### chatgpt_bridge.py —— 和 ChatGPT 对话

```bash
B=~/.claude/skills/ask-chatgpt/chatgpt_bridge.py

python3 $B status                                            # 当前窗口、是否在生成、草稿、账本用量
python3 $B ask - --new-chat --out answer.md < prompt.md      # 新对话提问（提示词从文件读，多行不会出错）
python3 $B ask - --new-chat --attach code.zip --timeout 10800 --poll 10 --out answer.md < prompt.md
python3 $B ask - --conversation "对话标题" --out answer2.md < followup.md   # 在指定对话里追问
python3 $B wait --conversation "对话标题" --out answer.md    # 接管一个已经在生成中的回答
python3 $B read-last --out answer.md                         # 重新复制当前对话最后一条回答
python3 $B usage                                             # 查看所有对话的 token 账本
```

- `--out` 同时写出 `answer.md.meta.json`（对话标题、已用 token、预算、剩余）。
- 退出码：`0` 成功，`1` 出错，`3` 这次追问会超出该对话的上下文预算（应新开对话并附交接摘要）。
- 出错时**不要直接重发**：失败的那次可能已经发出去了。先 `status` 看情况，回答已出来就用 `read-last` 取。

### review.py —— 审查循环

```bash
RV=~/.claude/skills/chatgpt-review/review.py

# 1. 建立审查（二选一）
python3 $RV init --root ~/code/my-project --context "这是一个……的服务，技术栈……，入口在……"
python3 $RV init --root ~/code/my-project --include web/api src --brief 审查说明.md   # 只审核心目录
python3 $RV init --root ~/code/my-project --source-zip 审查包.zip   # 审你打好的快照包（只适合单轮）

# 2. 打包、取下一步要发的内容
python3 $RV pack --root ~/code/my-project
python3 $RV next --root ~/code/my-project        # 输出 zip / prompt / out / conversation

# 3. 发给 ChatGPT（conversation 为空用 --new-chat，否则 --conversation "标题"）
python3 $B ask - --new-chat --attach <zip> --timeout 10800 --poll 10 --out <out> < <prompt>

# 4. 解析结论 → 逐条核实 → 出报告
python3 $RV ingest <out> --root ~/code/my-project
python3 $RV set F1 --verdict confirmed --note "第 11 行确实升序切片，实测返回最小值" --root ~/code/my-project
python3 $RV set F3 --verdict disputed  --note "调用方已在 X 处校验，不可达" --root ~/code/my-project
python3 $RV report --root ~/code/my-project --summary "几条成立、最大风险、建议先改什么"

# 5. 改完代码后记录处理结果，再从第 2 步开始下一轮
python3 $RV set F1 --status fixed --note "改为倒序排序并加了回归测试" --root ~/code/my-project
python3 $RV set F5 --status wontfix --note "只在单进程部署，多进程场景不适用" --root ~/code/my-project
```

`--source-zip` 的包里如果有 `00_审查说明.md` / `REVIEW_BRIEF.md`，会作为审查说明（项目背景、重点、要查和不要查的）原样放进提示词；`改动.diff` / `CHANGES.diff` 会作为「相对上次审查的改动」一起发送。也可以用 `--brief FILE`、`--diff FILE` 显式指定。

`--include` 接目录、文件或通配符（相对项目根目录），每一轮都按这个范围从工作区重新打包，所以「改代码 → 下一轮复审」能看到修改。`--source-zip` 审的是解压出来的快照，改动不会反映到下一轮，适合一次性审查。

审查状态都存在 `~/.chatgpt-review/<项目名>-<哈希>/`：每轮的 zip、提示词、ChatGPT 原始回答、`report.html`。**不会往你的项目目录里写任何文件。**

想先试一下流程，可以拿仓库里的 `examples/demo_project` 跑：它有两个故意埋的 bug，一轮应该全部找出来，修好后第二轮应确认修复。

## 报告长什么样

- 顶部：待修改数量、各严重级别计数、已修复数量
- Claude 的总结 + ChatGPT 的概述 + 覆盖情况（深读了多少、仅扫描多少、哪些没读到）+ 对话上下文用量条
- **要改的点**：按严重程度排序编号，每条包含位置、问题、证据代码、修改建议、ChatGPT 的验证方式、Claude 的核实意见；支持按级别筛选、一键复制修改清单
- 折叠区：已修复（含 ChatGPT 对修复的核实）、不修改 / 存疑（含理由与 ChatGPT 的回应）
- 自适应浅色 / 深色，手机上也能看

## 上下文怎么管

ChatGPT 一个对话的上下文是有限的（官方公布 Thinking 模式输入上限 128K，Pro 在 Chat 里的具体上限未公布）。脚本为每个对话记一本账（`~/.chatgpt-bridge/ledger.json`）：

- 按文字估算 token（中文约 1 字 1 token，其他约 3.5 字符 1 token，故意估高）；
- 附件按其文本量计入，但**封顶 3 万**——ChatGPT 是用工具在沙箱里处理 zip，不会把整个包读进上下文；
- 默认预算 90 000（约 128K 的七成），追问前超预算就返回退出码 3，由 Claude 新开对话并带上交接摘要。

可用环境变量调整：`CHATGPT_BRIDGE_BUDGET`、`CHATGPT_BRIDGE_ATTACH_TOKENS`。

大项目默认**整包一个对话**（ChatGPT 自己能处理几百上千个文件，跨模块的问题也看得到）。如果 ChatGPT 报告有大片文件没读到，可以用 `pack --chunk-tokens 60000` 按目录切成多块、每块一个对话审，后续轮次只重审有改动的块。

## 实现要点（踩过的坑）

| 问题 | 处理 |
|---|---|
| ChatGPT 桌面版是 Chromium 内核，默认辅助功能树只有十几个元素 | 每次读取都加大遍历深度（`--depth 80` 等），能拿到输入框、按钮和可见文字 |
| 辅助功能树只暴露屏幕上可见的部分，长回答读不全 | 用回答下方的 Copy 按钮复制到剪贴板读取完整 Markdown，前后保存 / 恢复你的剪贴板，并用哨兵值确认真的复制成功 |
| 对 Chromium 按钮执行 AXPress 会卡死 Peekaboo Bridge | 改用真实鼠标点击，点击前确认 ChatGPT 在最前面，避免点到盖在上面的其他窗口 |
| CLI 可能连到一个没有权限的临时 daemon | 固定使用 Peekaboo.app 的 Bridge socket |
| 长回答结束后视图不在底部，Copy 按钮不可见 | 后台滚动到底再找 |
| 上传附件后按钮行会移位；空的新对话里没有 Send 按钮 | 以「Add files and more」所在行为锚点识别按钮；生成中以 `Stop` 按钮判定 |
| `peekaboo dialog file` 识别不到上传的文件选择框 | 把文件单独放进一个新文件夹，用 ⌘⇧G 前往后双击选中；「前往」框的补全加载慢，回车会重试 |
| ChatGPT 会弹出「Continue in ChatGPT Work」并倒计时 | 自动点「Stay in Chat」，提示词里也明确要求在当前对话完成 |
| 新对话刚发送时页面标题是提示词本身，之后才改名 | 只认生成中、且与提示词不同、连续两次一致的标题，用来锁定对话 |

## 已知限制

- **依赖 ChatGPT 的界面**：按钮名（Send、Copy、New chat、Stay in Chat……）、布局一改就可能失效；目前只适配英文界面。
- 只支持 macOS，同一时间只能跑一个 ChatGPT 任务（共用一个窗口）。
- 上传、发送、复制那几秒如果你正好在点鼠标或打字，那一步可能失败；脚本会报错而不是乱点，按提示用 `status` / `read-last` 恢复即可。
- 读回的是 ChatGPT 的文字回答；它在沙箱里生成的文件（比如它自己的 HTML 报告、证据包）目前不会自动下载。
- token 数是估算，不是 ChatGPT 的真实计数。

## 故障排查

| 现象 | 处理 |
|---|---|
| `status` 报 No ChatGPT window | 打开 ChatGPT 桌面版并保持窗口（可以不在前台） |
| 权限相关报错 / 元素数很少 | 确认 Peekaboo.app 在运行，`permissions status` 三项 Granted |
| `already generating` / `draft` | 你正在用 ChatGPT，等它答完或清空输入框后再试 |
| `Clicked Copy but the clipboard never changed` | 那一刻鼠标或焦点被占用；等一下再 `read-last` |
| 超时 | `status` 看是否仍在生成；是的话继续等，或用 `wait --conversation "标题"` 接管 |
| `is not visible in the sidebar` | 要追问的对话不在侧边栏可见范围内，改用 `--new-chat` |
| 退出码 3 | 对话快满了：Claude 会用 `prompt --handoff` 生成交接提示词并新开对话 |

## 目录结构

```
skills/
  ask-chatgpt/
    SKILL.md              # 给 Claude Code 的使用说明
    chatgpt_bridge.py     # 操作 ChatGPT 桌面版
  chatgpt-review/
    SKILL.md              # 审查循环的流程说明
    review.py             # 打包 / 提示词 / 解析 / 状态 / HTML 报告
    prompts/round1.md     # 首轮审查提示词
    prompts/followup.md   # 复审提示词
examples/demo_project/    # 埋了 bug 的示例项目
install.sh
```

## License

MIT
