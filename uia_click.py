#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UIA 智能点击模块（Token 领取助手的可选增强层，需 `pip install comtypes`）。

原理：客户端由本工具以 --force-renderer-accessibility 启动后，其界面
（Electron/Chromium 应用）会暴露完整的 UI Automation 树；按名称关键词
查找"领取/签到"类元素，优先走 Invoke 模式（不移动鼠标），否则在通过
"目标点确属目标进程窗口"校验后模拟一次鼠标点击。

若本模块不可用（未安装 comtypes），主程序自动退回"仅启动客户端"模式。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import windll, wintypes, WINFUNCTYPE, byref

TH32CS_SNAPPROCESS = 0x2
A11Y_FLAG = "--force-renderer-accessibility"

# UIA 常量
TREE_SCOPE_DESCENDANTS = 4
UIA_INVOKE_PATTERN_ID = 10000
CT_BUTTON, CT_HYPERLINK, CT_MENUITEM = 50000, 50001, 50002
CLICKABLE_TYPES = {50000, 50001, 50002, 50007, 50018, 50019, 50020, 50025, 50029}

_uia_module = None
_uia = None


def available() -> bool:
    """comtypes 是否可用；可用则顺便生成 UIA 包装并初始化 COM。"""
    global _uia_module
    if _uia_module is not None:
        return True
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        _uia_module = comtypes.client.GetModule("UIAutomationCore.dll")
        return True
    except Exception:
        _uia_module = False
        return False


def comtypes_client_create():
    import comtypes
    import comtypes.client
    return comtypes.client.CreateObject(
        "{ff48dba4-60ef-4201-aa87-54103eef594e}",
        interface=_uia_module.IUIAutomation,
        clsctx=comtypes.CLSCTX_INPROC_SERVER)


# ---------------------------------------------------- 进程 / 窗口发现 ------
class _PE32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def list_pids(image_name: str) -> list[int]:
    pids, snap = [], windll.kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPPROCESS, 0)
    entry = _PE32W()
    entry.dwSize = ctypes.sizeof(_PE32W)
    ok = windll.kernel32.Process32FirstW(snap, byref(entry))
    while ok:
        if entry.szExeFile.lower() == image_name.lower():
            pids.append(entry.th32ProcessID)
        ok = windll.kernel32.Process32NextW(snap, byref(entry))
    windll.kernel32.CloseHandle(snap)
    return pids


def _visible_windows(pids: set[int]) -> list[int]:
    """该进程组所有可见顶层窗口，按面积从大到小。"""
    found: list[tuple[int, int]] = []

    @WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, lparam):
        pid = wintypes.DWORD()
        windll.user32.GetWindowThreadProcessId(hwnd, byref(pid))
        if pid.value in pids and windll.user32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            windll.user32.GetWindowRect(hwnd, byref(rect))
            area = max(rect.right - rect.left, 0) * max(rect.bottom - rect.top, 0)
            found.append((hwnd, area))
        return True

    windll.user32.EnumWindows(callback, 0)
    found.sort(key=lambda x: -x[1])
    return [hwnd for hwnd, _ in found]


def _window_pid_at(x: int, y: int) -> int:
    point = wintypes.POINT(x, y)
    hwnd = windll.user32.WindowFromPoint(point)
    pid = wintypes.DWORD()
    windll.user32.GetWindowThreadProcessId(hwnd, byref(pid))
    return pid.value


# ------------------------------------------------------------- 点击 ------
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004


def _mouse_click(x: int, y: int):
    """带原位恢复的模拟鼠标点击。"""
    pt = wintypes.POINT()
    windll.user32.GetCursorPos(byref(pt))
    windll.user32.SetCursorPos(x, y)
    time.sleep(0.05)
    for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        inp = _INPUT(type=0, mi=_MOUSEINPUT(0, 0, 0, flag, 0, None))
        windll.user32.SendInput(1, byref(inp), ctypes.sizeof(_INPUT))
        time.sleep(0.05)
    time.sleep(0.3)
    windll.user32.SetCursorPos(pt.x, pt.y)


# ------------------------------------------------- 元素查找与点击 ------
def _safe_props(el):
    """逐个属性独立容错读取，任何一个失败不影响已取到的其余值。"""
    name, ctype, rect = "", 0, None
    offscreen, enabled = 1, False
    try:
        name = (el.CurrentName or "").strip()
    except Exception:
        pass
    try:
        ctype = el.CurrentControlType or 0
    except Exception:
        pass
    try:
        offscreen = el.CurrentIsOffscreen
    except Exception:
        pass
    try:
        enabled = bool(el.CurrentIsEnabled)
    except Exception:
        pass
    try:
        rect = el.CurrentBoundingRectangle
    except Exception:
        pass
    return name, ctype, offscreen, rect, enabled


def classify(name: str, ctype: int, keywords: list[str],
             negative: tuple[str, ...]) -> str:
    """把界面元素分成三类：negative=已完成标识，candidate=可点的候选，skip=无关。

    独立成函数是为了能脱离 COM 环境直接单元测试 —— 判定规则本身才决定
    「点哪里」，不该只有连上真实界面才能验证。

    负向词用「已签」而非「已签到」，是因为实际界面写的是「今日已签」，
    不含「到」字，用「已签到」匹配不到（TraeWork CN 就是这样）。
    """
    if any(neg in name for neg in negative):
        return "negative"
    if ctype in CLICKABLE_TYPES and any(kw in name for kw in keywords):
        return "candidate"
    return "skip"


def find_and_click(image_name: str, keywords: list[str],
                   negative: tuple[str, ...] = ("已领", "已签", "已完成"),
                   clicked_names: set[str] | None = None,
                   point_pct: tuple[float, float] | None = None
                   ) -> tuple[str, str]:
    """在目标进程窗口中找关键词元素并点击。

    返回 (status, detail)，status 取值：
      no-window  进程或可见窗口尚未出现
      no-tree    窗口在，但界面树几乎为空（该实例未带无障碍参数启动）
      no-element 树正常但没有匹配的元素
      waiting    只剩已点过/负向匹配的元素，等待界面变化
      already    检测到"今日已领"类完成标识，无需点击
      blocked    坐标点击被其他窗口遮挡（仅 point_pct 模式）
      clicked    已点击（detail 为元素名/坐标）
      error      其他异常
    """
    if not available():
        return "error", "comtypes 不可用"
    global _uia
    try:
        if _uia is None:
            _uia = comtypes_client_create()
        pids = set(list_pids(image_name))
        if not pids:
            return "no-window", "进程未运行"
        hwnds = _visible_windows(pids)
        if not hwnds:
            return "no-window", "可见窗口未出现"

        # 手动坐标模式：不做 UIA 查找，直接按窗口百分比坐标点
        if point_pct:
            rect = wintypes.RECT()
            windll.user32.GetWindowRect(hwnds[0], byref(rect))
            x = int(rect.left + (rect.right - rect.left) * point_pct[0] / 100)
            y = int(rect.top + (rect.bottom - rect.top) * point_pct[1] / 100)
            if _window_pid_at(x, y) in pids:
                _mouse_click(x, y)
                return "clicked", f"坐标({x},{y})"
            return "blocked", f"({x},{y}) 被其他窗口遮挡"

        done_names = clicked_names if clicked_names is not None else set()
        candidate, already_seen = None, False
        tree_named, tree_total = 0, 0
        for hwnd in hwnds[:3]:   # 只看最大的几个窗口，忽略小工具窗
            try:
                root = _uia.ElementFromHandle(hwnd)
                allc = root.FindAll(TREE_SCOPE_DESCENDANTS,
                                    _uia.CreateTrueCondition())
            except Exception:
                continue
            tree_total += allc.Length
            for i in range(min(allc.Length, 5000)):
                el = allc.GetElement(i)
                name, ctype, offscreen, rect, enabled = _safe_props(el)
                if not name or not enabled or offscreen:
                    continue
                tree_named += 1
                verdict = classify(name, ctype, keywords, negative)
                if verdict == "negative":
                    already_seen = True
                    continue
                if verdict == "candidate" and name not in done_names:
                    if candidate is None:
                        candidate = (el, name)
            if candidate:
                break

        if tree_total and tree_named < 4:
            return "no-tree", "界面树为空（该实例可能未带无障碍参数启动）"
        if candidate is None:
            if already_seen:
                return "already", "检测到已完成领取的标识"
            return "no-element", "未找到匹配按钮"

        el, name = candidate
        invoked = False
        try:
            pattern = el.GetCurrentPattern(UIA_INVOKE_PATTERN_ID)
            if pattern:
                pattern.QueryInterface(
                    _uia_module.IUIAutomationInvokePattern).Invoke()
                invoked = True
        except Exception:
            invoked = False
        if not invoked:
            rect = _safe_props(el)[3]
            if rect is None or rect.right - rect.left <= 0:
                return "no-element", f"“{name}” 无有效位置"
            x = (rect.left + rect.right) // 2
            y = (rect.top + rect.bottom) // 2
            if _window_pid_at(x, y) not in set(list_pids(image_name)):
                return "blocked", f"“{name}” 被其他窗口遮挡"
            _mouse_click(x, y)
        done_names.add(name)
        return "clicked", ("Invoke" if invoked else "鼠标点击") + f"“{name}”"
    except Exception as e:  # noqa: BLE001 - 单次尝试失败不应影响调度循环
        return "error", repr(e)


def dump_tree(image_name: str, contains: str = "",
              max_lines: int = 300) -> list[str]:
    """枚举目标进程窗口的 UIA 树（--inspect 用），返回文本行。"""
    if not available():
        return ["comtypes 不可用：请先 pip install comtypes"]
    lines: list[str] = []
    global _uia
    if _uia is None:
        _uia = comtypes_client_create()
    pids = set(list_pids(image_name))
    if not pids:
        return [f"未发现进程 {image_name}（请先手动启动它再探测）"]
    hwnds = _visible_windows(pids)
    if not hwnds:
        return [f"{image_name} 没有可见窗口"]
    lines.append(f"进程 {image_name}：{len(pids)} 个 PID，"
                 f"{len(hwnds)} 个可见窗口")
    for hwnd in hwnds[:3]:
        try:
            root = _uia.ElementFromHandle(hwnd)
            allc = root.FindAll(TREE_SCOPE_DESCENDANTS,
                                _uia.CreateTrueCondition())
        except Exception as e:
            lines.append(f"  窗口 {hwnd:#x}: 枚举失败 {e!r}")
            continue
        lines.append(f"  窗口 {hwnd:#x}：{allc.Length} 个元素"
                     + ("（树很小，可能是未带无障碍参数启动的实例）"
                        if allc.Length < 15 else ""))
        shown = 0
        for i in range(min(allc.Length, 5000)):
            el = allc.GetElement(i)
            name, ctype, offscreen, rect, enabled = _safe_props(el)
            if not name:
                continue
            if contains and contains not in name:
                continue
            if shown >= max_lines:
                lines.append("  …（超出显示上限）")
                break
            pos = ""
            if rect and rect.right > rect.left:
                pos = f" @({rect.left},{rect.top}) {rect.right-rect.left}x{rect.bottom-rect.top}"
            flags = "" if enabled else " [禁用]"
            if offscreen:
                flags += " [屏幕外]"
            lines.append(f"    [ct{ctype}] {name[:70]}{pos}{flags}")
            shown += 1
    return lines
