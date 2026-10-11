这是项目「$project」的第 $round 轮审查。附件 `$zip_name` 是**修改后的源码**（$file_count 个文件，约 $line_count 行）$diff_note，请以它为准，不要依赖你对旧版本的记忆。请直接在当前对话中完成，不需要切换到 Work 模式。
$part_note
$handoff

## 上一轮结论的处理情况
$status_table

## 这一轮请完成三件事
1. **核实修复**：对每条「已修复」的问题，在新代码里确认是否真正修好、有没有改一半、有没有引入新问题。
2. **复核分歧**：对「不修改」或「暂缓」的条目，看看给出的理由是否成立；如果你仍然认为必须改，请说明理由。
3. **找新问题**：完整重读包内代码，报告新发现的问题，`id` 从 `$first_id` 开始编号。之前已经报告过的问题不要重复报告。

和上一轮一样可以长时间工作：用工具解压、阅读、写最小复现脚本实际运行，核实修复时要跑上一轮的反例和正常对照。

审查标准和上一轮相同：只报告有代码证据的问题，给出文件、行号和原样摘录的代码，不报告纯风格偏好，严重级别分 `critical` / `high` / `medium` / `low`，并标注 `confidence`。

## 输出格式（必须严格遵守）
先用不超过 5 句话总结这一轮的结论，然后输出**唯一一个** ```json 代码块：

```json
{
  "summary": "一句话总体结论",
  "fix_checks": [
    {"id": "F1", "status": "fixed", "note": "核实说明"}
  ],
  "disputes": [
    {"id": "F2", "agree": false, "note": "你对「不修改」理由的看法"}
  ],
  "unread_files": [],
  "findings": [
    {
      "id": "$first_id",
      "severity": "medium",
      "category": "correctness",
      "file": "src/example.py",
      "line": 42,
      "title": "一句话说明什么坏了",
      "evidence": "原样摘录的相关代码",
      "problem": "怎么触发 → 得到什么错误结果",
      "suggestion": "具体的修改方式",
      "verification": "怎么确认的：复现脚本与实际输出，或静态控制流依据",
      "confidence": "high"
    }
  ]
}
```

- `fix_checks[].status` 取值：`fixed`（已修好）、`partial`（部分修好）、`not_fixed`（没修好）。
- 用中文填写各字段。没有内容的数组写成空数组。
- JSON 代码块之后不要再输出任何内容。
