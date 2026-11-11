---
name: codebuddy-chat-recovery
description: 修复 CodeBuddy CN / VS Code 系编辑器 AI 聊天记录"消失"问题：工作区文件夹改名或移动后，历史对话列表变空、或列表在但点开会话是空白。本 skill 定位按路径哈希分桶的本地存储，从 IDE 日志解析「工作区路径→存储桶」映射，把旧桶会话完整合并回新桶并逐条校验消息文件。触发词：聊天记录没了、历史对话消失、CodeBuddy 记录恢复、改名后记录丢失、点开是空的、恢复 AI 对话。
agent_created: true
---

# CodeBuddy CN 聊天记录恢复

## 原理（必读）

CodeBuddy CN 的 AI 会话按**工作区路径的哈希**分桶存储，与账号无关：

```
%LOCALAPPDATA%\CodeBuddyExtension\Data\<uid>\CodeBuddyIDE\<uid>\history\<32位哈希>\
    index.json               # 桶级索引：conversations 列表（决定历史列表显示什么）
    recent.json / current.json
    <会话ID>\index.json       # 会话级索引：messages 清单（决定点开会话能看到什么）
    <会话ID>\messages\*.json  # 每条消息一个文件
```

文件夹改名/移动 → 哈希变化 → IDE 读到新空桶 → 历史"消失"。旧桶内容不会丢。

症状与病灶对应：

| 症状 | 病灶 |
|---|---|
| 历史列表整个变空 | 桶级 `index.json` 指向新空桶 |
| 列表在，点开是空白 | 会话级 `<会话ID>\index.json` 缺失或被写成空的 `{"messages":[]}` |

「工作区路径→桶」的映射不用逆向哈希算法：IDE 日志里有成对记录，直接解析——

```
[HistoryStorage] Initializing with workspace: c:/path/to/ws, uid: ...
History Base Path: ...\history\<哈希>\
```

## 工作流

脚本：`scripts/fix_codebuddy_history.py`（Windows；依赖 robocopy，Python 3.8+）。

1. **先让用户完全退出 IDE（所有窗口）**。这是硬前提：
   - 运行中的 IDE 会把内存索引写回，覆盖修复结果；
   - 用户每点开一个会话，IDE 就把该会话的 index.json 写成空的，越点越坏。
   - 脚本会自动检测进程并拒绝执行，除非 `--force`。
2. **扫描映射**：`python scripts/fix_codebuddy_history.py scan`
   - 列出所有桶 + 会话数 + 工作区路径。改名过的工作区会出现两个桶（旧的多、新的少）。
3. **合并**（默认 dry-run，确认后加 `--apply`）：

   ```bash
   python scripts/fix_codebuddy_history.py merge --from "旧工作区路径" --to "新工作区路径" --apply
   ```

   脚本依次做：备份新桶元数据 → 整目录补缺复制 → 会话级 index.json 同步 →
   桶级 index.json / recent.json 合并。幂等，可反复跑。
4. **校验**：`python scripts/fix_codebuddy_history.py verify "工作区路径"`
   - 逐会话核对「索引消息条数 vs 在盘消息文件数」，输出缺失数。
5. **让用户重新打开 IDE** 验收。列表和消息内容都应恢复。

## 硬坑清单（全部踩过）

1. **robocopy 千万不要加 `/XF index.json`**——它按文件名匹配**所有层级**，会把每个会话
   自己的 index.json（消息清单）也排除掉，造成"列表在、点开空"。必须整目录复制。
2. **messages 文件路径常超 260 字符**：普通 `shutil`/`os.walk` 会静默漏文件、`os.path.getsize`
   直接报错。复制用 robocopy；校验/枚举用 `\\?\` 长路径前缀。
3. **IDE 运行中做修复 = 白做**。先退进程；修完在重开前不要让用户点历史对话。
4. `%APPDATA%\CodeBuddy CN\...` 下只有空壳索引，**真正的内容在
   `%LOCALAPPDATA%\CodeBuddyExtension\...`**，别修错地方。
5. 合并方向：从"旧的、会话多的桶"合并进"新的、会话少的桶"；确认 `--from/--to` 不是同一个桶。
6. 会话级索引同步时比较 messages 条数，**条数不一致就强制覆盖**（robocopy 默认会跳过
   时间戳更新的目标文件，而被 IDE 写空的索引往往更新，必须用 copy 强制覆盖）。

## 范围外但相关

- WorkBuddy 桌面版的会话在 `~/.workbuddy/workbuddy.db` + `~/.workbuddy/projects/<slug>/*.jsonl`，
  同样按路径 slug 分目录，改名会出现 `c-旧路径/` 与 `C-新路径/` 两个目录，可用同样的"整目录
  复制 + 索引合并"思路处理（db 记录以 cwd 为准，一般无需改 db）。
- IDE 内置的「导入」按钮只接受其他客户端导出的单份对话文件，不适合批量找回本机历史。
