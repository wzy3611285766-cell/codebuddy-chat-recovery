#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_codebuddy_history.py — 找回 CodeBuddy CN（VS Code 系编辑器）里“消失”的 AI 聊天记录。

原理
----
CodeBuddy CN 的会话按「工作区路径」分桶存储：

    %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\<uid>\\CodeBuddyIDE\\<uid>\\history\\<32位哈希>\\
        index.json            # 桶级索引：conversations 列表
        recent.json / current.json
        <会话ID>\\index.json   # 会话级索引：messages 清单（没有它，点开会话是空的！）
        <会话ID>\\messages\\*.json

工作区文件夹改名 / 移动后，路径哈希变化 -> IDE 去读一个新空桶，历史对话“消失”。
旧桶内容仍在。IDE 日志（%APPDATA%\\CodeBuddy CN\\logs\\...）里有
"Initializing with workspace: <路径>" 和 "History Base Path: ...\\history\\<哈希>\\" 的成对记录，
据此可以把任意路径映射到它的桶。

用法
----
  python fix_codebuddy_history.py scan
      列出所有 工作区路径 -> 哈希桶 及各桶会话数（从日志 + 目录推断）

  python fix_codebuddy_history.py merge --from <旧工作区路径|哈希> --to <新工作区路径|哈希>
      把旧桶缺的会话补进新桶（幂等）。默认 dry-run，加 --apply 才真正写。

  python fix_codebuddy_history.py verify [<工作区路径|哈希>]
      校验桶里每个会话的消息清单与消息文件是否一一对应

注意事项（踩过的坑）
--------------------
1. 合并前必须完全退出 CodeBuddy CN（所有窗口）。IDE 运行中会把内存索引写回，覆盖修复结果；
   用户点开一个会话，IDE 就会把该会话的 index.json 写成空的。
2. robocopy 千万不要用 /XF index.json —— 它按文件名匹配所有层级，会把每个会话自己的
   index.json（消息清单）也排除掉。必须整目录复制。
3. messages 文件路径常超 260 字符，普通 shutil/os.walk 会静默漏文件；用 robocopy（复制）
   和 \\\\?\\ 前缀（校验）。
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

IDE_PROCESS_MARK = "CodeBuddy CN"
META_FILES = {"index.json", "current.json", "recent.json", ".index_bak.json"}


# --------------------------------------------------------------------------- #
# 定位
# --------------------------------------------------------------------------- #

def find_history_roots():
    """返回所有 ...\\CodeBuddyExtension\\Data\\<uid>\\CodeBuddyIDE\\<uid>\\history 目录。"""
    local = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
    roots = []
    pattern = os.path.join(local, "CodeBuddyExtension", "Data", "*", "CodeBuddyIDE", "*", "history")
    for p in glob.glob(pattern):
        if os.path.isdir(p):
            roots.append(p)
    return roots


def app_log_dirs():
    appdata = os.environ.get("APPDATA", os.path.expanduser("~/AppData/Roaming"))
    dirs = []
    for name in ("CodeBuddy CN", "CodeBuddy", "WorkBuddy"):
        d = os.path.join(appdata, name, "logs")
        if os.path.isdir(d):
            dirs.append(d)
    return dirs


# --------------------------------------------------------------------------- #
# 日志 -> 工作区/桶 映射
# --------------------------------------------------------------------------- #

RE_WS = re.compile(r"Initializing with workspace:\s*(.+?),\s*uid:")
RE_BP = re.compile(r"History Base Path:.*[\\/]history[\\/]([0-9a-f]{32})[\\/]?\s*$")
PAIR_WINDOW = 6  # 两条日志相隔行数上限


def parse_workspace_map():
    """扫描 IDE 日志，返回 {哈希: 工作区路径}（后出现的覆盖先出现的）。"""
    mapping = {}
    for base in app_log_dirs():
        for dirpath, _, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".log"):
                    continue
                last_ws, last_line = None, -10**9
                try:
                    fh = open(os.path.join(dirpath, fn), encoding="utf-8", errors="replace")
                except OSError:
                    continue
                with fh:
                    for i, line in enumerate(fh):
                        m = RE_WS.search(line)
                        if m:
                            last_ws, last_line = m.group(1).strip(), i
                            continue
                        m = RE_BP.search(line)
                        if m and last_ws and i - last_line <= PAIR_WINDOW:
                            mapping.setdefault(m.group(1), last_ws)
    return mapping


def norm_path(p):
    return os.path.normpath(str(p)).replace("\\", "/").rstrip("/").lower()


def resolve_bucket(spec, roots, ws_map):
    """把 '工作区路径或哈希' 解析成 (root, bucket绝对路径, 显示名)。"""
    # 直接给的是 32 位哈希
    if re.fullmatch(r"[0-9a-f]{32}", spec.strip().lower()):
        h = spec.strip().lower()
        for root in roots:
            p = os.path.join(root, h)
            if os.path.isdir(p):
                return root, p, ws_map.get(h, h[:8])
        raise SystemExit(f"[错误] 在 {len(roots)} 个 history 根目录里都找不到桶 {h}")

    key = norm_path(spec)
    hits = []
    for root in roots:
        for h, ws in ws_map.items():
            if norm_path(ws) == key:
                p = os.path.join(root, h)
                if os.path.isdir(p):
                    hits.append((root, p, ws))
    if not hits:
        known = "\n".join(f"  {h[:8]}…  {ws}" for h, ws in sorted(ws_map.items()))
        raise SystemExit(f"[错误] 日志里找不到工作区 {spec!r} 对应的桶。\n已知映射：\n{known or '  （空——先打开一次对应工作区让 IDE 写日志）'}")
    if len(hits) > 1:
        # 多个 uid 根目录命中（罕见），取会话最多的
        hits.sort(key=lambda x: conv_count(x[1]), reverse=True)
    return hits[0]


# --------------------------------------------------------------------------- #
# 桶操作
# --------------------------------------------------------------------------- #

def conv_count(bucket):
    idx = os.path.join(bucket, "index.json")
    if not os.path.isfile(idx):
        return 0
    try:
        return len(json.load(open(idx, encoding="utf-8")).get("conversations", []))
    except Exception:
        return 0


def bucket_report(roots, ws_map):
    rows = []
    for root in roots:
        for h in sorted(os.listdir(root)):
            p = os.path.join(root, h)
            if os.path.isdir(p):
                rows.append((h, ws_map.get(h, "(未知工作区)"), conv_count(p)))
    return rows


def cmd_scan():
    roots = find_history_roots()
    ws_map = parse_workspace_map()
    if not roots:
        raise SystemExit("[错误] 没找到 history 目录，确认装过 CodeBuddy CN 且打开过至少一次")
    print(f"history 根目录 {len(roots)} 个：")
    for r in roots:
        print(f"  {r}")
    print()
    rows = bucket_report(roots, ws_map)
    print(f"{'桶':<36}{'会话数':>4}  工作区")
    for h, ws, n in rows:
        print(f"{h:<36}{n:>4}  {ws}")
    print(f"\n共 {len(rows)} 个桶。改名/移动过的工作区会对应两个桶（旧的多、新的少）——")
    print("用 merge --from <旧路径> --to <新路径> 合并。")


def robocopy_dir(src, dst):
    """整目录补缺复制（不排除任何文件名！长路径安全）。"""
    if os.name != "nt":
        ok = True
        for dirpath, dirs, files in os.walk(src):
            rel = os.path.relpath(dirpath, src)
            target = os.path.join(dst, rel)
            os.makedirs(target, exist_ok=True)
            for f in files:
                s, d = os.path.join(dirpath, f), os.path.join(target, f)
                if not os.path.exists(d):
                    shutil.copy2(s, d)
        return True
    r = subprocess.run(
        ["robocopy", src, dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/R:1", "/W:1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode < 8


def sync_conv_index(old_bucket, new_bucket):
    """会话级 index.json：新桶里若为空/缺失，用旧桶强制覆盖。返回修正数。"""
    fixed = 0
    for c in os.listdir(old_bucket):
        si = os.path.join(old_bucket, c, "index.json")
        di = os.path.join(new_bucket, c, "index.json")
        if not os.path.isfile(si):
            continue
        try:
            so = len(json.load(open(si, encoding="utf-8")).get("messages", []))
            sn = -1
            if os.path.isfile(di):
                sn = len(json.load(open(di, encoding="utf-8")).get("messages", []))
            if sn != so:
                os.makedirs(os.path.dirname(di), exist_ok=True)
                shutil.copy2(si, di)
                fixed += 1
        except Exception:
            shutil.copy2(si, di)
            fixed += 1
    return fixed


def merge_bucket(old_bucket, new_bucket, dry=False):
    """核心合并：目录补缺 -> 会话索引同步 -> 桶级索引/recent 合并。"""
    old_idx_p = os.path.join(old_bucket, "index.json")
    new_idx_p = os.path.join(new_bucket, "index.json")
    old = json.load(open(old_idx_p, encoding="utf-8")) if os.path.isfile(old_idx_p) else {"conversations": []}
    new = json.load(open(new_idx_p, encoding="utf-8")) if os.path.isfile(new_idx_p) else {"conversations": []}
    have = {c["id"] for c in new.get("conversations", [])}
    to_add = [c for c in old.get("conversations", []) if c["id"] not in have]
    print(f"  旧桶 {conv_count(old_bucket)} 条 / 新桶 {conv_count(new_bucket)} 条，将补入 {len(to_add)} 条：")
    for c in to_add:
        print(f"    - {c.get('name') or c['id'][:8]}  ({c.get('createdAt', '')[:10]})")
    if dry:
        print("  [dry-run] 未写入。加 --apply 执行。")
        return

    # 0) 备份新桶根部元数据
    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(new_bucket, f".recovery-backup-{ts}")
    os.makedirs(bdir, exist_ok=True)
    for fn in META_FILES:
        src = os.path.join(new_bucket, fn)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(bdir, fn))
    print(f"  已备份新桶元数据 -> {bdir}")

    # 1) 整目录补缺（绝不排除 index.json！）
    for c in {x["id"] for x in to_add} | {d for d in os.listdir(old_bucket)
                                          if os.path.isdir(os.path.join(old_bucket, d))}:
        s = os.path.join(old_bucket, c)
        if os.path.isdir(s):
            robocopy_dir(s, os.path.join(new_bucket, c))

    # 2) 会话级索引同步（覆盖被 IDE 写空的）
    fixed = sync_conv_index(old_bucket, new_bucket)
    print(f"  会话级 index.json 修正 {fixed} 个")

    # 3) 桶级索引合并
    for c in to_add:
        if os.path.isdir(os.path.join(new_bucket, c["id"])):
            new["conversations"].append(c)
    new["conversations"].sort(key=lambda x: x.get("createdAt", ""))
    json.dump(new, open(new_idx_p, "w", encoding="utf-8"), ensure_ascii=False)

    # 4) recent.json 合并
    op, np = os.path.join(old_bucket, "recent.json"), os.path.join(new_bucket, "recent.json")
    if os.path.isfile(op):
        try:
            o = json.load(open(op, encoding="utf-8"))
            n = json.load(open(np, encoding="utf-8")) if os.path.isfile(np) else {}
            if isinstance(o, dict) and isinstance(n, dict):
                if isinstance(o.get("conversations"), list):
                    ids = {c["id"] for c in n.get("conversations", [])}
                    n.setdefault("conversations", []).extend(
                        [x for x in o["conversations"] if x["id"] not in ids])
                for k, v in o.items():
                    n.setdefault(k, v)
            else:
                n = o
            json.dump(n, open(np, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception as e:
            print(f"  recent.json 跳过：{e}")

    print(f"  完成，新桶现有 {conv_count(new_bucket)} 条。重新打开 IDE 查看。")


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def cmd_verify(spec, roots, ws_map):
    if spec:
        _, bucket, label = resolve_bucket(spec, roots, ws_map)
    else:
        # 不指定就查会话数最多的桶
        rows = bucket_report(roots, ws_map)
        rows.sort(key=lambda r: r[2], reverse=True)
        bucket = os.path.join(roots[0], rows[0][0])
        label = rows[0][1]
    print(f"校验 {label}  ({os.path.basename(bucket)})")
    idx = json.load(open(os.path.join(bucket, "index.json"), encoding="utf-8"))
    bad = 0
    for c in idx.get("conversations", []):
        cid = c["id"]
        cp = os.path.join(bucket, cid, "index.json")
        if not os.path.isfile(cp):
            print(f"  [缺] {c.get('name') or cid[:8]}: 会话级 index.json 不存在")
            bad += 1
            continue
        msgs = json.load(open(cp, encoding="utf-8")).get("messages", [])
        md = "\\\\?\\" + os.path.abspath(os.path.join(bucket, cid, "messages"))
        have = set(os.listdir(md)) if os.path.isdir(md) else set()
        missing = [m["id"] for m in msgs if m["id"] + ".json" not in have]
        flag = "OK " if not missing else "缺!"
        if missing:
            bad += 1
        print(f"  [{flag}] {(c.get('name') or cid[:8])[:16]:<18} 消息 {len(msgs):<5} 在盘 {len(have):<5} 缺 {len(missing)}")
    print("\n结论：" + ("全部完整 ✅" if bad == 0 else f"{bad} 个会话有问题 ❌"))


def ide_running():
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq WorkBuddy.exe"],
                             capture_output=True, text=True).stdout
    except Exception:
        return False
    # CodeBuddy CN 的主进程名带空格，tasklist 不支持模糊，改用 wmic 式查询
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process | Where-Object { $_.ProcessName -like '*CodeBuddy*' }).Count"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return out.isdigit() and int(out) > 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("用法")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="列出工作区->桶映射与会话数")
    m = sub.add_parser("merge", help="把旧桶会话合并进新桶")
    m.add_argument("--from", dest="src", required=True, help="旧工作区路径或桶哈希")
    m.add_argument("--to", dest="dst", required=True, help="新工作区路径或桶哈希")
    m.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    m.add_argument("--force", action="store_true", help="IDE 未退出时也强制执行（不建议）")
    v = sub.add_parser("verify", help="校验桶内会话完整性")
    v.add_argument("target", nargs="?", help="工作区路径或桶哈希，缺省=会话最多的桶")
    args = ap.parse_args()

    roots = find_history_roots()
    if not roots:
        raise SystemExit("[错误] 没找到 history 目录，确认装过 CodeBuddy CN 且打开过至少一次")
    ws_map = parse_workspace_map()

    if args.cmd == "scan":
        cmd_scan()
        return
    if args.cmd == "verify":
        cmd_verify(getattr(args, "target", None), roots, ws_map)
        return

    # merge
    if not args.force and ide_running():
        print("⚠ 检测到 CodeBuddy 正在运行。运行中的 IDE 会把内存里的索引写回、覆盖修复结果，")
        print("  还会在你点开会话时把会话级 index.json 写空。请先完全退出 IDE（所有窗口），")
        print("  或确认知晓风险后加 --force。")
        raise SystemExit(2)
    _, src, sl = resolve_bucket(args.src, roots, ws_map)
    _, dst, dl = resolve_bucket(args.dst, roots, ws_map)
    if os.path.normpath(src) == os.path.normpath(dst):
        raise SystemExit("[错误] --from 和 --to 是同一个桶")
    print(f"合并  {sl}  ({os.path.basename(src)[:8]}…)")
    print(f"  ->  {dl}  ({os.path.basename(dst)[:8]}…)\n")
    merge_bucket(src, dst, dry=not args.apply)


if __name__ == "__main__":
    main()
