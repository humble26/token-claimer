#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Token 领取助手
到点自动启动 AI Agent 桌面端（ZCode / WorkBuddy / TraeWork CN）
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
import ctypes.wintypes
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
import captcha_solver  # 滑块验证码自动处理（路线 B，见 docs/验证码勘察报告.md）

APP_TITLE = "Token 领取助手"
APP_VERSION = "v1.4.0"
MUTEX_NAME = "TokenClaimer_SingleInstance_ZWB"
SW_RESTORE = 9
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
        # Trae CN 是编码 IDE，没有每日积分入口，默认不勾选。
        # （桌面上的 TraeCode CN 快捷方式也指向同一个 exe。）
        "key": "trae", "label": "Trae CN", "enabled": False,
        "candidates": [r"E:\Trae CN\Trae CN.exe",
                       rf"{LOCAL}\Programs\Trae CN\Trae CN.exe"],
    },
    {
        # TraeWork CN：产品已改名，但安装目录与 exe 仍沿用旧名 "TRAE SOLO CN"
        # （exe 的 ProductName 也还是 TRAE SOLO CN）。判断依据是窗口标题为
        # "TraeWork CN"，且桌面/开始菜单的 "TraeWork CN" 快捷方式指向此 exe。
        # 不要把它跟 E:\Trae CN 的 Trae CN 混为一谈，两者是两个不同产品。
        "key": "traework", "label": "TraeWork CN", "enabled": True,
        "candidates": [rf"{LOCAL}\Programs\TRAE SOLO CN\TRAE SOLO CN.exe"],
    },
]

CLICK_KEYWORDS_DEFAULT = ["领取", "签到", "加油站", "免费"]

# 滑块验证码自动处理的默认参数（config.json 的 captcha 块）。
# enabled 默认 False：关闭时领取主流程与不带本功能时完全一致（硬性回归要求）。
# route 目前只实现 screen（屏幕截图 + SendInput 拖拽）；cdp 是留给将来的升级位
# （勘察结论：ZCode 有应用级单实例锁，调试端口只能由本工具启动时带上）。
# drag_scale / drag_bias / piece_x0 是现场标定项，见 docs/验证码勘察报告.md 待校准清单。
CAPTCHA_DEFAULTS = {
    "enabled": False,
    "route": "screen",
    "max_attempts": 5,       # 单次验证码最多尝试几次，超过转人工
    "retry_seconds": 3,      # 两次尝试之间的间隔（秒），限频用
    "wait_seconds": 2.5,     # 点开入口后等浮层渲染的秒数
    "min_gap_score": 0.55,   # 缺口定位置信度门槛，低于它不拖（避免乱拖触发风控）
    "drag_scale": 1.0,       # 缺口像素 -> 拖动像素的缩放
    "drag_bias": 0.0,        # 拖动距离偏置
    "piece_x0": 0.0,         # 拼图块初始 x（相对拼图区左边缘）
    "piece_zone_pct": 15.0,  # 左侧多少百分比内视为"拼图块本身"，排除后重找
    "settle_seconds": 1.2,   # 松手后等结果的时间
    "debug_save": False,     # 失败时把合成图/标注图存到 logs/ 便于排查
}

# 个别客户端的领取入口名称跟通用关键词对不上，这里按应用预置覆盖。
# config.json 的 per_app_click 里同名项优先级更高（见 ClaimEngine.per_app_click_cfg）。
# 写法有两种，调度层会归一化成同一结构（见 ClaimEngine.click_steps_for）：
#   单步（大多数应用）：{"keywords": ["领取"]} 或 {"point": [x%, y%]}
#   多步（要先开菜单再点领取）：{"steps": [{...}, {...}]}
# 多步里每一步独立重试，上一步点成功后等 wait 秒再走下一步，给菜单/浮层
# 留出渲染时间；若中途发现"目标/完成标识已经在界面上"，会跳过后面的前置步骤
# 直接到最后一步，避免把已经展开的菜单又点关（toggle）。
DEFAULT_PER_APP_CLICK = {
    # TraeWork CN 的领取入口藏在左下角账户菜单里，要两步：
    #   1) 点开账户那一行（名字里带用户名「用户0433459512」）；
    #   2) 菜单展开后，点右侧的「签到」按钮。
    #
    # 菜单结构来自 2026-10-02 的 --open-menu 校准清单
    # （logs/ui_dump_traework_menu_20261002_165347.txt，窗口 2561x1529）：
    #   [ct50020] 用户0433459512              @(100,850)
    #   [ct50000] 3,539                       @(318,842)
    #   [ct50020] 每日领 100 积分              @(47,980)    ← 只是标签文字
    #   [ct50020] 升级会员，每日多领 100 积分    @(47,1012)
    #   [ct50000] 签到                        @(313,988) 102x37  ← 真按钮
    #   [ct50000] 管理账户 / 消息 / 语言 / 主题 / 设置 / 报告问题 / 退出登录
    "traework": {
        "steps": [
            # 区域限制是必须的，不是保险：账户行的名字里带用户名（实测
            # 「用户0433459512 用户0433459512 免费」），而聊天正文里也会出现
            # "用户"这两个字，且正文元素（ct50020）同样属于可点类型 ——
            # 不限区域就会点到对话上。
            # 区域按实测坐标：未展开时账户行 @(24,1463) 348x48，中心
            # (7.7%, 97.3%)，取左下角 25%x8% 的角。
            {"desc": "打开左下角账户菜单", "keywords": ["用户"],
             "region_pct": (0, 92, 25, 100), "wait": 1.2},
            # 第二步只能用「签到」：
            #   · 「每日领」命中的是左侧标签 [ct50020] @(47,980)，它是文本节点，
            #     而且在树序里排在真按钮前面 —— 引擎是"先匹配到的先点"，会点
            #     到标签上，等于白点；
            #   · 「领取」会命中聊天正文和左侧任务卡片（实测左侧面板里就有
            #     "修复自动领取Token问题"），这些元素同样可点，且排得更靠前。
            # 「签到」在整棵树里唯一命中右侧按钮；已领取时该行会显示完成标识，
            # 负向词（已领/已签/已完成）会先判成已完成，不会重复点。
            # 区域把 x 收到 17%：菜单右边缘 428px，聊天列从 452px 起，
            # 这样正文里万一出现"签到"也匹配不到。
            {"desc": "点击签到按钮", "keywords": ["签到"],
             "region_pct": (0, 40, 17, 100), "wait": 0.0},
        ],
    },
    # WorkBuddy 的「加油站」卡片不会自己出现在页面上，要点开头像才有入口，三步：
    #   1) 点左下角头像那一行（可点元素的名字就是账号昵称「Hush」）；
    #   2) 账户菜单里点「Buddy加油站 去邀约 最高得 650 积分/人」；
    #   3) 卡片展开后点「立即领取」。
    #
    # 踩过的坑：原来只有单步、关键词是通用的 ["领取","签到","加油站","免费"]。
    # 卡片展开后树里第一个含「加油站」的元素是「关闭 Buddy 加油站」（在真按钮
    # 前面），于是每次都把卡片点开又点关 —— 表现就是"只把加油站点开了，没点
    # 领取"。所以这里必须拆成多步，并且第 2 步的关键词要精确到「Buddy加油站」。
    #
    # 结构来自 2026-10-03 的实测清单（窗口 2561x1529，logs/ui_dump_workbuddy_*.txt）：
    #   头像      [ct50020] Hush                         @(437,1261)  → (17.1%, 82.5%)
    #   菜单项    [ct50000] Buddy加油站 去邀约 …           @(393,829) 307x88 → (21.3%, 57.1%)
    #   领取按钮  [ct50000] 立即领取                      @(399,1195) 101x29 → (17.6%, 79.1%)
    # 菜单里同屏还有「设置」「退出登录」「积分余额」，卡片里还有「已领」「累计领取」
    # 这些标签，都靠关键词 + 区域排除掉。
    "workbuddy": {
        "steps": [
            # 头像名字就是账号昵称。区域收到左下角，避免点到底部其它按钮。
            {"desc": "打开左下角账户菜单", "keywords": ["Hush"],
             "region_pct": (0, 72, 30, 100), "wait": 1.2},
            # 关键词既不能只写「加油站」，也不能只写「Buddy加油站」：
            #   · 「加油站」会命中卡片里的「关闭 Buddy 加油站」（排在真按钮前面）；
            #   · 「Buddy加油站」会命中卡片里的期数标签「Buddy加油站·10期」。
            # 菜单项的完整名字是「Buddy加油站 去邀约 最高得 650 积分/人」，取前缀
            # 「Buddy加油站 去邀约」（注意中间的空格，卡片里的标签没有空格、是「·」）
            # 就能只命中菜单项本身。
            {"desc": "点击 Buddy 加油站", "keywords": ["Buddy加油站 去邀约"],
             "region_pct": (0, 35, 40, 80), "wait": 1.2},
            # 「立即领取」在整棵树里唯一；已领取时该按钮会带完成标识，负向词
            # （已领/已签/已完成）先判成已完成，不会重复点。
            {"desc": "点击立即领取", "keywords": ["立即领取"],
             "region_pct": (0, 70, 30, 100), "wait": 0.0},
        ],
    },
    # ZCode 的领取入口是左下角侧边栏底部的推广 banner，无障碍树里只暴露成一个
    # 名为「打开」的按钮（旁边还有一个「关闭」）。点它之后弹出安全验证（滑块），
    # 需要人工拖滑块才能到账 —— 所以是「半自动」：工具负责把入口点开，人负责过
    # 验证。hint 字段会在点击成功后写进日志提醒用户。
    #
    # 结构来自 2026-10-03 的实测清单（窗口 1707x1019，logs/ui_dump_zcode_home.txt）：
    #   banner  [ct50000] 打开  @(16,865) 236x96  → 中心 (7.9%, 89.6%)
    #   关闭    [ct50000] 关闭  @(222,875) 20x20
    # 关键词「打开」太通用：工具栏的提示文本「要获取缺失的图片说明，请打开上下文
    # 菜单。」里也有「打开」，不圈区域就会命中它。区域取左下角 20%x20%
    # （窗口 1707x1019 时覆盖 x 0-341、y 815-1019），banner 中心落其中；
    # banner 贴着窗口底边，换任何窗口尺寸它的纵向百分比都稳定在 87%~93%。
    "zcode": {
        "steps": [
            {"desc": "点击左下角领取 banner", "keywords": ["打开"],
             "region_pct": (0, 80, 20, 100), "wait": 1.2},
        ],
        # captcha=True 表示这个应用的入口点开后会出现滑块验证码：config.json 的
        # captcha.enabled 打开时由工具自动过滑块（见 ClaimEngine._process_captcha），
        # 关闭时退回下面这句人工提示。两个开关是"与"的关系。
        "captcha": True,
        "hint": "请在 ZCode 弹窗中手动拖滑块完成安全验证，验证通过后 token 才会到账",
    },
}

# 这三个键决定"点哪里"，谁出现谁就是完整的写法（见 per_app_click_cfg）
CLICK_SHAPE_KEYS = ("steps", "keywords", "point")
DEFAULT_STEP_WAIT = 1.2   # 多步之间默认等待，给菜单/浮层渲染留时间

# ---- 点击节奏 ----
# 客户端启动和菜单展开都很快（实测界面树约 5 秒就长出来），固定死等"窗口等待"
# 那 25 秒纯属浪费。改成：启动后先等一小会儿，之后按退避节奏重试，一就绪立刻点。
START_DELAY = 2.5      # 启动客户端后第一次尝试前的等待（秒）
FAST_RETRY = 0.8       # 首次重试间隔（秒）
RETRY_BACKOFF = 1.7    # 每次失败后重试间隔的放大倍数，封顶为 click_retry_seconds
# "树是活的，但没找到这个按钮"时的重试上限。这类失败没有"等一等就好了"的成分：
# 界面树既然正常，按钮在不在几秒内就能确定。按 click_max_attempts(10) 一路退避到
# 15 秒一档会白转一分多钟（实测 ZCode banner 已隐藏时转了 78 秒）。6 次 ≈ 15 秒
# 足够覆盖"菜单刚展开还在渲染"；真正要等的是 no-window / no-tree，那边预算不动。
NO_ELEMENT_MAX_ATTEMPTS = 6

# ---- 界面树不可用时的自愈参数 ----
# Electron/Chromium 客户端只在"带 --force-renderer-accessibility 冷启动"时
# 才暴露完整界面树。实测两种失败形态：① 用户自己先开着客户端，工具启动时
# 只是把手交回旧实例，那个实例没带参数、树永远是空的；② 运行中窗口被最小化
# 或渲染进程重建后，树会永久塌到 8 个无名 Pane，本进程内 WM_GETOBJECT /
# oleacc 都无法把它叫回来。两种情况的唯一有效恢复手段都是冷启动。
NO_TREE_WAIT_LAUNCHED = 90.0   # 本工具自己启动的实例：树可能还在长，先等
NO_TREE_WAIT_RUNNING = 20.0    # 用户已在运行的实例：多半没带参数，早重启早好
RECOVER_MAX = 2                # 单次任务最多重启几次，防止"重启-失败"死循环
RECOVER_CLOSE_TIMEOUT = 20.0   # 优雅关闭后等它自己退出多久
RECOVER_KILL_TIMEOUT = 20.0    # 强杀后再等多久
RECOVER_POLL = 2.0             # 恢复各阶段的轮询间隔
RECOVER_SETTLE = 5.0           # 进程退出后先歇一下再拉起，避开脏的目录锁
RECOVER_BUDGET = 180.0         # 恢复占用时间不计入原任务的放弃预算


def _as_region(value) -> tuple[float, float, float, float] | None:
    """把配置里的 region_pct 归一成 (x0, y0, x1, y1)；不合法就返回 None。

    JSON 里存的是列表，代码里预置的是元组，统一在这里转。配置是手写的，
    写错了宁可当作"没配区域"（退回整窗查找），也不要抛异常把整次领取打断。
    """
    if not value:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if x0 >= x1 or y0 >= y1:
        return None
    return (x0, y0, x1, y1)


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
            "restart_if_no_tree": True,  # 界面树不可用时自动重启该客户端
            "per_app_click": {},        # 按应用覆盖，见 README
            "captcha": dict(CAPTCHA_DEFAULTS),   # 滑块验证码自动处理，默认关闭
        }

    def _load(self) -> dict:
        defaults = self._defaults()
        data = dict(defaults)   # 浅拷贝：下面的赋值不能污染 defaults 快照
        try:
            stored = json.loads(self.path.read_text("utf-8"))
            if isinstance(stored, dict):
                for k, v in stored.items():
                    if k in data:
                        data[k] = v
        except (OSError, ValueError):
            pass
        # config.json 是用户可手改的文件，类型写坏（如 "fired_log": null）不该
        # 让程序起不来或让定时器静默停摆：poll_due 里的 `key in fired_log` 会
        # 抛 TypeError，而它跑在 tick 回调里 —— 异常一旦抛出，after 链条断裂，
        # 到点触发与关闭队列从此再也不执行，界面却看不出任何异样。这里把结构性
        # 字段的坏值退回默认，其余字段照旧原样保留。
        if not isinstance(data.get("apps"), list):
            data["apps"] = []
        if not isinstance(data.get("daily_times"), list):
            data["daily_times"] = list(defaults["daily_times"])
        if not isinstance(data.get("fired_log"), dict):
            data["fired_log"] = {}
        if not isinstance(data.get("per_app_click"), dict):
            data["per_app_click"] = {}
        # 数值字段同样兜住：int() 在 poll_due / next_run_text / make_click_job 里
        # 每轮都跑，一个 "abc" 就足以让整个调度循环停摆。
        for key, low in (("interval_minutes", 1), ("keep_minutes", 0),
                         ("grace_minutes", 0), ("click_wait_window", 5),
                         ("click_retry_seconds", 5), ("click_max_attempts", 3)):
            try:
                data[key] = max(int(data[key]), low)
            except (TypeError, ValueError):
                data[key] = max(int(defaults[key]), low)
        # 布尔字段手改成字符串（"false"）会让 `if data[...]` 恒为真，语义反了；
        # 统一按 Python 真值语义归一化。
        data["restart_if_no_tree"] = bool(data.get("restart_if_no_tree"))
        # captcha 是嵌套块，要跟默认逐字段合并（老配置没有它、手改的缺字段都
        # 要能补齐），数值再逐个夹到合理区间：这些值每轮尝试都参与计算，
        # 一个 "abc" 或负数不该让自动处理崩在 tick 回调里。
        cap = dict(CAPTCHA_DEFAULTS)
        if isinstance(data.get("captcha"), dict):
            cap.update(data["captcha"])
        cap["enabled"] = bool(cap.get("enabled"))
        cap["debug_save"] = bool(cap.get("debug_save"))
        cap["route"] = "cdp" if str(cap.get("route", "screen")).lower() == "cdp" \
            else "screen"
        for key, low, high in (("max_attempts", 1, 20), ("retry_seconds", 2, 60),
                               ("wait_seconds", 0.5, 20.0),
                               ("min_gap_score", 0.0, 1.0),
                               ("drag_scale", 0.5, 2.0),
                               ("drag_bias", -50.0, 50.0),
                               ("piece_x0", 0.0, 200.0),
                               ("piece_zone_pct", 0.0, 40.0),
                               ("settle_seconds", 0.2, 10.0)):
            try:
                val = float(cap[key])
            except (KeyError, TypeError, ValueError):
                val = float(CAPTCHA_DEFAULTS[key])
            val = min(max(val, low), high)
            cap[key] = (int(round(val))
                        if key in ("max_attempts", "retry_seconds") else val)
        data["captcha"] = cap
        # 应用列表按 key 合并：保留用户已改的路径，新版本新增的应用自动补上
        by_key = {a.get("key"): a for a in data["apps"] if isinstance(a, dict)}
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


# ------------------------------------------------------------ 关闭客户端 ------
WM_CLOSE = 0x0010


def _post_wm_close(image: str) -> int:
    """向该镜像名对应进程的每个可见顶层窗口发一条 WM_CLOSE。

    注意 `ctypes.wintypes` 必须显式 `import ctypes.wintypes`：只 `import
    ctypes` 时它并不存在（实测 AttributeError），而这类错误在"把本函数换成
    桩"的单测里永远暴露不出来，只有真去关客户端才会炸。
    """
    return _post_wm_close_to(set(uia_click.list_pids(image)))


def _post_wm_close_to(pids: set[int]) -> int:
    """向指定 PID 集合的每个可见顶层窗口发一条 WM_CLOSE，返回发出的条数。

    为什么不继续用 `taskkill /IM`：Electron 客户端的渲染 / GPU / 工具子进程
    都没有顶层窗口，taskkill 关不掉它们，于是"礼貌关闭"这一阶段实际总是失
    败，必然升级成下一阶段的 `/T /F` 硬杀整棵进程树。硬杀会让 Chromium 的
    用户数据目录锁与 GPU 缓存留在脏状态，紧接着重启客户端就会起不来或直接
    未响应（TraeWork CN 就是这么被跑崩的）。

    WM_CLOSE 是客户端自己的关闭流程：它会保存状态、释放单实例锁、自己带走
    子进程，之后 `is_running` 自然为假，也就不会再走到强制结束那一步。
    """
    if os.name != "nt" or not pids:
        return 0
    user32 = ctypes.windll.user32
    sent = 0

    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND,
                        ctypes.wintypes.LPARAM)
    def visit(hwnd, _lparam):
        nonlocal sent
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            sent += 1
        return True

    try:
        user32.EnumWindows(visit, 0)
    except OSError:
        return 0
    return sent


# ---------------------------------------------------------------- 引擎 ------
class ClaimEngine:
    """与界面无关的领取逻辑，GUI 与命令行共用。"""

    def __init__(self, cfg: AppConfig, log=print):
        self.cfg = cfg
        self.log = log
        # 自愈重启后的回调（由 GUI / 命令行注册）：重启出来的实例同样是本工具
        # 拉起的，要按"保持时长"重新登记自动关闭，否则它会一直开着。
        self.on_relaunch = None

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
        return _post_wm_close(image) > 0

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
            try:
                elapsed = (now - datetime.fromisoformat(last)).total_seconds() \
                    if last else None
            except (TypeError, ValueError):
                elapsed = None   # 时间戳被手改坏 → 当作从未触发，别让 tick 挂掉
            if elapsed is None or elapsed >= gap:
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
            # 用正则分组取时分，别用 t[:2]/t[-2:] 切片：TIME_RE 允许 "8:05"
            # 这种单位数小时，切片会得到 int("8:") 直接抛 ValueError。本函数由
            # status_tick 每秒调用一次，一旦抛异常，after 链条断裂，状态栏和
            # "下次执行"从此不再刷新。
            for t in times:
                m = TIME_RE.match(t)
                dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                 second=0, microsecond=0)
                if dt > now:
                    return self._human(dt, now)
            m = TIME_RE.match(times[0])
            tomorrow = now + timedelta(days=1)
            dt = tomorrow.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                  second=0, microsecond=0)
            return self._human(dt, now)
        last = d["fired_log"].get("interval-last")
        if not last:
            return "就绪（首次触发待运行）"
        try:
            dt = datetime.fromisoformat(last) + \
                timedelta(minutes=max(int(d["interval_minutes"]), 1))
        except (TypeError, ValueError):
            # 配置被手改坏时当作"尚未触发"，别让状态栏整块卡死
            return "就绪（首次触发待运行）"
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

        用户只要动了「点哪里」相关的键（steps/keywords/point），就把它当作
        整体换写法，先把预置的这几个键摘掉 —— 否则预置的 steps 会和用户写的
        point 同时存在，谁生效要看运气。launch_args 等无关键不受影响。
        """
        key = app.get("key", "")
        merged = dict(DEFAULT_PER_APP_CLICK.get(key, {}))
        user = self.cfg.data.get("per_app_click", {}).get(key, {})
        if any(k in user for k in CLICK_SHAPE_KEYS):
            for k in CLICK_SHAPE_KEYS:
                merged.pop(k, None)
        merged.update(user)
        return merged

    # ---- 滑块验证码自动处理 ----
    def captcha_cfg(self) -> dict:
        """config.json 的 captcha 块（归一化后的；老配置里没有时返回空字典）。"""
        return self.cfg.data.get("captcha") or {}

    def captcha_active(self) -> bool:
        """总开关是否生效。route 目前只实现了 screen，cdp 视为未实现 → 不启用。"""
        cap = self.captcha_cfg()
        return bool(cap.get("enabled")) and cap.get("route") == "screen"

    def captcha_for(self, app: dict) -> bool:
        """该应用是否需要自动过滑块：总开关打开，且该应用在 per_app_click
        里标了 captcha（如 ZCode）。两个开关是"与"的关系。"""
        return self.captcha_active() and bool(
            self.per_app_click_cfg(app).get("captcha"))

    def launch_extra_args(self, app: dict) -> list[str]:
        """启动参数：per_app 覆盖，或智能点击开启时带无障碍参数。"""
        ov = self.per_app_click_cfg(app)
        if "launch_args" in ov:
            return list(ov["launch_args"])
        if self.smart_click_active():
            return [uia_click.A11Y_FLAG]
        return []

    def click_steps_for(self, app: dict) -> list[dict]:
        """把该应用的点击配置归一化成步骤列表。

        单步应用和两步入口在这里统一成同一种结构，调度层（process_click_job）
        就不必再区分两种写法。每步字段：keywords / point / region_pct /
        desc / wait。

        region_pct 必须原样带过去：它把"只在窗口某个区域内找元素"这件事从
        配置一直传到 find_and_click。上一版把它漏在这里，导致区域过滤是死代
        码 —— 关键词仍在整个窗口里匹配，而聊天正文里恰好也有"用户""领取"
        这些字，会点到对话上。
        """
        ov = self.per_app_click_cfg(app)
        raw = ov.get("steps")
        if not raw:   # 单步写法：{"keywords": [...]} 或 {"point": [...]}
            return [{
                "keywords": list(ov.get("keywords")
                                 or self.cfg.data["click_keywords"]),
                "point": tuple(ov["point"]) if ov.get("point") else None,
                "region_pct": _as_region(ov.get("region_pct")),
                "desc": "领取",
                "wait": 0.0,
            }]
        steps = []
        for i, s in enumerate(raw):
            steps.append({
                "keywords": list(s.get("keywords") or []),
                "point": tuple(s["point"]) if s.get("point") else None,
                "region_pct": _as_region(s.get("region_pct")),
                "desc": s.get("desc") or f"第{i + 1}步",
                "wait": float(s.get("wait", DEFAULT_STEP_WAIT)),
            })
        return steps

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
        steps = self.click_steps_for(app)
        wait = max(int(d["click_wait_window"]), 5)
        retry = max(int(d["click_retry_seconds"]), 5)
        max_attempts = max(int(d["click_max_attempts"]), 3)
        keep = int(d["keep_minutes"])
        if keep > 0:   # 关闭前留出点击窗口
            deadline = start_ts + keep * 60 - 45
        else:          # 每步各自有重试预算，总时长按步数放大
            deadline = (start_ts + wait
                        + max_attempts * retry * len(steps))
        return {
            "label": app["label"], "image": self.image_name(app),
            "app": app, "wait": wait,
            "steps": steps, "step": 0,
            "next": start_ts + (START_DELAY if will_launch else 3),
            "retry": retry, "fast_retry": FAST_RETRY,
            "backoff": RETRY_BACKOFF, "attempts": 0, "max": max_attempts,
            "deadline": max(deadline, start_ts + wait + 15),
            "done": False, "hinted": False, "clicked": set(),
            # 界面树不可用时的自愈状态（见 _process_recover）：
            #   restarts/max_restarts 控制重启预算；no_tree_since 记录"树从何时起
            #   一直不可用"，用来区分"刚启动还没长出来"和"永远不会好了"；
            #   recover_stage 非 None 表示任务正处于恢复流程中。
            "restarts": 0,
            "max_restarts": (RECOVER_MAX
                             if self.cfg.data.get("restart_if_no_tree", True)
                             else 0),
            "no_tree_since": 0.0,
            "no_tree_wait": (NO_TREE_WAIT_LAUNCHED if will_launch
                             else NO_TREE_WAIT_RUNNING),
            "recover_stage": None, "recover_at": 0.0,
            # 半自动应用的补充说明（如 ZCode 需人工过滑块），点中最后一步后提示
            "hint": self.per_app_click_cfg(app).get("hint") or "",
            # 验证码阶段：captcha 为真表示本应用点开入口后要自动过滑块；
            # 关闭时这三个字段虽然带着，但流程永远不会走到验证码分支。
            "captcha": self.captcha_for(app),
            "captcha_pending": False,
            "captcha_tries": 0,
            "captcha_wait": float(self.captcha_cfg().get("wait_seconds", 2.5)),
        }

    def _retry_delay(self, job: dict) -> float:
        """失败后的重试间隔：从 fast_retry 起按倍数退避，封顶为配置的重试间隔。

        启动后"窗口还没出现""树还在长"这类失败，等 15 秒纯属浪费 —— 界面树实测
        约 5 秒就长出来。退避让就绪的那一刻能被立刻抓住，同时真正点不到按钮时
        节奏会迅速放慢到配置的 retry，不至于高频空转。
        """
        n = max(int(job["attempts"]), 1)
        return min(job["fast_retry"] * (job["backoff"] ** (n - 1)),
                   float(job["retry"]))

    def _skip_ahead_if_menu_open(self, job: dict) -> bool:
        """多步任务里，若最后一步的目标已经可见，说明前置菜单本来就开着。

        这时再点一次前置按钮反而会把菜单关掉（toggle），所以直接跳到最后一
        步。坐标步骤无法探测可见性，返回 False 按原顺序走。
        """
        last = job["steps"][-1]
        if not last["keywords"] or last["point"]:
            return False
        status, detail = uia_click.find_and_click(
            job["image"], last["keywords"], probe=True,
            region_pct=last.get("region_pct"))
        if status != "found":
            return False
        job["step"] = len(job["steps"]) - 1
        job["attempts"] = 0
        self.log(f"· {job['label']}：{detail}，跳过前置步骤直接领取")
        return True

    # ---- 滑块验证码阶段 ----
    def _captcha_fallback(self, job: dict, max_tries: int):
        """自动过滑块用尽预算：结束任务并提示人工处理，不阻塞领取主流程。"""
        job["done"] = True
        hint = job.get("hint") or "请在弹窗中手动完成安全验证"
        self.log(f"⚠ {job['label']}：{max_tries} 次未能自动通过验证码，转人工。"
                 f"{hint}", "warn")

    def _process_captcha(self, job: dict, now_ts: float):
        """点开入口后自动过滑块：成功收工，失败按预算重试，超限转人工。

        设计约束（见 docs/验证码勘察报告.md §六.5/§六.6）：
          · 每次尝试之间隔 retry_seconds（限频，不对抗风控）；
          · 累计 max_attempts 次仍不过就停止自动尝试，退回人工提示，绝不在
            同一个入口上无限重试；
          · 任何失败都不阻塞领取主流程（任务照常结束，等下个调度周期）。
        """
        cap = self.captcha_cfg()
        if not captcha_solver.available():
            job["done"] = True
            hint = job.get("hint") or "请在弹窗中手动完成安全验证"
            self.log(f"⚠ {job['label']}：缺少 numpy/opencv，无法自动处理验证码，"
                     f"转人工。{hint}", "warn")
            return
        max_tries = max(int(cap.get("max_attempts", 5)), 1)
        tries = int(job.get("captcha_tries", 0))
        if tries >= max_tries:
            self._captcha_fallback(job, max_tries)
            return
        job["captcha_tries"] = tries + 1
        ok, detail = captcha_solver.solve(job["image"], cap, log=self.log)
        if ok:
            job["done"] = True
            self.log(f"✔ {job['label']}：安全验证已通过（{detail}）", "ok")
            return
        self.log(f"· {job['label']}：第 {tries + 1}/{max_tries} 次自动验证未通过"
                 f"（{detail}）", "warn")
        if tries + 1 >= max_tries:
            self._captcha_fallback(job, max_tries)
        else:
            job["next"] = now_ts + max(int(cap.get("retry_seconds", 3)), 2)

    def process_click_job(self, job: dict, now_ts: float):
        if job["done"] or now_ts < job["next"]:
            return
        if job.get("captcha_pending"):
            self._process_captcha(job, now_ts)
            return
        if job.get("recover_stage") is not None:
            self._process_recover(job, now_ts)
            return
        steps, idx = job["steps"], job["step"]
        step, is_last = steps[idx], idx == len(steps) - 1
        where = f"{step['desc']}：" if len(steps) > 1 else ""
        if not is_last:              # 非最后一步才探测（单步应用不会进这里）
            if self._skip_ahead_if_menu_open(job):
                job["next"] = now_ts + 1
                return
        job["attempts"] += 1
        status, detail = uia_click.find_and_click(
            job["image"], step["keywords"],
            clicked_names=job["clicked"], point_pct=step["point"],
            region_pct=step.get("region_pct"))
        if status != "no-tree":
            # 树又好了（可能是刚启动时还没长出来）：清掉"持续不可用"的计时，
            # 下次再塌陷时重新从头计时，不会被上一轮的旧时间戳误触发重启。
            job["no_tree_since"] = 0.0
        give_up = job["attempts"] >= job["max"] or now_ts > job["deadline"]
        if status == "clicked":
            if is_last:
                if job.get("captcha"):
                    # 入口点开后会弹出滑块浮层，转入验证码阶段（_process_captcha）
                    job["captcha_pending"] = True
                    job["captcha_tries"] = 0
                    job["next"] = now_ts + job["captcha_wait"]
                    self.log(f"✔ {job['label']}：已自动点击 {detail}，"
                             "等待安全验证…", "ok")
                else:
                    job["done"] = True
                    self.log(f"✔ {job['label']}：已自动点击 {detail}", "ok")
                    if job.get("hint"):
                        self.log(f"→ {job['label']}：{job['hint']}", "warn")
            else:
                job["step"] = idx + 1
                job["attempts"] = 0
                job["next"] = now_ts + step["wait"]
                self.log(f"✔ {job['label']}：{where}已点击 {detail}，继续下一步",
                         "ok")
        elif status == "already":
            if is_last:
                job["done"] = True
                self.log(f"✔ {job['label']}：{detail}，无需再点", "ok")
            else:
                # 前置步骤就撞上完成标识，说明菜单开着且今天已领，跳到最后一
                # 步去确认即可，不要再点一次前置按钮把菜单关掉。
                job["step"] = len(steps) - 1
                job["attempts"] = 0
                job["next"] = now_ts + 1
                self.log(f"· {job['label']}：{where}{detail}，跳到领取步骤确认")
        elif status == "no-window":
            if give_up:
                job["done"] = True
                self.log(f"⚠ {job['label']}：窗口始终未出现，放弃自动点击", "warn")
            else:
                job["next"] = now_ts + self._retry_delay(job)
        elif status == "no-tree":
            # 界面树不可用。可能是刚启动还没长出来（等一会儿就好），也可能是
            # 树已塌陷 / 这个实例没带无障碍参数（等多久都不会好）。前者靠重试
            # 等它，后者只能冷启动。用"持续不可用多久"来区分，见 _maybe_recover。
            if self._maybe_recover(job, now_ts, detail):
                return
            # 还在 no_tree_wait 窗口内：这段等待由自愈逻辑兜底，不该消耗重试
            # 预算，否则会在自愈到点之前就因预算耗尽先放弃。
            warming = now_ts - job["no_tree_since"] < job["no_tree_wait"]
            if warming:
                job["attempts"] = max(job["attempts"] - 1, 0)
            if give_up and not warming:
                job["done"] = True
                self.log(f"⚠ {job['label']}：界面树始终不可用（{detail}），"
                         "放弃自动点击", "warn")
            else:
                if not job["hinted"]:
                    job["hinted"] = True
                    self.log(f"⚠ {job['label']}：{detail}", "warn")
                job["next"] = now_ts + self._retry_delay(job)
        elif status == "minimized":
            # 窗口被最小化时拿不到可用矩形，百分比区域过滤无从谈起。这是可恢复
            # 状态（还原窗口即可），不能像未知错误那样立即放弃 —— 之前没有这个
            # 分支，会落到 else 记成"自动点击出错"，用户照提示去翻关键词也找不
            # 到原因。这里按重试处理，并把"还原窗口"写进日志。
            if give_up:
                job["done"] = True
                self.log(f"⚠ {job['label']}：{where}目标窗口已最小化，"
                         f"{job['attempts']} 次尝试无法按区域查找，放弃自动点击"
                         "（还原窗口后重试即可）", "warn")
            else:
                job["next"] = now_ts + self._retry_delay(job)
        elif status in ("no-element", "blocked"):
            element_give_up = (job["attempts"] >= min(job["max"],
                                                      NO_ELEMENT_MAX_ATTEMPTS)
                               or now_ts > job["deadline"])
            if element_give_up:
                job["done"] = True
                hint = f"可用 --inspect {job['image']} 查看界面元素名后调整关键词" \
                    if status == "no-element" else detail
                self.log(f"⚠ {job['label']}：{where}{job['attempts']} 次尝试未点到"
                         f"按钮，放弃自动点击（{hint}）", "warn")
            else:
                job["next"] = now_ts + self._retry_delay(job)
        else:  # error
            job["done"] = True
            self.log(f"⚠ {job['label']}：{where}自动点击出错 {detail}", "warn")

    # ---- 界面树不可用时的自愈 ----
    def _maybe_recover(self, job: dict, now_ts: float, detail: str) -> bool:
        """界面树持续不可用就转入自愈重启；返回 True 表示已进入恢复流程。

        触发条件是"树连续不可用超过 no_tree_wait 秒"而不是"第一次报错就重启"：
        工具刚把客户端冷启动时，Chromium 要几十秒才把无障碍树长出来（实测同一
        台机器上 12s～100s 不等），这段时间里的 no-tree 是正常现象，急着重启
        反而会把一个马上就能用的实例打断。
        """
        if not job["max_restarts"] or job["restarts"] >= job["max_restarts"]:
            return False
        if not job["no_tree_since"]:
            job["no_tree_since"] = now_ts
            return False
        waited = now_ts - job["no_tree_since"]
        if waited < job["no_tree_wait"]:
            return False
        job["restarts"] += 1
        job["recover_stage"] = 0
        job["recover_at"] = now_ts
        # 恢复本身要花一两分钟，不能算进"放弃预算"，否则刚重启完就因超时放弃。
        job["deadline"] += RECOVER_BUDGET
        self.log(f"· {job['label']}：界面树已持续 {int(waited)} 秒不可用（{detail}），"
                 f"第 {job['restarts']}/{job['max_restarts']} 次尝试重启客户端"
                 "以恢复自动点击…", "warn")
        return True

    def _process_recover(self, job: dict, now_ts: float):
        """自愈状态机：优雅关闭 → 等退出（超时升级强杀）→ 歇一下 → 带参数重启。

        单实例锁是这里最容易被忽略的一环：Electron 客户端的第二个进程会把
        "打开窗口"的请求交回已在运行的旧进程然后自己退出，新进程根本不会带
        无障碍参数 —— 所以必须确认旧进程真的没了再拉起，否则重启一次白费一次。
        """
        image, stage = job["image"], job["recover_stage"]
        if stage == 0:
            if not self.is_running(image):
                job["recover_stage"] = 3
                job["next"] = now_ts + RECOVER_SETTLE
                return
            self.close_graceful(image)
            self.log(f"… {job['label']}：正在优雅关闭，随后带无障碍参数重启…")
            job["recover_stage"] = 1
            job["recover_at"] = now_ts
            job["next"] = now_ts + RECOVER_POLL
            return
        if stage in (1, 2):
            if not self.is_running(image):
                job["recover_stage"] = 3
                job["next"] = now_ts + RECOVER_SETTLE
                return
            budget = RECOVER_CLOSE_TIMEOUT if stage == 1 else RECOVER_KILL_TIMEOUT
            if now_ts - job["recover_at"] < budget:
                job["next"] = now_ts + RECOVER_POLL
                return
            if stage == 2:
                self._end_recover(job, "强制结束后进程仍未退出")
                return
            # 优雅关闭没走通（客户端自己卡住或没响应 WM_CLOSE）：升级为强杀。
            # 这条路径和定时自动关闭的两段式关闭保持一致 —— 不这样做的话，
            # 残留进程会一直占着单实例锁，重启永远拿不到无障碍参数。
            self.log(f"⚠ {job['label']}：{int(RECOVER_CLOSE_TIMEOUT)} 秒未退出，"
                     "强制结束以便重启", "warn")
            self.close_force(image)
            job["recover_stage"] = 2
            job["recover_at"] = now_ts
            job["next"] = now_ts + RECOVER_POLL
            return
        # stage 3：进程已退出，带无障碍参数重新拉起
        app = job["app"]
        extra = self.launch_extra_args(app) or [uia_click.A11Y_FLAG]
        try:
            self.launch(app["exe"], tuple(extra))
        except Exception as e:  # noqa: BLE001 - 启动失败不该把任务卡在恢复态
            self._end_recover(job, f"重启失败 {e!r}")
            return
        self.log(f"✔ {job['label']}：已带无障碍参数重启（{' '.join(extra)}），"
                 "重新开始领取流程", "ok")
        if self.on_relaunch:
            try:
                self.on_relaunch(image)
            except Exception:  # noqa: BLE001 - 登记自动关闭失败不影响领取
                pass
        # 新实例的界面是全新的：之前点开的菜单没了、点过的元素也可以再点，
        # 所以把步骤复位重跑，而不是接着原来的步号往下走。
        job["step"] = 0
        job["attempts"] = 0
        job["clicked"] = set()
        job["no_tree_since"] = 0.0
        job["no_tree_wait"] = NO_TREE_WAIT_LAUNCHED
        job["hinted"] = False
        job["captcha_pending"] = False
        job["recover_stage"] = None
        job["next"] = now_ts + START_DELAY

    def _end_recover(self, job: dict, why: str):
        """恢复失败：结束任务并说明原因，不再继续重试（避免无限重启）。"""
        job["recover_stage"] = None
        job["done"] = True
        self.log(f"⚠ {job['label']}：自动恢复失败（{why}），放弃自动点击", "warn")


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
        self.engine.on_relaunch = self._on_app_relaunched
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
    def _is_descendant(widget, ancestor) -> bool:
        while widget is not None:
            if widget is ancestor:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _on_wheel(self, event):
        """滚轮只滚设置区。

        鼠标停在日志面板上时让日志自己滚，停在数字/下拉框上时交给控件（滚轮
        会改它们的值），其余位置才滚动设置画布 —— 否则日志和设置区会一起滚。
        """
        w = event.widget
        if isinstance(w, (ttk.Combobox, ttk.Spinbox)):
            return
        if w is self.log_text or self._is_descendant(w, self.log_text):
            return
        if w is self.canvas or self._is_descendant(w, self._settings):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def toggle_advanced(self):
        """展开/收起「高级设置」——启动后行为 + 智能点击。"""
        if self.adv_frame.winfo_ismapped():
            self.adv_frame.pack_forget()
        else:
            self.adv_frame.pack(fill="x", after=self.adv_head)
        self._update_adv_label()

    def _update_adv_label(self):
        opened = self.adv_frame.winfo_ismapped()
        self.adv_btn.configure(
            text=("▾ " if opened else "▸ ")
            + "高级设置（启动后行为 · 智能点击）")

    def _on_settings_configure(self, _event=None):
        """设置区内容尺寸变化：刷新滚动范围，并重新贴合高度。"""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._fit_settings()

    def _fit_settings(self, event=None):
        """让设置画布的高度贴合内容，但不超过可用高度。

        内容比窗口矮时画布就收窄到内容高度，富余的竖直空间全部让给日志
        面板；内容更高时画布占满可用空间并出现滚动条。这样既不会在「高级
        设置」和按钮之间留出空白，也不会在小窗口下把内容裁掉。
        """
        if not hasattr(self, "_body"):
            return
        self._settings.update_idletasks()
        need = self._settings.winfo_reqheight()
        avail = event.height if event is not None else self._body.winfo_height()
        reserve = (self.btn_row.winfo_reqheight()
                   + self.log_card.winfo_reqheight() + 40)
        space = avail - reserve
        target = max(120, min(need, space))
        if self.canvas.winfo_reqheight() != target:
            self.canvas.configure(height=target)
        # 滚动条按需出现：判定用的是当前布局下的 need，不会来回抖
        if need > space + 1:
            if not self._vbar.winfo_ismapped():
                self._vbar.pack(side="right", fill="y", before=self.canvas)
        elif self._vbar.winfo_ismapped():
            self._vbar.pack_forget()

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
        self.root.geometry(f"{int(900 * s)}x{int(920 * s)}")
        self.root.minsize(int(800 * s), int(640 * s))
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
        self._body = body

        # 日志（深色终端风）—— 固定在底部，不随上面的设置区滚动。
        # expand=True：设置区按内容高度收窄后，多出来的竖直空间归日志，
        # 免得「高级设置」和按钮之间空出一大块。
        log_card = tk.Frame(body, bg=self.C_CARD,
                            highlightbackground=self.C_BORDER,
                            highlightthickness=1)
        self.log_card = log_card
        log_card.pack(side="bottom", fill="both", expand=True, pady=(0, 12))
        log_head = tk.Frame(log_card, bg=self.C_CARD)
        log_head.pack(fill="x", padx=14, pady=(10, 4))
        tk.Frame(log_head, bg=self.C_ACCENT, width=4, height=15).pack(
            side="left")
        tk.Label(log_head, text="运行日志", bg=self.C_CARD, fg=self.C_TEXT,
                 font=self.f_section).pack(side="left", padx=(8, 0))
        log_body = tk.Frame(log_card, bg=self.C_LOG_BG)
        log_body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text = tk.Text(log_body, height=8, state="disabled",
                                bg=self.C_LOG_BG, fg=self.C_LOG_FG,
                                insertbackground="#FFFFFF",
                                selectbackground="#33415E",
                                font=self.f_mono, wrap="word", relief="flat",
                                padx=10, pady=8)
        log_scroll = ttk.Scrollbar(log_body, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for tag, color in (("info", self.C_LOG_FG), ("ok", "#5EE39A"),
                           ("warn", "#FFC24B"), ("head", "#7FB5FF")):
            self.log_text.tag_configure(tag, foreground=color)

        # 操作按钮 —— 同样固定在底部
        btn_row = tk.Frame(body, bg=self.C_BG)
        self.btn_row = btn_row
        btn_row.pack(side="bottom", fill="x", pady=(14, 12))
        self._btn(btn_row, "▶  立即领取", self.manual_claim,
                  kind="accent").pack(side="left")
        self._btn(btn_row, "保存设置", self.save_settings).pack(
            side="left", padx=10)
        self.autostart_btn = self._btn(btn_row, "", self.toggle_autostart)
        self.autostart_btn.pack(side="left")
        self.autostart_btn_text()
        self._btn(btn_row, "退出程序", self.quit_app, kind="danger").pack(
            side="right")

        # 设置区：项目较多，套一层画布。画布高度跟着内容走（见
        # _fit_settings），只有内容确实超出可用高度时才出现滚动条。
        holder = tk.Frame(body, bg=self.C_BG)
        self._holder = holder
        holder.pack(side="top", fill="x")
        self.canvas = tk.Canvas(holder, bg=self.C_BG, highlightthickness=0,
                                bd=0)
        vbar = ttk.Scrollbar(holder, orient="vertical",
                             command=self.canvas.yview)
        self._vbar = vbar
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        settings = tk.Frame(self.canvas, bg=self.C_BG)
        self._settings = settings
        self._settings_win = self.canvas.create_window(
            (0, 0), window=settings, anchor="nw")
        settings.bind("<Configure>", self._on_settings_configure)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._settings_win, width=e.width))
        body.bind("<Configure>", self._fit_settings)
        self.root.bind_all("<MouseWheel>", self._on_wheel)

        pad = {"pady": (12, 0)}

        # ① 应用
        apps_body = self._card(settings, "应用", **pad)
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
                     "右侧「领取日」用来错开频率，例如只在周末领。")).pack(
            anchor="w", pady=(8, 0))

        # ② 定时计划
        sched_body = self._card(settings, "定时计划", **pad)
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
        # 文案随模式切换，见 on_mode_change()
        self.daily_hint = tk.Label(
            row2, fg=self.C_SUB, bg=self.C_CARD, justify="left",
            font=self.f_small, text="到点依次启动勾选的应用，登录即完成领取。")
        self.daily_hint.pack(side="left", padx=18, pady=2)

        # ③④ 高级设置：默认收起，界面不至于一上来就堆满
        self.adv_head = tk.Frame(settings, bg=self.C_BG)
        self.adv_head.pack(fill="x", pady=(14, 0))
        self.adv_btn = tk.Label(self.adv_head, text="", bg=self.C_BG,
                                fg=self.C_ACCENT, font=self.f_section,
                                cursor="hand2", anchor="w")
        self.adv_btn.pack(side="left")
        self.adv_btn.bind("<Button-1>", lambda e: self.toggle_advanced())
        self.adv_frame = tk.Frame(settings, bg=self.C_BG)

        # 启动后行为
        beh = self._card(self.adv_frame, "启动后行为", **pad)
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
        row4.pack(fill="x", pady=(8, 0))
        self.skip_var = tk.BooleanVar(value=self.cfg.data["skip_if_running"])
        self._check(row4, "已在运行的客户端跳过启动（且不关闭它）",
                    self.skip_var).pack(side="left")
        row4b = tk.Frame(beh, bg=self.C_CARD)
        row4b.pack(fill="x", pady=(8, 0))
        self.catchup_var = tk.BooleanVar(value=self.cfg.data["catchup_missed"])
        self._check(row4b, "错过时间点后补领，宽限",
                    self.catchup_var).pack(side="left")
        self.grace_var = tk.IntVar(value=self.cfg.data["grace_minutes"])
        ttk.Spinbox(row4b, from_=10, to=720, increment=10, width=6,
                    font=self.f_body, textvariable=self.grace_var).pack(
            side="left", padx=6)
        tk.Label(row4b, text="分钟内有效", bg=self.C_CARD, fg=self.C_SUB,
                 font=self.f_body).pack(side="left")

        # ④ 智能点击
        click_body = self._card(self.adv_frame, "智能点击", **pad)
        row5 = tk.Frame(click_body, bg=self.C_CARD)
        row5.pack(fill="x", pady=2)
        self.click_var = tk.BooleanVar(value=self.cfg.data["click_enabled"])
        self._check(row5, "启用", self.click_var,
                    command=self.on_click_toggle).pack(side="left")
        self.restart_var = tk.BooleanVar(
            value=bool(self.cfg.data["restart_if_no_tree"]))
        self.restart_check = self._check(
            row5, "界面树不可用时自动重启客户端", self.restart_var,
            command=self.on_click_toggle)
        self.restart_check.pack(side="left", padx=(14, 0))
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
                     "自动查找并点击含关键词的按钮；文案变了用 --inspect 查元素名。")).pack(
            anchor="w", pady=(8, 0))

        # ⑤ 验证码（滑块）自动处理
        cap_body = self._card(self.adv_frame, "验证码（滑块）", **pad)
        row7 = tk.Frame(cap_body, bg=self.C_CARD)
        row7.pack(fill="x", pady=2)
        self.captcha_var = tk.BooleanVar(
            value=bool(self.cfg.data["captcha"].get("enabled")))
        self._check(row7, "自动处理滑块验证码", self.captcha_var,
                    command=self.on_captcha_toggle).pack(side="left")
        tk.Label(row7, text="最多尝试", bg=self.C_CARD, fg=self.C_SUB,
                 font=self.f_body).pack(side="left", padx=(14, 4))
        self.captcha_max_var = tk.IntVar(
            value=int(self.cfg.data["captcha"].get("max_attempts", 5)))
        self.captcha_spin = ttk.Spinbox(
            row7, from_=1, to=20, width=5, font=self.f_body,
            textvariable=self.captcha_max_var)
        self.captcha_spin.pack(side="left", ipady=2)
        tk.Label(row7, text="次后转人工", bg=self.C_CARD, fg=self.C_SUB,
                 font=self.f_body).pack(side="left", padx=(4, 0))
        tk.Label(cap_body, fg=self.C_SUB, bg=self.C_CARD, justify="left",
                 font=self.f_small, text=(
                     "点开带验证码的领取入口后自动拖滑块，仅对已标注 captcha 的"
                     "应用生效（当前 ZCode）；失败按上限次数重试后提示人工完成。")).pack(
            anchor="w", pady=(8, 0))

        # 设置区底部留白，滚动到底时不贴边
        tk.Frame(settings, bg=self.C_BG, height=12).pack(fill="x")

        self._update_adv_label()
        self.on_mode_change()
        self._set_click_state()
        self._set_captcha_state()

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

    def on_captcha_toggle(self):
        self._set_captcha_state()
        self.save_settings(quiet=True)

    def on_mode_change(self):
        is_daily = self.mode_var.get() == "daily"
        state = "disabled" if is_daily else "normal"
        self.interval_spin.configure(state=state)
        for w in (self.time_list, self.time_entry):
            w.configure(state="normal" if is_daily else "disabled")
        self.daily_hint.configure(
            text="到点依次启动勾选的应用，登录即完成领取。" if is_daily else
                 "每隔设定分钟数执行一次领取。")

    def _set_click_state(self):
        state = "normal" if self.click_var.get() else "disabled"
        self.click_keywords_entry.configure(state=state)
        for spin in self.click_spins:
            spin.configure(state=state)
        self.restart_check.configure(state=state)

    def _set_captcha_state(self):
        state = "normal" if self.captcha_var.get() else "disabled"
        self.captcha_spin.configure(state=state)

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
        d["restart_if_no_tree"] = bool(self.restart_var.get())
        cap = d.setdefault("captcha", {})
        cap["enabled"] = bool(self.captcha_var.get())
        cap["max_attempts"] = self._int_of(self.captcha_max_var, 5, 1)

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

    def _on_app_relaunched(self, image: str):
        """自愈重启后的实例同样是本工具拉起的，按当前「保持时长」重新登记关闭。

        不登记的话，用户"本来开着"的客户端被重启后会一直留在那里，和界面上
        "N 分钟后自动关闭"的承诺对不上；同时把旧条目删掉，免得新旧两条各关一次。
        """
        keep = self._int_of(self.keep_var, 10, 0)
        self.close_queue = [c for c in self.close_queue if c[1] != image]
        if keep <= 0:
            return
        self.close_queue.append([time.time() + keep * 60, image, 0])
        self.log(f"… {image} 将在 {keep} 分钟后自动关闭")

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
        # 300ms 而不是 1s：点击步骤之间本来就只等 1.2 秒，粗粒度轮询会让每步
        # 白白多等最多 1 秒。轮询只是本地读时间戳，加密到 3 次/秒开销可忽略。
        self.root.after(300, self.tick)

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
        if self.engine.captcha_active():
            parts.append("验证码自动处理：开")
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


def list_windows() -> list[tuple[int, int, str]]:
    """所有可见顶层窗口的 (hwnd, pid, 标题)。"""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    out: list[tuple[int, int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND,
                        ctypes.wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            out.append((hwnd, pid.value, buf.value))
        return True

    try:
        user32.EnumWindows(visit, 0)
    except OSError:
        return out
    return out


def focus_existing_instance() -> bool:
    """把另一个实例的主窗口提到前台（最小化则还原）。

    重复启动时不该只丢一句"已在运行中"就退出 —— 用户点了图标却什么都
    没发生，看起来就是"打开就闪退"。提不到前台就闪任务栏图标，至少让
    用户知道窗口在哪。
    """
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    me = os.getpid()
    for hwnd, pid, title in list_windows():
        if pid == me or not title.startswith(APP_TITLE):
            continue
        user32.ShowWindow(hwnd, SW_RESTORE)
        if not user32.SetForegroundWindow(hwnd):
            user32.FlashWindow(hwnd, True)
        return True
    return False


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

    def _register_relaunch(image: str):
        # 就地改列表（closes 在循环里会被重新赋值，闭包不能用 rebind 的方式改）
        closes[:] = [c for c in closes if c["image"] != image]
        if keep > 0:
            closes.append({"ts": time.time() + keep * 60,
                           "image": image, "stage": 0})
            eng.log(f"… {image} 将在 {keep} 分钟后自动关闭")

    eng.on_relaunch = _register_relaunch
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
        time.sleep(0.3)
    eng.log("本次领取流程结束。")


def run_calibrate(key: str, open_menu: bool = False) -> int:
    """校准某应用的领取入口：只读地导出界面元素清单。

    ⚠ 它**不关闭、不重启**应用。上一版在这里先 taskkill、4 秒后升级成
    `/T /F` 硬杀整棵进程树、紧接着又拉起来，把正在使用的 TraeWork CN 弄成
    未响应 —— 这是本工具最严重的一次事故，那段逻辑已彻底删除。现在的做法：

      · 应用没在运行 → 带无障碍参数启动它（这是"启动"，不碰任何别的东西）；
      · 应用正在运行 → 原样不动地读它当前的界面树；
      · 运行中的普通实例界面树是空的（实测只有 13 个标题栏按钮）→ 明确提示
        "请你自己退出后重跑"，把决定权交回用户，而不是替他把应用杀掉。

    open_menu=True 时额外点一下第一步（只点开菜单，不点领取），把菜单里的
    元素也照出来，用来确定第二步的关键词与区域。

    返回进程退出码：0 成功，1 界面树不可用或第一步没点到，2 参数/环境问题。
    """
    cfg = AppConfig()
    app = next((a for a in cfg.data["apps"] if a.get("key") == key), None)
    if app is None:
        keys = "、".join(a["key"] for a in cfg.data["apps"])
        print(f"配置里没有 key 为 {key!r} 的应用；可选：{keys}")
        return 2
    exe = (app.get("exe") or "").strip()
    if not exe or not Path(exe).exists():
        print(f"{app['label']} 的可执行文件不存在：{exe or '（未配置）'}")
        return 2
    if not uia_click.available():
        print("需要先安装依赖: pip install comtypes")
        return 2

    image = Path(exe).name
    eng = ClaimEngine(cfg, log=lambda msg, tag="info": print("·", msg))
    print(f"· 校准 {app['label']}（{image}）")
    if uia_click.has_window(image):
        print("  它正在运行：本脚本不会关闭也不会重启它，直接读当前界面。")
    else:
        print(f"  它没在运行：带 {uia_click.A11Y_FLAG} 启动它"
              "（只是启动，不影响其他程序）。")
        eng.launch(exe, (uia_click.A11Y_FLAG,))
        if not _wait_for_window(image, timeout=90):
            print("⚠ 90 秒内没等到窗口，校准中止")
            return 1
        time.sleep(6)   # 等首屏渲染完，否则元素还没建出来

    size = uia_click.tree_size(image)
    if size < 50:
        print(f"⚠ 只枚举到 {size} 个元素：这个实例没带无障碍参数启动，"
              "界面树是空的。")
        print("  请你自己退出它（托盘图标右键 → 退出），再重新运行本脚本。")
        print("  本脚本不会替你关掉它 —— 硬杀客户端会把它的缓存弄脏，"
              "下次启动就起不来了。")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = config_dir() / "logs"

    def save_dump(tag: str) -> Path:
        lines = uia_click.dump_tree(image, max_lines=4000)
        out = out_dir / f"ui_dump_{key}_{tag}_{stamp}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"· 共 {len(lines)} 行，已写入：{out}")
        return out

    save_dump("before")
    if not open_menu:
        print("· 想连菜单里的元素一起照出来，加 --open-menu 再跑一次"
              "（只点开菜单，不点领取）。")
        return 0

    step = eng.click_steps_for(app)[0]
    print(f"· 点开第一步「{step['desc']}」：关键词 {step['keywords']}，"
          f"区域 {step['region_pct'] or '整窗'} —— 只点这一下，不点领取。")
    status, detail = uia_click.find_and_click(
        image, step["keywords"], point_pct=step["point"],
        region_pct=step["region_pct"])
    print(f"  → {status}：{detail}")
    if status != "clicked":
        print("⚠ 第一步没点到，菜单没打开；先照上面的清单调关键词或区域。")
        return 1
    time.sleep(3)   # 等菜单渲染
    save_dump("menu")
    print("· 把这两份 ui_dump_*.txt 交给助手，即可定死两步的关键词与区域。")
    return 0


def _wait_for_window(image: str, timeout: float = 60.0) -> bool:
    """等目标进程的可见窗口出现（校准用，不阻塞主界面）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if uia_click.has_window(image):
            return True
        time.sleep(1.5)
    return False


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
    parser.add_argument("--inspect-max", type=int, default=300, metavar="N",
                        help="配合 --inspect，最多列出多少行（默认 300）")
    parser.add_argument("--inspect-contains", default="", metavar="文字",
                        help="配合 --inspect，只列出名字里含该文字的元素")
    parser.add_argument("--calibrate", metavar="应用key",
                        help="校准某应用的领取入口：把界面元素清单写入 "
                             "logs/ui_dump_*.txt（只读，不关闭也不重启应用）")
    parser.add_argument("--open-menu", action="store_true",
                        help="配合 --calibrate：额外点开第一步的菜单，"
                             "把菜单里的元素也照出来（只点开菜单，不点领取）")
    parser.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.smoke:
        run_smoke()
        return
    if args.inspect:
        if uia_click.available():
            for line in uia_click.dump_tree(args.inspect,
                                            args.inspect_contains,
                                            args.inspect_max):
                print(line)
        else:
            print("需要先安装依赖: pip install comtypes")
        return
    if args.calibrate:
        sys.exit(run_calibrate(args.calibrate, args.open_menu))
    if args.now:
        run_headless(args.dry_run)
        return
    if not acquire_single_instance():
        if focus_existing_instance():
            return
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(APP_TITLE, "程序已在运行中（请查看任务栏）。")
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


def report_fatal() -> None:
    """启动阶段崩溃时留痕：写日志 + 弹框。

    pythonw 没有控制台，未捕获异常默认被丢掉 —— 用户看到的就是"双击一下
    窗口一闪就没了"，而且查无痕迹。这里至少把堆栈落到日志、把摘要弹出来。
    """
    detail = traceback.format_exc()
    stamp = f"[{datetime.now():%H:%M:%S}]"
    try:
        log_dir = config_dir() / "logs"
        log_dir.mkdir(exist_ok=True)
        with open(log_dir / f"{datetime.now():%Y%m%d}.log", "a",
                  encoding="utf-8") as f:
            f.write(f"{stamp} ✖ 启动失败\n{detail}\n")
    except Exception:
        pass
    try:
        last = [ln for ln in detail.strip().splitlines() if ln.strip()][-1]
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            APP_TITLE, f"程序启动失败，已写入日志。\n\n{last}\n\n"
                       f"日志目录：{config_dir() / 'logs'}")
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        report_fatal()
        sys.exit(1)
