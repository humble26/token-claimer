#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Token 领取助手
到点自动启动 AI Agent 桌面端（ZCode / WorkBuddy / Trae CN / TRAE SOLO CN）
领取每日 token；启动后附带无障碍参数，自动在界面里查找并点击
"领取/签到"类按钮（智能点击层，见 uia_click.py），保持设定时长后
自动关闭，避免客户端常驻占用内存。

依赖：Python 3.8+ 标准库（tkinter）；可选 `pip install comtypes` 启用智能点击。
用法：
    python token_claimer.py            打开图形界面
    python token_claimer.py --now      立即执行一次领取（命令行，无界面）
    python token_claimer.py --now --dry-run   只演示计划，不真的启动
    python token_claimer.py --inspect WorkBuddy.exe   枚举界面元素
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import uia_click  # 智能点击（可选增强，内部检测 comtypes 是否可用）

APP_TITLE = "Token 领取助手"
APP_VERSION = "v1.2.0"
MUTEX_NAME = "TokenClaimer_SingleInstance_ZWB"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

LOCAL = os.environ.get("LOCALAPPDATA", "")
PF = os.environ.get("ProgramFiles", r"C:\Program Files")

# 各应用的候选安装路径（依次探测，取第一个存在的；界面上可手动改）
DEFAULT_APPS = [
    {
        "key": "zcode", "label": "ZCode", "enabled": True,
        "candidates": [r"E:\Zcode\ZCode.exe",
                       rf"{LOCAL}\Programs\ZCode\ZCode.exe",
                       rf"{PF}\Zcode\ZCode.exe"],
    },
    {
        "key": "workbuddy", "label": "WorkBuddy", "enabled": True,
        "candidates": [r"E:\workbuddy\WorkBuddy.exe",
                       rf"{LOCAL}\Programs\WorkBuddy\WorkBuddy.exe",
                       rf"{PF}\WorkBuddy\WorkBuddy.exe"],
    },
    {
        "key": "trae", "label": "Trae CN", "enabled": True,
        "candidates": [r"E:\Trae CN\Trae CN.exe",
                       rf"{LOCAL}\Programs\Trae CN\Trae CN.exe"],
    },
    {
        "key": "trae_solo", "label": "TRAE SOLO CN（可选）", "enabled": False,
        "candidates": [rf"{LOCAL}\Programs\TRAE SOLO CN\TRAE SOLO CN.exe"],
    },
]

CLICK_KEYWORDS_DEFAULT = ["领取", "签到", "加油站", "免费"]

# 个别客户端的领取入口名称跟通用关键词对不上，这里按应用预置覆盖。
# config.json 的 per_app_click 里同名项优先级更高（见 ClaimEngine.per_app_click_cfg）。
DEFAULT_PER_APP_CLICK = {
    # TraeWork CN 的领取入口在左下角账户菜单里，行名是「每日领 100 积分」，
    # 右侧按钮未领取时显示「领取」、已领取时显示「今日已签」。
    # 只匹配这两者，避开同一菜单里的「立即升级」「管理账户」「免费」等无关控件。
    # 注意：该入口需要先点开账户菜单才会出现在界面树里，单次点击模型无法完成
    # 「开菜单 → 点领取」两步，详见 README「已知限制」。
    "trae_solo": {"keywords": ["每日领", "领取"]},
}

# 每个应用可以单独选领取日，用来错开频率（例如某个客户端只在周末领）。
# 集合里的数字对应 datetime.weekday()：周一=0 … 周日=6。
DAY_PRESETS = {
    "daily": {0, 1, 2, 3, 4, 5, 6},
    "weekday": {0, 1, 2, 3, 4},
    "weekend": {5, 6},
}
DAY_ORDER = ("daily", "weekend", "weekday")
DAY_LABELS = {"daily": "每天", "weekend": "仅周末", "weekday": "仅工作日"}
DAY_BY_LABEL = {v: k for k, v in DAY_LABELS.items()}


def day_matches(app: dict, now: datetime) -> bool:
    """该应用今天是否在领取日；未配置 days 的旧配置一律按「每天」处理。"""
    return now.weekday() in DAY_PRESETS.get(app.get("days") or "daily",
                                            DAY_PRESETS["daily"])


def system_dpi_scale() -> float:
    """系统 DPI 缩放系数（96 = 100%）。窗口尺寸需按它放大，否则高缩放屏
    上字体变大而窗口不变，底部控件会被挤出可视区。"""
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi:
            return dpi / 96
    except Exception:
        pass
    try:
        dc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 90)  # LOGPIXELSY
        ctypes.windll.user32.ReleaseDC(0, dc)
        return dpi / 96
    except Exception:
        return 1.0


# ---------------------------------------------------------------- 配置 ------
def config_dir() -> Path:
    """配置与日志存放目录：脚本/EXE 同目录，不可写时退回 %APPDATA%。"""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    try:
        (base / ".write_test").touch()
        (base / ".write_test").unlink()
        return base
    except OSError:
        fallback = Path(os.environ.get("APPDATA", base)) / "TokenClaimer"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class AppConfig:
    def __init__(self):
        self.path = config_dir() / "config.json"
        self.data = self._load()

    def _defaults(self) -> dict:
        apps = []
        for d in DEFAULT_APPS:
            exe = next((c for c in d["candidates"] if c and Path(c).exists()), "")
            apps.append({"key": d["key"], "label": d["label"],
                         "exe": exe, "enabled": d["enabled"], "days": "daily"})
        return {
            "apps": apps,
            "mode": "daily",            # daily=每天定时  interval=固定间隔
            "daily_times": ["08:00", "12:00", "20:00"],
            "interval_minutes": 720,
            "keep_minutes": 10,         # 启动后保持多少分钟，0=不自动关闭
            "skip_if_running": True,    # 已在运行的客户端跳过（也不关闭它）
            "catchup_missed": True,     # 错过时间点后在宽限期内补领
            "grace_minutes": 120,
            "autostart": False,
            "first_run_done": False,
            "fired_log": {},            # {"2026-09-26 08:00": "2026-09-26T08:00:05"}
            # 智能点击：启动后在界面里找"领取/签到"类按钮并自动点
            "click_enabled": True,
            "click_keywords": list(CLICK_KEYWORDS_DEFAULT),
            "click_wait_window": 25,    # 启动后等待窗口出现的秒数
            "click_retry_seconds": 15,  # 找不到按钮时的重试间隔
            "click_max_attempts": 10,   # 最多尝试次数
            "per_app_click": {},        # 按应用覆盖，见 README
        }

    def _load(self) -> dict:
        data = self._defaults()
        try:
            stored = json.loads(self.path.read_text("utf-8"))
            if isinstance(stored, dict):
                for k, v in stored.items():
                    if k in data:
                        data[k] = v
        except (OSError, ValueError):
            pass
        # 应用列表按 key 合并：保留用户已改的路径，新版本新增的应用自动补上
        by_key = {a.get("key"): a for a in data["apps"]}
        merged = []
        for d in DEFAULT_APPS:
            saved = by_key.get(d["key"], {})
            exe = saved.get("exe") or next(
                (c for c in d["candidates"] if c and Path(c).exists()), "")
            merged.append({"key": d["key"], "label": d["label"], "exe": exe,
                           "enabled": saved.get("enabled", d["enabled"]),
                           "days": saved.get("days") or "daily"})
        data["apps"] = merged
        return data

    def save(self):
        data = dict(self.data)
        # 只保留最近 7 天的触发记录，防止无限膨胀
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        data["fired_log"] = {k: v for k, v in data["fired_log"].items()
                             if k.startswith("interval") or k[:10] >= cutoff}
        try:
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except OSError as e:
            print(f"[warn] 配置保存失败: {e}", file=sys.stderr)


# ---------------------------------------------------------------- 引擎 ------
class ClaimEngine:
    """与界面无关的领取逻辑，GUI 与命令行共用。"""

    def __init__(self, cfg: AppConfig, log=print):
        self.cfg = cfg
        self.log = log

    # ---- 进程操作 ----
    def image_name(self, app) -> str:
        return Path(app["exe"]).name if app.get("exe") else ""

    def is_running(self, image: str) -> bool:
        if not image:
            return False
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, errors="ignore",
                creationflags=CREATE_NO_WINDOW, timeout=15)
            return image.lower() in (r.stdout or "").lower()
        except (OSError, subprocess.SubprocessError):
            return False

    def launch(self, exe: str, args: tuple = ()):
        exe_path = Path(exe)
        subprocess.Popen(
            [str(exe_path), *args], cwd=str(exe_path.parent),
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True)

    def close_graceful(self, image: str) -> bool:
        return self._taskkill(["taskkill", "/IM", image])

    def close_force(self, image: str) -> bool:
        return self._taskkill(["taskkill", "/IM", image, "/T", "/F"])

    def _taskkill(self, cmd) -> bool:
        try:
            subprocess.run(cmd, capture_output=True, text=True, errors="ignore",
                           creationflags=CREATE_NO_WINDOW, timeout=30)
            return True
        except (OSError, subprocess.SubprocessError) as e:
            self.log(f"⚠ 关闭 {cmd[2] if len(cmd) > 2 else ''} 失败: {e}", "warn")
            return False

    # ---- 调度判断 ----
    def eligible_apps(self, apps: list[dict], now: datetime) -> list[dict]:
        """筛掉今天不在领取日的应用，并在日志里说明跳过了谁。

        定时触发与手动「立即领取」都走这里：领取日是用户对"这个客户端
        什么时候该领"的约束，手动触发也不该绕过它。
        """
        ok, skipped = [], []
        for a in apps:
            (ok if day_matches(a, now) else skipped).append(a)
        if skipped:
            names = "、".join(a["label"] for a in skipped)
            self.log(f"· 今天不在领取日，跳过：{names}")
        return ok

    def poll_due(self, now: datetime | None = None) -> list[dict]:
        """检查是否有到点的计划；有则标记已触发并返回到点的应用列表（裸字典）。"""
        now = now or datetime.now()
        d = self.cfg.data
        fired, due = False, []
        if not d["first_run_done"]:
            # 刚安装首次打开时不自动触发，避免突然拉起全部客户端；
            # 之后错过补领照常生效，也可手动点「立即领取」。
            return []
        if d["mode"] == "daily":
            for t in sorted(set(d["daily_times"])):
                m = TIME_RE.match(t)
                if not m:
                    continue
                due_dt = now.replace(hour=int(m.group(1)),
                                     minute=int(m.group(2)),
                                     second=0, microsecond=0)
                delta = (now - due_dt).total_seconds()
                key = f"{due_dt:%Y-%m-%d} {t}"
                if delta < 0 or key in d["fired_log"]:
                    continue
                grace = max(d["grace_minutes"] * 60 if d["catchup_missed"] else 0, 60)
                if delta <= grace:
                    d["fired_log"][key] = now.isoformat(timespec="seconds")
                    fired = True
                    late = "（错过补领）" if delta > 60 else ""
                    self.log(f"⏰ 到点 {t}{late}")
                    due.extend(a for a in d["apps"] if a["enabled"])
        else:  # interval
            key = "interval-last"
            last = d["fired_log"].get(key)
            gap = max(int(d["interval_minutes"]), 1) * 60
            if not last or (now - datetime.fromisoformat(last)).total_seconds() >= gap:
                d["fired_log"][key] = now.isoformat(timespec="seconds")
                fired = True
                self.log(f"⏰ 间隔模式到点（每 {d['interval_minutes']} 分钟）")
                due.extend(a for a in d["apps"] if a["enabled"])
        if fired:
            self.cfg.save()
        return due

    def next_run_text(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        d = self.cfg.data
        if d["mode"] == "daily":
            times = sorted(set(t for t in d["daily_times"] if TIME_RE.match(t)))
            if not times:
                return "未设置时间"
            for t in times:
                dt = now.replace(hour=int(t[:2]), minute=int(t[-2:]), second=0)
                if dt > now:
                    return self._human(dt, now)
            tomorrow = now + timedelta(days=1)
            dt = tomorrow.replace(hour=int(times[0][:2]), minute=int(times[0][-2:]),
                                  second=0, microsecond=0)
            return self._human(dt, now)
        last = d["fired_log"].get("interval-last")
        if not last:
            return "就绪（首次触发待运行）"
        dt = datetime.fromisoformat(last) + timedelta(minutes=max(int(d["interval_minutes"]), 1))
        return self._human(dt, now)

    @staticmethod
    def _human(dt: datetime, now: datetime) -> str:
        secs = int((dt - now).total_seconds())
        h, m = secs // 3600, secs % 3600 // 60
        return f"{dt:%m-%d %H:%M}（还有 {h} 小时 {m} 分）" if secs > 0 else "即将执行"

    # ---- 执行一次领取 ----
    def plan_claim(self, due_apps: list[dict] | None = None,
                   now: datetime | None = None) -> list[dict]:
        """返回本次领取计划 [{app, will_launch}]；仅做判断不启动。

        due_apps=None 表示"全部启用的应用"（手动/命令行触发）；
        传 poll_due 的结果（可为空列表）表示只处理到点的应用。
        已在运行的应用也纳入计划（will_launch=False）：智能点击仍会到
        它的界面里找领取按钮，但不会重复启动、也不会替用户关闭它。
        """
        now = now or datetime.now()
        if due_apps is None:
            apps = [a for a in self.cfg.data["apps"] if a["enabled"]]
        else:  # 同一秒可能命中多个时间点，按 key 去重
            seen, apps = set(), []
            for a in due_apps:
                if a["key"] not in seen:
                    seen.add(a["key"])
                    apps.append(a)
        # 领取日过滤放在这里，定时与手动两条路径就都覆盖到了
        apps = self.eligible_apps(apps, now)
        plan = []
        for a in apps:
            if not a["exe"] or not Path(a["exe"]).exists():
                self.log(f"⚠ {a['label']}：未找到程序，跳过（请在界面设置路径）", "warn")
                continue
            image = self.image_name(a)
            if self.cfg.data["skip_if_running"] and self.is_running(image):
                self.log(f"– {a['label']} 已在运行，跳过启动")
                plan.append({"app": a, "will_launch": False})
                continue
            plan.append({"app": a, "will_launch": True})
        return plan

    def smart_click_active(self) -> bool:
        return bool(self.cfg.data["click_enabled"]) and uia_click.available()

    def per_app_click_cfg(self, app: dict) -> dict:
        """该应用的点击配置：预置值打底，config.json 里的同名项覆盖。

        预置值走代码而不是写进 _defaults()，是为了让老配置（已经生成过
        config.json、per_app_click 为空）也能自动拿到，不必删配置重来。
        """
        key = app.get("key", "")
        merged = dict(DEFAULT_PER_APP_CLICK.get(key, {}))
        merged.update(self.cfg.data.get("per_app_click", {}).get(key, {}))
        return merged

    def launch_extra_args(self, app: dict) -> list[str]:
        """启动参数：per_app 覆盖，或智能点击开启时带无障碍参数。"""
        ov = self.per_app_click_cfg(app)
        if "launch_args" in ov:
            return list(ov["launch_args"])
        if self.smart_click_active():
            return [uia_click.A11Y_FLAG]
        return []

    def click_settings_for(self, app: dict) -> tuple[list[str], tuple | None]:
        ov = self.per_app_click_cfg(app)
        keywords = ov.get("keywords") or list(self.cfg.data["click_keywords"])
        point = tuple(ov["point"]) if ov.get("point") else None
        return keywords, point

    def do_launch(self, app: dict) -> bool:
        # 必须兜住所有异常：本函数在 Tkinter 的 after 回调里执行，而程序以
        # pythonw（无控制台）运行时，未捕获的异常会被静默丢弃 —— 表现为
        # 「点了立即领取，日志却停在那里，客户端一个都没起来」。
        try:
            extra = self.launch_extra_args(app)
            self.launch(app["exe"], tuple(extra))
            suffix = f"，参数 {' '.join(extra)}" if extra else ""
            self.log(f"✔ 已启动 {app['label']}（{app['exe']}）{suffix}")
            return True
        except Exception as e:
            self.log(f"⚠ 启动 {app['label']} 失败: {e!r}", "warn")
            return False

    # ---- 智能点击任务 ----
    def make_click_job(self, app: dict, will_launch: bool, start_ts: float) -> dict:
        d = self.cfg.data
        keywords, point = self.click_settings_for(app)
        wait = max(int(d["click_wait_window"]), 5)
        retry = max(int(d["click_retry_seconds"]), 5)
        keep = int(d["keep_minutes"])
        if keep > 0:   # 关闭前留出点击窗口
            deadline = start_ts + keep * 60 - 45
        else:
            deadline = start_ts + wait + int(d["click_max_attempts"]) * retry
        return {
            "label": app["label"], "image": self.image_name(app),
            "keywords": keywords, "point": point,
            "next": start_ts + (wait if will_launch else 3),
            "retry": retry, "attempts": 0,
            "max": max(int(d["click_max_attempts"]), 3),
            "deadline": max(deadline, start_ts + wait + 15),
            "done": False, "hinted": False, "clicked": set(),
        }

    def process_click_job(self, job: dict, now_ts: float):
        if job["done"] or now_ts < job["next"]:
            return
        job["attempts"] += 1
        status, detail = uia_click.find_and_click(
            job["image"], job["keywords"],
            clicked_names=job["clicked"], point_pct=job["point"])
        give_up = job["attempts"] >= job["max"] or now_ts > job["deadline"]
        if status == "clicked":
            job["done"] = True
            self.log(f"✔ {job['label']}：已自动点击 {detail}", "ok")
        elif status == "already":
            job["done"] = True
            self.log(f"✔ {job['label']}：{detail}，无需再点", "ok")
        elif status == "no-window":
            if give_up:
                job["done"] = True
                self.log(f"⚠ {job['label']}：窗口始终未出现，放弃自动点击", "warn")
            else:
                job["next"] = now_ts + job["retry"]
        elif status == "no-tree":
            if not job["hinted"]:
                job["hinted"] = True
                self.log(f"⚠ {job['label']}：{detail}；请关闭它后由本工具重新"
                         "启动（自启的实例才带界面树）", "warn")
            job["next"] = now_ts + job["retry"]
        elif status in ("no-element", "blocked"):
            if give_up:
                job["done"] = True
                hint = f"可用 --inspect {job['image']} 查看界面元素名后调整关键词" \
                    if status == "no-element" else detail
                self.log(f"⚠ {job['label']}：{job['attempts']} 次尝试未点到按钮，"
                         f"放弃自动点击（{hint}）", "warn")
            else:
                job["next"] = now_ts + job["retry"]
        else:  # error
            job["done"] = True
            self.log(f"⚠ {job['label']}：自动点击出错 {detail}", "warn")


# ---------------------------------------------------------------- 自启 ------
# ⚠ 写 .vbs 时踩过的四个坑（都在这几个函数里一并解决）：
#   1. VBScript 里字符串的唯一转义是「把引号双写」，反斜杠没有特殊含义；
#      用 json.dumps 生成会把 " 变成 \"，被 VBS 解析成「反斜杠 + 字符串结束」→ 语法错误。
#   2. json.dumps 还会把每个 \ 翻倍成 \\，即使引号侥幸通过，路径也指向不存在的位置。
#   3. json.dumps 默认 ensure_ascii=True，会把中文目录名（如 12-Token领取助手）
#      转义成 \u9886\u53d6... 这样的字面量 —— 路径依然找不到。
#   4. WSH 默认按 ANSI 读取 .vbs，UTF-8 写入的中文路径会乱码。
#      带 BOM 的 UTF-16LE 是 WSH 明确支持的 Unicode 脚本格式，与系统区域设置无关。
_VBS_ENCODING = "utf-16"   # Python 的 "utf-16" 会自动写入 BOM


def _vbs_literal(s: str) -> str:
    """把普通字符串变成 VBScript 字符串字面量。

    只做引号双写 —— 不转义反斜杠，也不做 ASCII 转义（见上方第 1~3 条）。
    """
    return '"' + s.replace('"', '""') + '"'


def autostart_vbs_path() -> Path:
    startup = Path(os.environ.get("APPDATA", "")) / \
        "Microsoft/Windows/Start Menu/Programs/Startup"
    return startup / "Token领取助手_自启动.vbs"


def autostart_enabled() -> bool:
    return autostart_vbs_path().exists()


def autostart_target() -> str:
    """自启要执行的命令行（纯函数，便于测试）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    runner = pyw if pyw.exists() else Path(sys.executable)
    return f'"{runner}" "{Path(__file__).resolve()}"'


def autostart_vbs_text(target: str | None = None) -> str:
    """自启 .vbs 的完整内容（纯函数，便于测试）。"""
    t = autostart_target() if target is None else target
    return (
        'Set ws = CreateObject("WScript.Shell")\r\n'
        f'ws.Run {_vbs_literal(t)}, 0, False\r\n'
    )


def set_autostart(enable: bool) -> str:
    vbs = autostart_vbs_path()
    try:
        if enable:
            vbs.parent.mkdir(parents=True, exist_ok=True)
            # newline="" 防止 Python 把 \n 再翻译一次；文件编码必须让 WSH 能认出 Unicode
            with open(vbs, "w", encoding=_VBS_ENCODING, newline="") as f:
                f.write(autostart_vbs_text())
        else:
            vbs.unlink(missing_ok=True)
    except OSError as e:
        return f"操作失败: {e}"
    return "已开启开机自启" if enable else "已关闭开机自启"


# ---------------------------------------------------------------- GUI ------
class App:
    # ---- 设计系统（扁平浅色主题） ----
    C_BG = "#F3F5F9"          # 窗口底
    C_CARD = "#FFFFFF"        # 卡片
    C_BORDER = "#E4E8F0"      # 卡片描边
    C_TEXT = "#1F2733"        # 主文字
    C_SUB = "#7B8494"         # 次要文字
    C_ACCENT = "#2F6BFF"      # 强调色
    C_ACCENT_DK = "#245BD9"
    C_ACCENT_SOFT = "#EAF0FF"
    C_OK = "#149E52"          # 成功
    C_OK_SOFT = "#E6F6ED"
    C_DANGER = "#DC2626"
    C_DANGER_SOFT = "#FDECEC"
    C_GRAY_SOFT = "#F0F2F6"   # 中性徽章底
    C_LOG_BG = "#161B26"      # 日志面板
    C_LOG_FG = "#C9D2E3"

    def __init__(self, root: tk.Tk, cfg: AppConfig | None = None):
        self.root = root
        self.cfg = cfg or AppConfig()
        self.engine = ClaimEngine(self.cfg, log=self.log)
        self.close_queue: list[list] = []   # [触发时间戳, 镜像名, 阶段0/1]
        self.click_jobs: list[dict] = []    # 智能点击任务
        # pythonw 无控制台，Tk 回调里未捕获的异常默认被直接丢弃；接管后
        # 至少会落到日志里，不至于「点了没反应，又查无痕迹」。
        self.root.report_callback_exception = self._on_tk_error
        self._build_ui()
        # 自动保存基线：构建完界面后记录当前快照
        self.collect_settings()
        self._settings_snapshot = json.dumps(
            self.cfg.data, ensure_ascii=False, sort_keys=True, default=str)
        self._dirty_since: float | None = None
        self._status_countdown = 0
        self._first_run_notice()
        self.tick()
        self.status_tick()

    # ---- 设计系统构件 ----
    def _fonts(self):
        self.f_title = ("Microsoft YaHei UI", 15, "bold")
        self.f_section = ("Microsoft YaHei UI", 10, "bold")
        self.f_body = ("Microsoft YaHei UI", 10)
        self.f_small = ("Microsoft YaHei UI", 9)
        self.f_mono = ("Consolas", 9)

    def _card(self, parent, title: str, body_fill: str = "x", **pack_kw):
        """白色扁平卡片：标题条（强调色竖标 + 粗体）+ 内容区。

        pack_kw 透传给卡片自身的 pack（如 pady）。
        """
        card = tk.Frame(parent, bg=self.C_CARD,
                        highlightbackground=self.C_BORDER, highlightthickness=1)
        card.pack(fill="x", **pack_kw)
        head = tk.Frame(card, bg=self.C_CARD)
        head.pack(fill="x", padx=14, pady=(12, 0))
        tk.Frame(head, bg=self.C_ACCENT, width=4, height=15).pack(side="left")
        tk.Label(head, text=title, bg=self.C_CARD, fg=self.C_TEXT,
                 font=self.f_section).pack(side="left", padx=(8, 0))
        body = tk.Frame(card, bg=self.C_CARD)
        body.pack(fill=body_fill, expand=(body_fill == "both"),
                  padx=14, pady=(6, 12))
        return body

    def _btn(self, parent, text, command, kind="ghost", small=False):
        bg, fg, active, hl = {
            "accent": (self.C_ACCENT, "#FFFFFF", self.C_ACCENT_DK, 0),
            "ghost": ("#FFFFFF", self.C_TEXT, "#F1F4F9", 1),
            "danger": ("#FFFFFF", self.C_DANGER, self.C_DANGER_SOFT, 1),
        }[kind]
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=active, activeforeground=fg,
                         relief="flat", bd=0, cursor="hand2", font=self.f_body,
                         padx=8 if small else 16, pady=3 if small else 6,
                         highlightthickness=hl,
                         highlightbackground=self.C_BORDER)

    def _check(self, parent, text, var, **kw):
        kw.setdefault("font", self.f_body)
        return tk.Checkbutton(parent, text=text, variable=var, bg=self.C_CARD,
                              fg=self.C_TEXT, activebackground=self.C_CARD,
                              activeforeground=self.C_TEXT, highlightthickness=0,
                              anchor="w", **kw)

    def _radio(self, parent, text, value):
        """分段式单选（indicatoron=0：选中后整块变成浅蓝底）。"""
        return tk.Radiobutton(parent, text=text, value=value,
                              variable=self.mode_var, indicatoron=0,
                              bg="#FFFFFF", selectcolor=self.C_ACCENT_SOFT,
                              fg=self.C_TEXT, activebackground="#FFFFFF",
                              relief="flat", highlightthickness=1,
                              highlightbackground=self.C_BORDER,
                              font=self.f_body, padx=14, pady=5,
                              cursor="hand2", command=self.on_mode_change)

    def _pill(self, parent, text, fg, bg):
        return tk.Label(parent, text=text, fg=fg, bg=bg, font=self.f_small,
                        padx=10, pady=3)

    @staticmethod
    def _int_of(var, default: int, minimum: int = 0) -> int:
        """读 Spinbox 的 IntVar；内容被清空/非法时回退默认值，不抛异常。"""
        try:
            return max(int(var.get()), minimum)
        except (tk.TclError, TypeError, ValueError):
            return max(default, minimum)

    # ---------- 界面 ----------
    def _build_ui(self):
        self._fonts()
        s = system_dpi_scale()
        self.root.title(f"{APP_TITLE} {APP_VERSION}")
        self.root.geometry(f"{int(900 * s)}x{int(885 * s)}")
        self.root.minsize(int(860 * s), int(845 * s))
        self.root.configure(bg=self.C_BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 状态栏（先占住底部）
        self.status_bar = tk.Label(self.root, bg="#EAEDF3", fg=self.C_SUB,
                                   font=self.f_small, anchor="w", padx=14,
                                   pady=5)
        self.status_bar.pack(fill="x", side="bottom")

        # 顶栏（强调色横幅）
        header = tk.Frame(self.root, bg=self.C_ACCENT)
        header.pack(fill="x")
        tk.Label(header, text=APP_TITLE, bg=self.C_ACCENT, fg="#FFFFFF",
                 font=self.f_title).pack(side="left", padx=(18, 8), pady=14)
        tk.Label(header, text=APP_VERSION, bg=self.C_ACCENT, fg="#BFD2FF",
                 font=self.f_small).pack(side="left")
        right = tk.Frame(header, bg=self.C_ACCENT)
        right.pack(side="right", padx=18)
        tk.Label(right, text="下次执行", bg=self.C_ACCENT, fg="#BFD2FF",
                 font=self.f_small).pack(side="left", padx=(0, 8))
        self.next_pill = tk.Label(right, text="…", bg="#FFFFFF",
                                  fg=self.C_ACCENT_DK,
                                  font=("Microsoft YaHei UI", 10, "bold"),
                                  padx=14, pady=6)
        self.next_pill.pack(side="left", pady=10)

        body = tk.Frame(self.root, bg=self.C_BG)
        body.pack(fill="both", expand=True)
        pad_top = {"pady": (12, 0)}

        # ① 应用
        apps_body = self._card(body, "应用（勾选后到点自动启动）", **pad_top)
        self.app_vars, self.status_labels, self.exe_entries = [], [], []
        self.day_vars, self.day_boxes = [], []
        for a in self.cfg.data["apps"]:
            row = tk.Frame(apps_body, bg=self.C_CARD)
            row.pack(fill="x", pady=3)
            var = tk.BooleanVar(value=a["enabled"])
            self.app_vars.append(var)
            self._check(row, a["label"], var, width=17,
                        font=self.f_section,
                        command=self.on_toggle_app).pack(side="left")
            entry = ttk.Entry(row, font=self.f_body)
            entry.insert(0, a["exe"])
            entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.exe_entries.append(entry)
            self._btn(row, "浏览…", lambda i=len(self.exe_entries) - 1:
                      self.pick_exe(i), small=True).pack(side="left")
            # 领取日：同一套定时计划下，各应用可错开频率（如仅周末领）
            day_var = tk.StringVar(
                value=DAY_LABELS.get(a.get("days") or "daily", DAY_LABELS["daily"]))
            self.day_vars.append(day_var)
            box = ttk.Combobox(row, textvariable=day_var, width=7,
                               state="readonly", font=self.f_body,
                               values=[DAY_LABELS[k] for k in DAY_ORDER])
            box.pack(side="left", padx=(8, 0))
            box.bind("<<ComboboxSelected>>",
                     lambda e: self.save_settings(quiet=True))
            self.day_boxes.append(box)
            self.status_labels.append(
                self._pill(row, "…", self.C_SUB, self.C_GRAY_SOFT))
            self.status_labels[-1].pack(side="left", padx=(10, 0))
        tk.Label(apps_body, fg=self.C_SUB, bg=self.C_CARD, justify="left",
                 font=self.f_small, text=(
                     "路径框右侧的下拉框是「领取日」：到点时只在选定的日子启动该应用，"
                     "用来错开频率（例如只在周末领的客户端选「仅周末」）。")).pack(
            anchor="w", pady=(6, 0))

        # ② 定时计划
        sched_body = self._card(body, "定时计划", **pad_top)
        row1 = tk.Frame(sched_body, bg=self.C_CARD)
        row1.pack(fill="x", pady=(0, 8))
        self.mode_var = tk.StringVar(value=self.cfg.data["mode"])
        self._radio(row1, "每天定时（多个时间点）", "daily").pack(side="left")
        self._radio(row1, "固定间隔", "interval").pack(side="left", padx=(8, 4))
        self.interval_var = tk.IntVar(value=self.cfg.data["interval_minutes"])
        self.interval_spin = ttk.Spinbox(row1, from_=5, to=10080, increment=5,
                                         width=7, font=self.f_body,
                                         textvariable=self.interval_var)
        self.interval_spin.pack(side="left")
        tk.Label(row1, text="分钟执行一次", bg=self.C_CARD, fg=self.C_SUB,
                 font=self.f_body).pack(side="left", padx=(4, 0))
        row2 = tk.Frame(sched_body, bg=self.C_CARD)
        row2.pack(fill="x")
        self.time_list = tk.Listbox(row2, height=4, width=9,
                                    exportselection=False, relief="flat",
                                    highlightthickness=1,
                                    highlightbackground=self.C_BORDER,
                                    selectbackground=self.C_ACCENT,
                                    selectforeground="#FFFFFF",
                                    font=self.f_body, activestyle="none")
        self.time_list.pack(side="left", fill="y")
        for t in sorted(set(self.cfg.data["daily_times"])):
            self.time_list.insert("end", t)
        btns = tk.Frame(row2, bg=self.C_CARD)
        btns.pack(side="left", padx=10)
        self.time_entry = ttk.Entry(btns, width=8, font=self.f_body,
                                    justify="center")
        self.time_entry.insert(0, "08:00")
        self.time_entry.pack(anchor="w", ipady=2)
        self._btn(btns, "＋ 添加", self.add_time, small=True).pack(
            anchor="w", pady=(6, 0), ipadx=6)
        self._btn(btns, "－ 删除所选", self.del_time, small=True).pack(
            anchor="w", pady=4, ipadx=6)
        self.daily_hint = tk.Label(
            row2, fg=self.C_SUB, bg=self.C_CARD, justify="left",
            font=self.f_small, text=(
                "到点后依次启动勾选的应用，登录后即完成每日 token 领取。\n"
                "电脑当时关机/睡眠错过时间点，开启「错过补领」后会在宽限期\n"
                "内自动补执行。"))
        self.daily_hint.pack(side="left", padx=18, pady=2)

        # ③ 启动后行为
        beh = self._card(body, "启动后行为", **pad_top)
        row3 = tk.Frame(beh, bg=self.C_CARD)
        row3.pack(fill="x", pady=2)
        tk.Label(row3, text="启动并保持", bg=self.C_CARD, fg=self.C_TEXT,
                 font=self.f_body).pack(side="left")
        self.keep_var = tk.IntVar(value=self.cfg.data["keep_minutes"])
        ttk.Spinbox(row3, from_=0, to=720, increment=1, width=6,
                    font=self.f_body, textvariable=self.keep_var).pack(
            side="left", padx=6)
        tk.Label(row3, text="分钟后自动关闭（0 = 保持打开不关闭）",
                 bg=self.C_CARD, fg=self.C_SUB,
                 font=self.f_body).pack(side="left")
        row4 = tk.Frame(beh, bg=self.C_CARD)
        row4.pack(fill="x", pady=(6, 0))
        self.skip_var = tk.BooleanVar(value=self.cfg.data["skip_if_running"])
        self._check(row4, "已在运行的客户端跳过启动（且不关闭它）",
                    self.skip_var).pack(side="left")
        self.catchup_var = tk.BooleanVar(value=self.cfg.data["catchup_missed"])
        self._check(row4, "错过时间点后补领，宽限",
                    self.catchup_var).pack(side="left", padx=(24, 2))
        self.grace_var = tk.IntVar(value=self.cfg.data["grace_minutes"])
        ttk.Spinbox(row4, from_=10, to=720, increment=10, width=6,
                    font=self.f_body, textvariable=self.grace_var).pack(
            side="left", padx=2)
        tk.Label(row4, text="分钟内有效", bg=self.C_CARD, fg=self.C_SUB,
                 font=self.f_body).pack(side="left")

        # ④ 智能点击
        click_body = self._card(
            body, "智能点击（自动查找并点击界面里的领取按钮）", **pad_top)
        row5 = tk.Frame(click_body, bg=self.C_CARD)
        row5.pack(fill="x", pady=2)
        self.click_var = tk.BooleanVar(value=self.cfg.data["click_enabled"])
        self._check(row5, "启用", self.click_var,
                    command=self.on_click_toggle).pack(side="left")
        tk.Label(row5, text="按钮关键词", bg=self.C_CARD, fg=self.C_SUB,
                 font=self.f_body).pack(side="left", padx=(14, 4))
        self.click_keywords_var = tk.StringVar(
            value="、".join(self.cfg.data["click_keywords"]))
        self.click_keywords_entry = ttk.Entry(
            row5, width=28, font=self.f_body,
            textvariable=self.click_keywords_var)
        self.click_keywords_entry.pack(side="left", ipady=2)
        row6 = tk.Frame(click_body, bg=self.C_CARD)
        row6.pack(fill="x", pady=(6, 0))
        self.click_wait_var = tk.IntVar(value=self.cfg.data["click_wait_window"])
        self.click_retry_var = tk.IntVar(value=self.cfg.data["click_retry_seconds"])
        self.click_max_var = tk.IntVar(value=self.cfg.data["click_max_attempts"])
        self.click_spins = []
        for text, var, hi in (("窗口等待", self.click_wait_var, 300),
                              ("重试间隔", self.click_retry_var, 300),
                              ("最多尝试", self.click_max_var, 60)):
            tk.Label(row6, text=text, bg=self.C_CARD, fg=self.C_SUB,
                     font=self.f_body).pack(side="left", padx=(0, 3))
            spin = ttk.Spinbox(row6, from_=1, to=hi, width=5, font=self.f_body,
                               textvariable=var)
            spin.pack(side="left", padx=(0, 14))
            self.click_spins.append(spin)
        tk.Label(row6, text="秒", bg=self.C_CARD, fg=self.C_SUB,
                 font=self.f_body).pack(side="left")
        tk.Label(click_body, fg=self.C_SUB, bg=self.C_CARD, justify="left",
                 font=self.f_small, text=(
                     "客户端由本工具启动时附带无障碍参数，启动后自动查找含关键词的按钮并点击\n"
                     "（Invoke 优先，鼠标模拟兜底；\"今日已领\"等完成标识会被识别，不会乱点）。\n"
                     "按钮文案哪天变了：python token_claimer.py --inspect WorkBuddy.exe 查看\n"
                     "界面元素名，把新名字加进关键词；也可在 config.json 里配专属关键词/坐标。")).pack(
            anchor="w", pady=(6, 0))

        # 操作按钮
        btn_row = tk.Frame(body, bg=self.C_BG)
        btn_row.pack(fill="x", pady=(14, 0))
        self._btn(btn_row, "▶  立即领取", self.manual_claim,
                  kind="accent").pack(side="left")
        self._btn(btn_row, "保存设置", self.save_settings).pack(
            side="left", padx=10)
        self.autostart_btn = self._btn(btn_row, "", self.toggle_autostart)
        self.autostart_btn.pack(side="left")
        self.autostart_btn_text()
        self._btn(btn_row, "退出程序", self.quit_app, kind="danger").pack(
            side="right")

        # 日志（深色终端风）
        log_card = tk.Frame(body, bg=self.C_CARD,
                            highlightbackground=self.C_BORDER,
                            highlightthickness=1)
        log_card.pack(fill="both", expand=True, pady=(12, 12))
        log_head = tk.Frame(log_card, bg=self.C_CARD)
        log_head.pack(fill="x", padx=14, pady=(10, 4))
        tk.Frame(log_head, bg=self.C_ACCENT, width=4, height=15).pack(
            side="left")
        tk.Label(log_head, text="运行日志", bg=self.C_CARD, fg=self.C_TEXT,
                 font=self.f_section).pack(side="left", padx=(8, 0))
        log_body = tk.Frame(log_card, bg=self.C_LOG_BG)
        log_body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text = tk.Text(log_body, height=9, state="disabled",
                                bg=self.C_LOG_BG, fg=self.C_LOG_FG,
                                insertbackground="#FFFFFF",
                                selectbackground="#33415E",
                                font=self.f_mono, wrap="word", relief="flat",
                                padx=10, pady=8)
        scroll = ttk.Scrollbar(log_body, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for tag, color in (("info", self.C_LOG_FG), ("ok", "#5EE39A"),
                           ("warn", "#FFC24B"), ("head", "#7FB5FF")):
            self.log_text.tag_configure(tag, foreground=color)

        self.on_mode_change()
        self._set_click_state()

    def _first_run_notice(self):
        if not self.cfg.data["first_run_done"]:
            self.log(f"{APP_TITLE} {APP_VERSION} 首次启动：已自动探测各应用路径，"
                     "确认无误后点「保存设置」（后续改动会自动保存）。", "head")
            self.cfg.data["first_run_done"] = True
            self.cfg.save()
        else:
            self.log(f"{APP_TITLE} 已启动，定时任务运行中。", "head")

    # ---------- 小部件回调 ----------
    def pick_exe(self, idx: int):
        path = filedialog.askopenfilename(
            title=f"选择 {self.cfg.data['apps'][idx]['label']} 的主程序",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.exe_entries[idx].delete(0, "end")
            self.exe_entries[idx].insert(0, path)

    def on_toggle_app(self):
        self.save_settings(quiet=True)

    def on_click_toggle(self):
        self._set_click_state()
        self.save_settings(quiet=True)

    def on_mode_change(self):
        is_daily = self.mode_var.get() == "daily"
        state = "disabled" if is_daily else "normal"
        self.interval_spin.configure(state=state)
        for w in (self.time_list, self.time_entry):
            w.configure(state="normal" if is_daily else "disabled")
        self.daily_hint.configure(
            text="到点后依次启动勾选的应用，登录后即完成每日 token 领取。\n"
                 "电脑当时关机/睡眠错过时间点，开启「错过补领」后会在宽限期\n"
                 "内自动补执行。" if is_daily else
                 "每隔设定分钟数执行一次领取，适合想全天多次刷新的场景。")

    def _set_click_state(self):
        state = "normal" if self.click_var.get() else "disabled"
        self.click_keywords_entry.configure(state=state)
        for spin in self.click_spins:
            spin.configure(state=state)

    def add_time(self):
        t = self.time_entry.get().strip()
        if not TIME_RE.match(t):
            messagebox.showwarning("格式错误", "时间格式应为 HH:MM，例如 08:30")
            return
        h, m = int(t.split(":")[0]), int(t.split(":")[1])
        t = f"{h:02d}:{m:02d}"
        if t in set(self.time_list.get(0, "end")):
            return
        self.time_list.insert("end", t)
        items = sorted(self.time_list.get(0, "end"))
        self.time_list.delete(0, "end")
        for it in items:
            self.time_list.insert("end", it)

    def del_time(self):
        sel = self.time_list.curselection()
        if sel:
            self.time_list.delete(sel[0])
        elif self.time_list.size():
            messagebox.showinfo("提示", "请先在列表中选中一个时间")

    def autostart_btn_text(self):
        on = autostart_enabled()
        self.autostart_btn.configure(
            text=("🟢 开机自启：已开启" if on else "⚪ 开机自启：已关闭"))

    def toggle_autostart(self):
        msg = set_autostart(not autostart_enabled())
        self.autostart_btn_text()
        self.log(msg, "ok" if "开启" in msg and "关闭" not in msg else "info")
        self.cfg.data["autostart"] = autostart_enabled()
        self.cfg.save()

    # ---------- 设置读写 ----------
    def collect_settings(self):
        apps = self.cfg.data["apps"]
        for i, a in enumerate(apps):
            a["exe"] = self.exe_entries[i].get().strip()
            a["enabled"] = bool(self.app_vars[i].get())
            a["days"] = DAY_BY_LABEL.get(self.day_vars[i].get(), "daily")
        d = self.cfg.data
        d["mode"] = self.mode_var.get()
        d["daily_times"] = sorted(set(self.time_list.get(0, "end")))
        d["interval_minutes"] = self._int_of(self.interval_var, 720, 5)
        d["keep_minutes"] = self._int_of(self.keep_var, 10, 0)
        d["skip_if_running"] = bool(self.skip_var.get())
        d["catchup_missed"] = bool(self.catchup_var.get())
        d["grace_minutes"] = self._int_of(self.grace_var, 120, 10)
        d["click_enabled"] = bool(self.click_var.get())
        d["click_keywords"] = [
            s for s in re.split(r"[,，、\s]+", self.click_keywords_var.get().strip())
            if s] or list(CLICK_KEYWORDS_DEFAULT)
        d["click_wait_window"] = self._int_of(self.click_wait_var, 25, 5)
        d["click_retry_seconds"] = self._int_of(self.click_retry_var, 15, 5)
        d["click_max_attempts"] = self._int_of(self.click_max_var, 10, 3)

    def save_settings(self, quiet: bool = False):
        self.collect_settings()
        self.cfg.save()
        self._settings_snapshot = json.dumps(
            self.cfg.data, ensure_ascii=False, sort_keys=True, default=str)
        self._dirty_since = None
        self.log("✔ 设置已保存", "ok")
        if not quiet:
            messagebox.showinfo("已保存", "设置已保存，定时任务即刻按新计划生效。")

    def _maybe_autosave(self):
        """界面改动 1.5 秒无变化后自动落盘，免去"忘点保存"的隐患。"""
        self.collect_settings()
        snap = json.dumps(self.cfg.data, ensure_ascii=False, sort_keys=True,
                          default=str)
        if snap != self._settings_snapshot:
            self._settings_snapshot = snap
            self._dirty_since = time.time()
        elif self._dirty_since and time.time() - self._dirty_since >= 1.5:
            self._dirty_since = None
            self.cfg.save()
            self.log("✔ 设置已自动保存", "ok")

    # ---------- 领取执行 ----------
    def manual_claim(self):
        self.save_settings(quiet=True)
        self.log("—— 手动触发领取 ——", "head")
        self.run_claim(self.engine.plan_claim())

    def run_claim(self, plan: list[dict]):
        """错峰依次启动；登记关闭队列与智能点击任务。"""
        if not plan:
            self.log("没有启用的应用，无事可做。")
            return
        keep = self._int_of(self.keep_var, 10, 0)
        smart = self.engine.smart_click_active()
        for i, item in enumerate(plan):
            self.root.after(i * 3000,
                            lambda it=item: self.launch_one(it, keep, smart))

    def launch_one(self, item: dict, keep: int, smart: bool):
        app, will_launch = item["app"], item["will_launch"]
        if will_launch and self.engine.do_launch(app):
            if keep > 0:
                due = time.time() + keep * 60
                self.close_queue.append([due, self.engine.image_name(app), 0])
                self.log(f"… {app['label']} 将在 {keep} 分钟后自动关闭")
        if smart:
            self.click_jobs.append(
                self.engine.make_click_job(app, will_launch, time.time()))

    # ---------- 周期任务 ----------
    def tick(self):
        now = datetime.now()
        # 到点检查（poll_due 返回到点的应用，统一走 plan_claim 做运行检查）
        due = self.engine.poll_due(now)
        if due:
            self.log("—— 定时触发 ——", "head")
            self.run_claim(self.engine.plan_claim(due))
        # 关闭队列（两段式：先礼貌关闭，20 秒后仍存活则强制结束）
        nowts = time.time()
        remaining = []
        for item in self.close_queue:
            due_ts, image, stage = item
            if nowts < due_ts:
                remaining.append(item)
                continue
            if stage == 0:
                self.engine.close_graceful(image)
                self.log(f"正在关闭 {image}…")
                remaining.append([nowts + 20, image, 1])
            else:
                if self.engine.is_running(image):
                    self.engine.close_force(image)
                    self.log(f"✔ {image} 已强制结束", "ok")
                else:
                    self.log(f"✔ {image} 已正常退出", "ok")
        self.close_queue = remaining
        # 智能点击任务
        for job in self.click_jobs:
            self.engine.process_click_job(job, nowts)
        self.click_jobs = [j for j in self.click_jobs if not j["done"]]
        # 设置自动保存
        try:
            self._maybe_autosave()
        except Exception:
            pass
        self.root.after(1000, self.tick)

    def status_tick(self):
        parts = [f"模式：{'每天定时' if self.mode_var.get() == 'daily' else '固定间隔'}"]
        self._status_countdown -= 1
        running = 0
        if self._status_countdown <= 0:
            self._status_countdown = 5
            for i, a in enumerate(self.cfg.data["apps"]):
                exe = self.exe_entries[i].get().strip()
                image = Path(exe).name if exe else ""
                ok = self.engine.is_running(image)
                running += ok
                if ok:
                    self.status_labels[i].configure(
                        text="● 运行中", fg=self.C_OK, bg=self.C_OK_SOFT)
                else:
                    self.status_labels[i].configure(
                        text="○ 未运行", fg=self.C_SUB, bg=self.C_GRAY_SOFT)
            parts.append(f"运行中 {running} 个应用")
        parts.append(f"保持 {self._int_of(self.keep_var, 10)} 分钟后自动关闭")
        self.status_bar.configure(text="　·　".join(parts))
        self.next_pill.configure(text=self.engine.next_run_text())
        self.root.after(1000, self.status_tick)

    # ---------- 关闭行为 ----------
    def on_close(self):
        if messagebox.askyesno(
                "后台运行", "点「是」最小化到任务栏，定时任务继续运行；\n"
                "点「否」彻底退出程序（到点将不会自动领取）。",
                default="yes"):
            self.root.iconify()
        else:
            self.root.destroy()

    def quit_app(self):
        if messagebox.askokcancel("退出", "确定退出？定时领取将停止。"):
            self.root.destroy()

    # ---------- 日志 ----------
    def _on_tk_error(self, exc_type, exc_value, tb):
        """接管 Tkinter 回调异常：界面上一行摘要，完整堆栈追加到当日日志。"""
        self.log(f"⚠ 内部错误：{exc_type.__name__}: {exc_value}", "warn")
        try:
            log_dir = config_dir() / "logs"
            log_dir.mkdir(exist_ok=True)
            detail = "".join(traceback.format_exception(exc_type, exc_value, tb))
            with open(log_dir / f"{datetime.now():%Y%m%d}.log", "a",
                      encoding="utf-8") as f:
                f.write(detail + "\n")
        except OSError:
            pass

    def log(self, msg: str, tag: str = "info"):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n", tag)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass
        try:
            log_dir = config_dir() / "logs"
            log_dir.mkdir(exist_ok=True)
            with open(log_dir / f"{datetime.now():%Y%m%d}.log", "a",
                      encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ------------------------------------------------------------ 单实例锁 ------
def acquire_single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# ------------------------------------------------------------ 命令行 ------
def run_headless(dry_run: bool):
    cfg = AppConfig()
    eng = ClaimEngine(cfg, log=lambda msg, tag="info": print(msg))
    eng.log(f"{APP_TITLE} 命令行模式" + ("（演练，不实际启动）" if dry_run else ""),
            "head")
    plan = eng.plan_claim()
    if dry_run:
        if not plan:
            eng.log("没有启用的应用。")
        for it in plan:
            extra = eng.launch_extra_args(it["app"]) if it["will_launch"] else []
            act = "启动" if it["will_launch"] else "查找领取按钮（应用已在运行）"
            eng.log(f"[演练] {it['app']['label']}：将{act}"
                    + (f"，参数 {' '.join(extra)}" if extra else ""))
        return
    if not plan:
        eng.log("没有启用的应用。")
        return

    smart = eng.smart_click_active()
    keep = int(cfg.data["keep_minutes"] or 0)
    jobs: list[dict] = []
    closes: list[dict] = []
    for i, it in enumerate(plan):
        if it["will_launch"]:
            eng.do_launch(it["app"])
            if keep > 0:
                closes.append({"ts": time.time() + keep * 60,
                               "image": eng.image_name(it["app"]), "stage": 0})
            if i < len(plan) - 1:
                time.sleep(3)
        if smart:
            jobs.append(eng.make_click_job(it["app"], it["will_launch"],
                                           time.time()))
    if not jobs and not closes:
        eng.log("保持时长为 0 且未启用智能点击，客户端将保持打开。")
        return
    eng.log(f"进入后台阶段：智能点击 {'开' if smart else '关'}，"
            + (f"{keep} 分钟后自动关闭" if keep > 0 else "不自动关闭") + "…")
    while True:
        nowts = time.time()
        for job in jobs:
            eng.process_click_job(job, nowts)
        jobs = [j for j in jobs if not j["done"]]
        remaining = []
        for c in closes:
            if nowts < c["ts"]:
                remaining.append(c)
                continue
            if c["stage"] == 0:
                eng.close_graceful(c["image"])
                eng.log(f"正在关闭 {c['image']}…")
                remaining.append({"ts": nowts + 20, "image": c["image"],
                                  "stage": 1})
            else:
                if eng.is_running(c["image"]):
                    eng.close_force(c["image"])
                    eng.log(f"✔ {c['image']} 已强制结束", "ok")
                else:
                    eng.log(f"✔ {c['image']} 已正常退出", "ok")
        closes = remaining
        if not jobs and not closes:
            break
        time.sleep(1)
    eng.log("本次领取流程结束。")


def run_smoke():
    import tempfile
    root = tk.Tk()
    root.withdraw()
    cfg = AppConfig()
    cfg.path = Path(tempfile.gettempdir()) / "token_claimer_smoke.json"
    cfg.data["daily_times"] = []          # 冒烟测试不真触发启动
    cfg.data["catchup_missed"] = False
    app = App(root, cfg)
    root.update()
    times = app.time_list.get(0, "end")
    assert all(TIME_RE.match(t) for t in times), "时间格式异常"
    assert app.status_bar.cget("text"), "状态栏为空"
    root.destroy()
    print("SMOKE OK")


def main():
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--now", action="store_true", help="立即执行一次领取后退出")
    parser.add_argument("--dry-run", action="store_true", help="配合 --now，只演示")
    parser.add_argument("--inspect", metavar="镜像名",
                        help="列出某客户端界面的可交互元素名，"
                             "用于调整智能点击关键词（如 --inspect WorkBuddy.exe）")
    parser.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.smoke:
        run_smoke()
        return
    if args.inspect:
        if uia_click.available():
            for line in uia_click.dump_tree(args.inspect):
                print(line)
        else:
            print("需要先安装依赖: pip install comtypes")
        return
    if args.now:
        run_headless(args.dry_run)
        return
    if not acquire_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(APP_TITLE, "程序已在运行中（请查看任务栏）。")
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
