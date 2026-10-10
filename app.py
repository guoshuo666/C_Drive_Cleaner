#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
C盘空间可视化清理助手 · 无限层级下钻版 (app.py)
================================================================
职责：以【只读】方式列出指定目录的第一层内容（文件夹 + 文件），
      并统计每个文件夹的总占用，供前端做"无限层级下钻"直到定位最大文件。

【安全声明 · 请放心阅读】
  1. 本文件不含任何删除 / 移动 / 重命名 / 修改用户文件的逻辑，
     没有 os.remove、os.unlink、os.rmdir、shutil.rmtree 之类的调用，
     也没有导入 shutil / subprocess / webbrowser。
  2. 全流程只有两类操作：读取目录与文件信息（只读）、把统计报告写进
     本工具自己的目录（C_Drive_Scan_Report.txt）。
  3. 不调用 webbrowser，不会自己打开任何浏览器页面。
  4. 遇到没有权限的目录（PermissionError）一律跳过并标记为"下限值"。

【依赖】仅使用 Python 标准库（http.server），无需 pip 安装任何东西。

【接口】
  GET /                    前端页面
  GET /api/scan?path=...   列出该目录第一层，按占用降序返回前 10 项
                           &refresh=1 强制忽略缓存重新统计
  GET /api/progress        当前扫描进度（供前端显示）
  GET /api/ping            存活探测
  GET /report.txt          上一次扫描的纯文本报告

【用法】
  python app.py                      # 默认根目录 C:\\Users\\Administrator，端口 5000
  python app.py --port 5001
  python app.py --root "D:\\test"    # 指定根目录（测试用）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ----------------------------------------------------------------------------
# 基础配置
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(BASE_DIR, "index.html")
REPORT_FILE = os.path.join(BASE_DIR, "C_Drive_Scan_Report.txt")
EXPORT_FILE = os.path.join(BASE_DIR, "report.txt")   # 导出报告：路径写死，不接受任何用户输入的文件名

DEFAULT_ROOT = r"C:\Users\Administrator"   # 初始扫描目录（不含 C:\Windows 等系统目录）
TOP_N = 10                                 # 每一层只展示前 10 名
CACHE_TTL = 900.0                          # 目录体积缓存有效期（秒）
MAX_WALK_SECONDS = 300.0                   # 单个目录统计的安全上限，超时截断并标记为"下限值"

FILE_ATTRIBUTE_REPARSE_POINT = 0x400       # 符号链接 / 目录联接，跳过以免重复与死循环
WINDOWS_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")   # 只接受 Windows 绝对路径

GB = 1024.0 ** 3
MB = 1024.0 ** 2
KB = 1024.0

# ---- 文件类型统计 ----
EXT_TOP_N = 8                              # 徽章只展示占用最大的前 8 种类型
EXT_KEY_CAP = 48                           # 单个目录最多保留多少种扩展名，溢出的并入 OTHER_EXT（防内存膨胀）
OTHER_EXT = "其它"                          # 伪类型：溢出 / 长尾汇总
NO_EXT = "无扩展名"                         # 没有扩展名（或后缀不像扩展名）的文件
EXT_VALID = re.compile(r"^\.[A-Za-z0-9_+-]{1,16}$")   # 上限 16：既能容纳 .CompositeFont，又能挡掉"根本不是后缀"的长尾巴

sys.setrecursionlimit(3000)   # 目录层级很深时也够用


def format_size(num_bytes: int) -> str:
    """把字节数格式化成人类可读的 GB / MB / KB。"""
    value = float(num_bytes or 0)
    if value >= GB:
        return "%.2f GB" % (value / GB)
    if value >= MB:
        return "%.1f MB" % (value / MB)
    if value >= KB:
        return "%.1f KB" % (value / KB)
    return "%d B" % int(value)


# ----------------------------------------------------------------------------
# 文件类型（扩展名）统计工具
#   ext_map 结构：{ ".log": [占用字节, 文件个数], ... }
#   用列表存 [bytes, count]，一次遍历同时拿到体积与数量；目录级缓存因此只多一个 dict。
# ----------------------------------------------------------------------------
def ext_of(name: str) -> str:
    """取文件扩展名（小写，含点）。没有后缀或后缀不像扩展名时归入"无扩展名"。"""
    if not name:
        return NO_EXT
    head, dot, tail = name.rpartition(".")
    if not dot or not head or not tail:
        return NO_EXT          # "README" / ".gitignore" / "foo." 都没有真正的扩展名
    ext = "." + tail.lower()
    return ext if EXT_VALID.match(ext) else NO_EXT


def _ext_add(ext_map: dict, ext: str, size: int) -> None:
    """把一个文件计入某个类型。"""
    slot = ext_map.get(ext)
    if slot is None:
        ext_map[ext] = [size, 1]
    else:
        slot[0] += size
        slot[1] += 1


def _ext_merge(dst: dict, src: dict) -> None:
    """把子目录的类型统计合并进父目录（值会复制，不与缓存里的列表共享对象）。"""
    for key, slot in (src or {}).items():
        target = dst.get(key)
        if target is None:
            dst[key] = [slot[0], slot[1]]
        else:
            target[0] += slot[0]
            target[1] += slot[1]


def _ext_cap(ext_map: dict, cap: int = EXT_KEY_CAP) -> dict:
    """类型过多时，把占用最小的若干种并进"其它"，保证单目录内存有上限。"""
    if len(ext_map) <= cap:
        return ext_map
    prev_other = ext_map.pop(OTHER_EXT, [0, 0])
    ranked = sorted(ext_map.items(), key=lambda kv: kv[1][0])   # 升序：先丢最小的
    overflow_bytes, overflow_count = prev_other[0], prev_other[1]
    drop = len(ranked) - max(cap - 1, 1)
    for key, slot in ranked[:drop]:
        overflow_bytes += slot[0]
        overflow_count += slot[1]
        del ext_map[key]
    ext_map[OTHER_EXT] = [overflow_bytes, overflow_count]
    return ext_map


def _ext_dominant(ext_map: dict) -> tuple[str, int] | None:
    """取某个目录里占用最大的类型（忽略"其它"这种伪类型），用于给文件夹加一个类型标签。"""
    best_key, best_bytes = None, -1
    for key, slot in (ext_map or {}).items():
        if key == OTHER_EXT:
            continue
        if slot[0] > best_bytes:
            best_key, best_bytes = key, slot[0]
    if best_key is None:
        return None
    return best_key, max(best_bytes, 0)


# ----------------------------------------------------------------------------
# 扫描进度（纯内存状态）
# ----------------------------------------------------------------------------
class Progress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = {
            "running": False, "done": False, "root": "", "current": "",
            "started_at": None, "finished_at": None,
            "folders": 0, "files": 0, "bytes": 0, "skipped": 0, "error": None,
        }

    def begin(self, root: str) -> None:
        with self._lock:
            self._data.update(running=True, done=False, root=root, current=root,
                              started_at=time.time(), finished_at=None,
                              folders=0, files=0, bytes=0, skipped=0, error=None)

    def tick(self, current: str, folders: int, files: int, total_bytes: int, skipped: int) -> None:
        with self._lock:
            self._data.update(current=current, folders=folders,
                              files=files, bytes=total_bytes, skipped=skipped)

    def add_skip(self, n: int = 1) -> None:
        with self._lock:
            self._data["skipped"] += n

    def finish(self, error: str | None = None) -> None:
        with self._lock:
            self._data.update(running=False, done=True,
                              finished_at=time.time(), error=error)

    def snapshot(self) -> dict:
        with self._lock:
            data = dict(self._data)
        started = data.get("started_at")
        ended = data.get("finished_at") or time.time()
        data["elapsed"] = round(ended - started, 1) if started else 0.0
        data["bytes_gb"] = round(data["bytes"] / GB, 3)
        return data


PROGRESS = Progress()

# 目录信息缓存：path -> {bytes, largest_path, largest_name, largest_size, partial, ts}
# 首次完整统计会把整棵子树的每一层都写进缓存，之后每次下钻都是毫秒级。
_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()

# 同一时刻只允许一个扫描任务，避免重复压榨硬盘
SCAN_LOCK = threading.Lock()

# 运行时确定的扫描根目录（由命令行 --root 决定）
SCAN_ROOT: str | None = None


# ----------------------------------------------------------------------------
# 只读扫描核心
# ----------------------------------------------------------------------------
def _is_reparse_point(entry: os.DirEntry) -> bool:
    """符号链接 / 目录联接：跳过，避免循环统计与重复计数。"""
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def cache_get(path: str, refresh: bool) -> dict | None:
    if refresh:
        return None
    with _CACHE_LOCK:
        entry = _CACHE.get(path)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > CACHE_TTL:
        return None
    return entry


def cache_put(path: str, info: dict) -> None:
    info["ts"] = time.time()
    with _CACHE_LOCK:
        _CACHE[path] = info


def _walk(path: str, deadline: float, depth: int = 0) -> dict:
    """
    递归统计一个目录：总大小 + 其中最大的单个文件 + 各文件类型的占用。
    同时把【每一层子目录】的结果都写进缓存，所以后续下钻无需重新遍历。

    文件类型的统计是"顺路"完成的：反正每个文件都已经 stat 过，不增加任何额外磁盘 IO。
    任何 PermissionError / OSError 都只标记 partial（该数值为下限）并继续，
    绝不抛出异常导致中断。
    """
    total = 0
    largest_path = None
    largest_name = None
    largest_size = -1
    partial = False
    folders_here = 0
    files_here = 0
    ext_here: dict = {}

    if time.time() > deadline:
        # 安全上限：不再深入，标记为下限值，避免极端目录卡死服务
        info = {"bytes": 0, "largest_path": None, "largest_name": None,
                "largest_size": 0, "partial": True, "folders": 0, "files": 0, "ext": {}}
        cache_put(path, info)
        return info

    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _is_reparse_point(entry):
                            PROGRESS.add_skip()
                            continue
                        sub = _walk(entry.path, deadline, depth + 1)
                        total += sub["bytes"]
                        partial = partial or sub["partial"]
                        _ext_merge(ext_here, sub.get("ext"))
                        if sub["largest_size"] > largest_size:
                            largest_size = sub["largest_size"]
                            largest_path = sub["largest_path"]
                            largest_name = sub["largest_name"]
                        folders_here += 1 + sub.get("folders", 0)
                        files_here += sub.get("files", 0)
                    elif entry.is_file(follow_symlinks=False):
                        size = entry.stat(follow_symlinks=False).st_size
                        total += size
                        files_here += 1
                        _ext_add(ext_here, ext_of(entry.name), size)
                        if size > largest_size:
                            largest_size = size
                            largest_path = entry.path
                            largest_name = entry.name
                except PermissionError:
                    partial = True
                    PROGRESS.add_skip()
                except OSError:
                    partial = True
                    PROGRESS.add_skip()
    except PermissionError:
        partial = True
        PROGRESS.add_skip()
    except OSError:
        partial = True
        PROGRESS.add_skip()

    info = {
        "bytes": total,
        "largest_path": largest_path,
        "largest_name": largest_name,
        "largest_size": max(largest_size, 0),
        "partial": partial,
        "folders": folders_here,
        "files": files_here,
        "ext": _ext_cap(ext_here),
    }
    cache_put(path, info)
    PROGRESS.tick(path, folders_here, files_here, total, PROGRESS.snapshot()["skipped"])
    return info


def dir_info(path: str, refresh: bool = False) -> dict:
    """取目录统计信息：优先用缓存，否则完整统计一次。"""
    cached = cache_get(path, refresh)
    if cached:
        return cached
    try:
        return _walk(path, time.time() + MAX_WALK_SECONDS)
    except RecursionError:
        info = {"bytes": 0, "largest_path": None, "largest_name": None,
                "largest_size": 0, "partial": True, "folders": 0, "files": 0, "ext": {}}
        cache_put(path, info)
        return info


def is_windows_abs(path: str) -> bool:
    return bool(WINDOWS_ABS.match(path or ""))


def build_breadcrumb(path: str) -> list[dict]:
    """把 C:\\Users\\Administrator\\AppData 变成可点击的面包屑数组。"""
    crumbs: list[dict] = []
    cur = path
    for _ in range(64):
        name = os.path.basename(cur.rstrip("\\/"))
        if name:
            crumbs.append({"name": name, "path": cur})
        else:
            drive = os.path.splitdrive(cur)[0]
            label = (drive[:1].upper() + "盘") if drive[:1] else cur
            crumbs.append({"name": label, "path": cur})
            break
        parent = os.path.dirname(cur.rstrip("\\/"))
        if not parent or parent == cur:
            break
        cur = parent
    crumbs.reverse()
    return crumbs


def _public_item(item: dict) -> dict:
    """
    把内部条目转成给前端/导出用的结构。
    文件 -> ext 就是它自己的扩展名；文件夹 -> ext 是它内部占用最大的类型（"多为 .log" 那种提示）。
    """
    if item["type"] == "dir":
        dom = _ext_dominant(item.get("ext") or {})
        ext_label = dom[0] if dom else None
        ext_bytes = dom[1] if dom else 0
    else:
        ext_label = ext_of(item["name"])
        ext_bytes = item["size"]
    return {
        "name": item["name"],
        "path": item["path"],
        "type": item["type"],
        "size": item["size"],
        "size_formatted": item["size_formatted"],
        "size_gb": item["size_gb"],
        "size_mb": item["size_mb"],
        "percent": item["percent"],
        "partial": item["partial"],
        "ext": ext_label,
        "ext_bytes": ext_bytes,
    }


def scan_path(path: str, refresh: bool = False, with_all: bool = False) -> dict:
    """
    列出 path 的第一层内容（文件夹 + 文件），按占用降序返回前 TOP_N 项。
    文件夹的占用来自完整统计（带缓存）；文件用 stat 直接取大小，瞬时完成。

    额外产出：
      · file_types       —— 当前层【整棵子树】按扩展名聚合的占用（占用最大的前 EXT_TOP_N 种）
      · file_types_other —— 长尾 + 溢出类型的合计（界面上显示为"其它"）
      · all_items        —— 仅当 with_all=True 时附带（导出报告用，避免污染常规响应）
    """
    started = time.time()
    PROGRESS.begin(path)

    result = {
        "ok": True,
        "path": path,
        "parent": None,
        "breadcrumb": [],
        "items": [],
        "item_count": 0,
        "truncated": False,
        "total_bytes": 0,
        "total_formatted": "0 B",
        "skipped": 0,
        "duration_sec": 0.0,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "biggest_file": None,
        "file_types": [],
        "file_types_other": None,
        "file_types_total_bytes": 0,
        "warning": None,
        "error": None,
        "report_file": REPORT_FILE,
        "export_file": EXPORT_FILE,
    }

    if not os.path.isdir(path):
        result["ok"] = False
        result["error"] = "目录不存在或不可访问：%s" % path
        PROGRESS.finish(result["error"])
        return result

    entries: list[dict] = []
    skipped = 0
    warning = None

    with SCAN_LOCK:
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if _is_reparse_point(entry):
                                skipped += 1
                                continue
                            info = dir_info(entry.path, refresh=refresh)
                            entries.append({
                                "name": entry.name,
                                "path": entry.path,
                                "type": "dir",
                                "size": info["bytes"],
                                "partial": info["partial"],
                                "largest_path": info["largest_path"],
                                "largest_name": info["largest_name"],
                                "largest_size": info["largest_size"],
                                "ext": info.get("ext") or {},
                            })
                        elif entry.is_file(follow_symlinks=False):
                            size = entry.stat(follow_symlinks=False).st_size
                            entries.append({
                                "name": entry.name,
                                "path": entry.path,
                                "type": "file",
                                "size": size,
                                "partial": False,
                                "largest_path": entry.path,
                                "largest_name": entry.name,
                                "largest_size": size,
                            })
                    except PermissionError:
                        skipped += 1
                        PROGRESS.add_skip()
                    except OSError:
                        skipped += 1
                        PROGRESS.add_skip()
        except PermissionError:
            warning = "没有访问权限，无法读取该目录（已自动跳过）。"
        except OSError as exc:
            warning = "读取目录失败：%s（已自动跳过）。" % exc

    total_bytes = sum(item["size"] for item in entries)
    entries.sort(key=lambda item: item["size"], reverse=True)

    # 当前层"最大的单个文件"（精确值，包含各子目录内部）
    biggest = None
    for item in entries:
        if item["largest_path"] and item["largest_size"] > (biggest["size"] if biggest else -1):
            biggest = {
                "path": item["largest_path"],
                "name": item["largest_name"],
                "size": item["largest_size"],
                "partial": item["partial"],
            }

    for item in entries:
        item["percent"] = round(item["size"] * 100.0 / total_bytes, 2) if total_bytes else 0.0
        item["size_formatted"] = format_size(item["size"])
        item["size_gb"] = round(item["size"] / GB, 3)
        item["size_mb"] = round(item["size"] / MB, 2)

    # ---- 文件类型聚合：当前层整棵子树（直接文件的类型 + 每个子目录缓存里的类型） ----
    type_totals: dict = {}
    for item in entries:
        if item["type"] == "dir":
            _ext_merge(type_totals, item.get("ext"))
        else:
            _ext_add(type_totals, ext_of(item["name"]), item["size"])
    type_total_bytes = sum(slot[0] for slot in type_totals.values())

    ranked = sorted(type_totals.items(), key=lambda kv: kv[1][0], reverse=True)
    real_types = [(key, slot) for key, slot in ranked if key != OTHER_EXT]
    head = real_types[:EXT_TOP_N]
    tail = real_types[EXT_TOP_N:]
    overflow = type_totals.get(OTHER_EXT, [0, 0])

    def _pct(num: int) -> float:
        return round(num * 100.0 / type_total_bytes, 2) if type_total_bytes else 0.0

    result["file_types"] = [
        {"ext": key, "bytes": slot[0], "count": slot[1],
         "formatted": format_size(slot[0]), "percent": _pct(slot[0])}
        for key, slot in head
    ]
    other_bytes = overflow[0] + sum(slot[0] for _, slot in tail)
    other_count = overflow[1] + sum(slot[1] for _, slot in tail)
    result["file_types_other"] = {
        "ext": OTHER_EXT,
        "bytes": other_bytes,
        "count": other_count,
        "formatted": format_size(other_bytes),
        "percent": _pct(other_bytes),
        "kinds": len(tail) + (1 if OTHER_EXT in type_totals else 0),
    }
    result["file_types_total_bytes"] = type_total_bytes

    crumbs = build_breadcrumb(path)
    result.update(
        items=[_public_item(item) for item in entries[:TOP_N]],
        item_count=len(entries),
        truncated=len(entries) > TOP_N,
        total_bytes=total_bytes,
        total_formatted=format_size(total_bytes),
        skipped=skipped,
        duration_sec=round(time.time() - started, 1),
        breadcrumb=crumbs,
        parent=crumbs[-2]["path"] if len(crumbs) >= 2 else None,
        warning=warning,
    )
    if biggest:
        biggest["size_formatted"] = format_size(biggest["size"])
        biggest["size_gb"] = round(biggest["size"] / GB, 3)
        biggest["parent"] = os.path.dirname(biggest["path"])
        result["biggest_file"] = biggest

    if with_all:
        result["all_items"] = [_public_item(item) for item in entries]

    PROGRESS.finish(None)
    write_report(result)
    return result


# ----------------------------------------------------------------------------
# 报告输出（唯一写操作，位置固定在本工具目录内）
# ----------------------------------------------------------------------------
def write_report(result: dict) -> None:
    if not result.get("ok"):
        return
    lines = [
        "=" * 78,
        " C盘空间可视化清理助手 · 扫描报告（当前层）",
        "=" * 78,
        "扫描时间 : %s" % result["scanned_at"],
        "当前路径 : %s" % result["path"],
        "扫描用时 : %s 秒" % result["duration_sec"],
        "本层合计 : %s  (%s 字节)" % (result["total_formatted"], format(result["total_bytes"], ",")),
        "条目数量 : %s（显示前 %s 项）" % (format(result["item_count"], ","), len(result["items"])),
        "跳过项目 : %s（没有权限或读取失败）" % format(result["skipped"], ","),
    ]
    if result.get("warning"):
        lines.append("提示信息 : %s" % result["warning"])
    if result.get("biggest_file"):
        bf = result["biggest_file"]
        lines.append("最大文件 : %s  (%s)" % (bf["path"], bf["size_formatted"]))
    lines += [
        "-" * 78,
        " 排名   类型   占用            占比      名称",
        "-" * 78,
    ]
    for index, item in enumerate(result["items"], 1):
        lines.append(" %2d   %-4s %-14s %6.2f%%   %s"
                     % (index, "目录" if item["type"] == "dir" else "文件",
                        ("≥" if item["partial"] else "") + item["size_formatted"],
                        item["percent"], item["name"]))
    lines += [
        "-" * 78,
        " 提示：本工具只做安全扫描，绝不删除任何文件，请根据结果自行手动清理。",
        "=" * 78,
    ]
    with open(REPORT_FILE, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# ----------------------------------------------------------------------------
# 导出报告（本轮新增）：把当前层【全部条目 + 文件类型统计】写成 report.txt
#   唯一写操作之一，文件名与目录都写死在本工具目录内，不接受任何外部输入。
#   编码用 utf-8-sig（带 BOM）+ CRLF，双击用记事本打开不会乱码、不会挤成一行。
# ----------------------------------------------------------------------------
def write_export(result: dict) -> dict:
    """写出 report.txt，返回文件信息（供前端展示与打开链接）。"""
    all_items = result.get("all_items") or result.get("items") or []
    types = result.get("file_types") or []
    other = result.get("file_types_other") or None

    lines = [
        "=" * 90,
        " C盘空间可视化清理助手 · 导出报告（终极进化版）",
        "=" * 90,
        "导出时间 : %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "当前路径 : %s" % result.get("path", ""),
        "扫描时间 : %s      统计用时 : %s 秒" % (result.get("scanned_at", ""), result.get("duration_sec", 0)),
        "本层合计 : %s  (%s 字节)" % (result.get("total_formatted", "0 B"), format(result.get("total_bytes", 0), ",")),
        "条目数量 : %s 项（本文件完整导出全部条目）" % format(len(all_items), ","),
        "跳过项目 : %s（没有权限或读取失败）" % format(result.get("skipped", 0), ","),
    ]
    if result.get("warning"):
        lines.append("提示信息 : %s" % result["warning"])
    if result.get("biggest_file"):
        bf = result["biggest_file"]
        lines.append("最大文件 : %s  (%s)" % (bf["path"], bf["size_formatted"]))

    lines += [
        "-" * 90,
        " 一、文件类型统计（当前层整棵子树，按占用降序）",
        "-" * 90,
        " 排名  文件类型            占用              占比       文件数",
        "-" * 90,
    ]
    for index, item in enumerate(types, 1):
        lines.append(" %2d   %-16s %-16s %8.2f%%   %s"
                     % (index, item["ext"], item["formatted"], item["percent"], format(item["count"], ",")))
    if other and (other.get("bytes") or other.get("count")):
        lines.append("  -   %-16s %-16s %8.2f%%   %s"
                     % (other["ext"] + "(%d 种)" % other.get("kinds", 0), other["formatted"],
                        other["percent"], format(other["count"], ",")))
    if not types and not other:
        lines.append("  （该目录下没有可统计的文件）")

    lines += [
        "-" * 90,
        " 二、全部条目（路径 / 大小 / 文件类型）",
        "-" * 90,
        " 排名  类型  占用              占比       文件类型       路径",
        "-" * 90,
    ]
    for index, item in enumerate(all_items, 1):
        is_dir = item["type"] == "dir"
        ext_label = item.get("ext") or "-"
        if is_dir and item.get("ext"):
            ext_label = "%s 为主" % item["ext"]      # 目录显示"内部占用最大的类型"
        lines.append(" %2d   %-4s %-16s %7.2f%%   %-13s %s"
                     % (index, "目录" if is_dir else "文件",
                        ("≥" if item.get("partial") else "") + item["size_formatted"],
                        item["percent"], ext_label, item["path"]))

    lines += [
        "-" * 90,
        " 说明：目录的体积为递归统计值（可能因无权限目录而偏小，标 ≥ 的为下限值）；",
        "       目录那一列的\"文件类型\"表示该目录内部占用最大的类型。",
        " 提示：本工具只做安全扫描，绝不删除任何文件，请根据结果自行手动清理。",
        "=" * 90,
    ]

    text = "\n".join(lines) + "\n"
    with open(EXPORT_FILE, "w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write(text)

    return {
        "file": EXPORT_FILE,
        "url": "/export.txt",
        "bytes": len(text.encode("utf-8-sig")),
        "lines": len(lines),
        "item_count": len(all_items),
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def resolve_root(root: str | None) -> str:
    """确定实际扫描根目录：优先命令行指定，其次 C:\\Users\\Administrator，再次当前用户目录。"""
    candidates = [root, DEFAULT_ROOT, os.path.expanduser("~"),
                  os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users")]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(root or DEFAULT_ROOT)


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "CDriveScanTool/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    # --- 响应工具 -----------------------------------------------------------
    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_json(self, payload, status: int = 200) -> None:
        self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _send_text(self, text: str, status: int = 200) -> None:
        self._send(text.encode("utf-8"), "text/plain; charset=utf-8", status)

    def _send_file(self, path: str, content_type: str, encoding: str = "utf-8") -> None:
        try:
            with open(path, "r", encoding=encoding) as handle:
                body = handle.read()
        except OSError:
            self._send_text("文件不存在：%s" % os.path.basename(path), 404)
            return
        self._send(body.encode("utf-8"), content_type)

    # --- 路由 ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if route in ("/", "/index.html"):
                self._send_file(INDEX_FILE, "text/html; charset=utf-8")

            elif route == "/api/ping":
                self._send_json({"ok": True, "service": "C_Drive_Scan_Tool",
                                 "version": "3.0",
                                 # 注意：必须保留 drilldown 子串，start.bat 靠它判断"是不是本工具的新版本"
                                 "feature": "drilldown,types,export",
                                 "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

            elif route == "/api/progress":
                self._send_json({"ok": True, "progress": PROGRESS.snapshot()})

            elif route == "/api/scan":
                raw = str(query.get("path", [""])[0]).strip().strip('"')
                refresh = str(query.get("refresh", ["0"])[0]).lower() in ("1", "true", "yes")

                if raw:
                    if not is_windows_abs(raw):
                        self._send_json({"ok": False, "path": raw,
                                         "error": "路径必须是 Windows 绝对路径，例如 C:\\Users"}, 400)
                        return
                    target = os.path.abspath(raw)
                else:
                    target = resolve_root(SCAN_ROOT)

                print("[扫描] %s%s" % (target, "（强制刷新）" if refresh else ""))
                result = scan_path(target, refresh=refresh)
                if result.get("ok"):
                    print("[扫描] 完成：%s 项，合计 %s，用时 %s 秒%s"
                          % (result["item_count"], result["total_formatted"],
                             result["duration_sec"],
                             "（含无权限目录，数值为下限）" if result.get("warning") else ""))
                else:
                    print("[扫描] 失败：%s" % result.get("error"))
                self._send_json(result)

            elif route == "/api/export":
                raw = str(query.get("path", [""])[0]).strip().strip('"')
                if raw:
                    if not is_windows_abs(raw):
                        self._send_json({"ok": False, "path": raw,
                                         "error": "路径必须是 Windows 绝对路径，例如 C:\\Users"}, 400)
                        return
                    target = os.path.abspath(raw)
                else:
                    target = resolve_root(SCAN_ROOT)

                print("[导出] 正在生成报告：%s" % target)
                # with_all=True：把当前层的【全部】条目带上（不只是前 10 条）；
                # 走同一套缓存，所以刚扫过的话这里是毫秒级完成。
                result = scan_path(target, refresh=False, with_all=True)
                if not result.get("ok"):
                    self._send_json({"ok": False, "error": result.get("error") or "扫描失败"}, 400)
                    print("[导出] 失败：%s" % result.get("error"))
                    return
                info = write_export(result)
                print("[导出] 完成：%s（%s 字节，%s 条）" % (info["file"], info["bytes"], info["item_count"]))
                self._send_json({"ok": True, **info})

            elif route == "/export.txt":
                if os.path.isfile(EXPORT_FILE):
                    # 落盘是 utf-8-sig（记事本友好）；发给浏览器时用 utf-8-sig 读掉 BOM，页面更干净
                    self._send_file(EXPORT_FILE, "text/plain; charset=utf-8", encoding="utf-8-sig")
                else:
                    self._send_text("还没有导出报告，请先在页面上点击「导出报告」。", 404)

            elif route == "/report.txt":
                if os.path.isfile(REPORT_FILE):
                    self._send_file(REPORT_FILE, "text/plain; charset=utf-8")
                else:
                    self._send_text("还没有生成报告，请先在页面上点击「开始扫描」。", 404)

            elif route == "/favicon.ico":
                self._send(b"", "image/x-icon", 204)

            else:
                self._send_text("404 Not Found: %s" % self.path, 404)

        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._send_json({"ok": False, "error": "服务器内部错误，请查看控制台日志"}, 500)
            except Exception:
                pass

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


class ScanServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def port_is_free(host: str, port: int) -> bool:
    """严格探测端口是否真的空闲（Windows 下避免 SO_REUSEADDR 的假绑定）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main() -> int:
    global SCAN_ROOT

    parser = argparse.ArgumentParser(description="C盘空间可视化清理助手 · 后台服务（只读扫描）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机可访问）")
    parser.add_argument("--port", type=int, default=5000, help="监听端口，默认 5000")
    parser.add_argument("--root", default=None, help="初始扫描目录，默认 C:\\Users\\Administrator")
    args = parser.parse_args()

    SCAN_ROOT = args.root
    root = resolve_root(args.root)

    if not os.path.isfile(INDEX_FILE):
        print("[错误] 找不到界面文件：%s" % INDEX_FILE)
        print("       请确认 index.html 与 app.py 放在同一个目录下。")
        return 1

    httpd = None
    port = args.port
    for _ in range(20):
        if port_is_free(args.host, port):
            try:
                httpd = ScanServer((args.host, port), Handler)
                break
            except OSError as exc:
                print("[警告] 端口 %d 绑定失败：%s" % (port, exc))
        else:
            print("[警告] 端口 %d 已被占用，尝试下一个端口 ..." % port)
        port += 1

    if httpd is None:
        print("[错误] 连续尝试 20 个端口都被占用，请关闭占用端口的程序后重试。")
        return 1

    url = "http://%s:%d" % (args.host, port)
    print("=" * 64)
    print("  C盘空间可视化清理助手 · 无限层级下钻版 已启动")
    print("=" * 64)
    print("  界面地址 : %s" % url)
    print("  初始目录 : %s" % root)
    print("  报告文件 : %s" % REPORT_FILE)
    print("  安全说明 : 只读扫描，绝不删除任何文件（本程序无删除逻辑）")
    print("-" * 64)
    print("  关闭本窗口或按 Ctrl+C 即可停止服务。")
    print("=" * 64)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[信息] 收到停止信号，服务已退出。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
