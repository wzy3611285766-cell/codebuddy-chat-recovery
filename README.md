# codebuddy-chat-recovery

找回 CodeBuddy CN（腾讯 VS Code 系 AI 编辑器）里"消失"的 AI 聊天记录。

**典型场景**：工作区文件夹改名 / 移动之后，打开 IDE 发现 AI 面板的历史对话全没了；或者历史列表还在，但点开会话是一片空白。记录其实**一条都没删**——它们只是被按旧路径存进了另一个"桶"。

## 原理

CodeBuddy CN 的 AI 会话按**工作区路径的哈希**分桶存储（与账号无关）：

```
%LOCALAPPDATA%\CodeBuddyExtension\Data\<uid>\CodeBuddyIDE\<uid>\history\<32位哈希>\
    index.json               # 桶级索引：conversations 列表（决定历史列表显示什么）
    recent.json / current.json
    <会话ID>\index.json       # 会话级索引：messages 清单（决定点开会话能看到什么）
    <会话ID>\messages\*.json  # 每条消息一个文件
```

文件夹改名 → 哈希变化 → IDE 去读一个新空桶 → 历史"消失"。旧桶内容完好。

「工作区路径 → 桶」的映射不用逆向哈希：IDE 日志（`%APPDATA%\CodeBuddy CN\logs\...`）里有成对记录：

```
[HistoryStorage] Initializing with workspace: c:/path/to/ws, uid: ...
History Base Path: ...\history\<哈希>\
```

本工具解析日志拿到映射，再把旧桶的会话**完整合并**回新桶（幂等、带备份、带校验）。

## 使用

### 0. 前提（硬性）

**完全退出 CodeBuddy CN（所有窗口）**。运行中的 IDE 会把内存索引写回、覆盖修复结果；而且每点开一个会话，IDE 都会把该会话的索引写成空的，越点越坏。

### 1. 扫描：看看你的记录在哪

```bash
python scripts/fix_codebuddy_history.py scan
```

```
桶                                    会话数  工作区
b5ed0dea694f75c16ca97ba428c0ce95       7  c:/Users/me/WorkBuddy/2026-10-08-22-42-20
160f342755acc62653a3d516fae86cea       1  c:/Users/me/WorkBuddy/数境          ← 改名后的新空桶
```

改名过的工作区会出现两个桶：旧的会话多、新的会话少。

### 2. 合并（默认 dry-run，加 `--apply` 才写入）

```bash
python scripts/fix_codebuddy_history.py merge \
    --from "c:/Users/me/WorkBuddy/2026-10-08-22-42-20" \
    --to   "c:/Users/me/WorkBuddy/数境" --apply
```

`--from` / `--to` 都可以直接填桶哈希。脚本会：

1. 备份新桶元数据到桶内 `.recovery-backup-<时间戳>/`；
2. 按会话目录整目录补缺复制（长路径安全）；
3. 同步会话级 `index.json`（被 IDE 写空的强制用旧桶覆盖）；
4. 合并桶级 `index.json` 与 `recent.json`。

幂等，跑多少遍都安全。

### 3. 校验

```bash
python scripts/fix_codebuddy_history.py verify "c:/Users/me/WorkBuddy/数境"
```

逐会话核对「索引消息条数 vs 在盘消息文件数」，输出缺失数。

### 4. 重新打开 IDE 验收

## 踩过的坑（写在代码注释里也写在 SKILL.md 里）

1. robocopy 千万别加 `/XF index.json`——它按文件名匹配**所有层级**，会把每个会话自己的 index.json（消息清单）也排除掉，造成"列表在、点开空"。
2. `messages` 文件路径常超 260 字符：普通 `shutil` / `os.walk` 会**静默漏文件**。复制用 robocopy，枚举用 `\\?\` 长路径前缀。
3. 真正的内容在 `%LOCALAPPDATA%\CodeBuddyExtension\...`，`%APPDATA%\CodeBuddy CN\...` 下只有空壳索引，别修错地方。
4. 会话级索引同步要比对 messages 条数并**强制覆盖**——robocopy 默认跳过时间戳更新的目标文件，而被 IDE 写空的索引往往时间戳更新。

## 安装为 WorkBuddy Skill

把整个文件夹放进 `~/.workbuddy/skills/codebuddy-chat-recovery/` 即可，之后对 WorkBuddy 说"我的 CodeBuddy 聊天记录没了"就会自动触发。

## 环境要求

- Windows（依赖 robocopy 处理长路径）
- Python 3.8+，无第三方依赖

## License

MIT
