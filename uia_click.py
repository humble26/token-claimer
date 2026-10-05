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
SW_RESTORE = 9
HWND_TOP = 0
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
TREE_WARMUP_MAX = 3.0   # 激活窗口后最多等多久让无障碍树重建
TREE_READY_MIN = 15     # 树元素数达到这个值即视为已重建（塌陷态只有 8）

_uia_module = None
_uia = None


def available() -> bool:
    """comtypes 是否可用；可用则顺便生成 UIA 包装并初始化 COM。

    注意缓存的是三态：None=还没探测，False=探测失败，模块对象=可用。
    早期写成 `if _uia_module is not None: return True`，于是首次探测失败后
    被缓存成 False，第二次调用却因"不是 None"而返回 True —— 在没装 comtypes
    的机器上，第一次调用（如 run_claim 里的 smart_click_active）说"不可用"，
    紧接着 launch_extra_args 再问一次就变成"可用"，会给客户端多加无障碍参数，
    并生成永远点不中的点击任务。判定必须显式排除 False。
    """
    global _uia_module
    if _uia_module is not None:
        return _uia_module is not False
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


def mouse_button(flag: int):
    """发送一次鼠标按键事件（LEFTDOWN / LEFTUP）。

    抽出来给"按住不放"的复合操作（如验证码滑块拖拽）复用：拖拽需要在若干
    次 SetCursorPos 之间保持左键按下，而 _mouse_click 是"按下即抬起"的原子
    点击，用它做不出拖拽。这里沿用同一套 SendInput 结构，行为与 _mouse_click
    的按键部分完全一致。
    """
    inp = _INPUT(type=0, mi=_MOUSEINPUT(0, 0, 0, flag, 0, None))
    windll.user32.SendInput(1, byref(inp), ctypes.sizeof(_INPUT))


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


# --------------------------------------------- 窗口矩形 / 还原 ------
def _restore_if_minimized(hwnd) -> bool:
    """窗口最小化时把它还原，否则枚举到的元素没有可用坐标。

    只在 IsIconic 为真时才调 SW_RESTORE：对已经最大化的窗口调 SW_RESTORE 会
    顺手把它还原成普通尺寸（用户会看到窗口突然变小），那不是本工具该干的事。
    最小化的窗口则相反 —— 还原后 UIA 才给得出真实坐标，点击才落得准。
    """
    try:
        if windll.user32.IsIconic(hwnd):
            windll.user32.ShowWindow(hwnd, SW_RESTORE)
            return True
    except Exception:
        pass
    return False


def _bring_to_foreground(hwnd) -> bool:
    """把窗口激活到前台，返回是否真的切换了前台。

    Chromium/Electron 客户端的 UIA 树只在窗口处于前台时才存在：窗口失去前台
    几秒后，树会塌成 8 个无名 Pane，此后必须重新激活窗口才能恢复（实测冷启动
    后保持置前 60 秒树稳定在 177，一旦切到别的窗口就掉到 8）。所以点击前必须
    先激活目标窗口，否则查找必然落在空树上。

    直接调 SetForegroundWindow 在"调用进程自己不是前台"时会被系统静默拒绝
    （后台运行的 pythonw 正是这种情况），因此先 AttachThreadInput 借用前台线程
    的输入队列，设完再解除 —— 这是 Windows 下跨进程置前的标准做法。
    """
    user32 = windll.user32
    try:
        if not hwnd or user32.GetForegroundWindow() == hwnd:
            return False
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        fg = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        fg_thread = user32.GetWindowThreadProcessId(fg, byref(pid)) if fg else 0
        cur_thread = windll.kernel32.GetCurrentThreadId()
        attached = False
        try:
            if fg_thread and fg_thread != cur_thread:
                attached = bool(user32.AttachThreadInput(fg_thread, cur_thread,
                                                         True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_thread, cur_thread, False)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _wait_tree_ready(hwnd, timeout: float = TREE_WARMUP_MAX) -> bool:
    """激活窗口后等无障碍树重建，一就绪立刻返回，避免固定空等。

    实测树通常 1 秒内就回来，偶尔要 2~3 秒。固定 sleep 2 秒要么白等、要么不够，
    改成每 150ms 探一次：就绪即走，既快又稳。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            root = _uia.ElementFromHandle(hwnd)
            allc = root.FindAll(TREE_SCOPE_DESCENDANTS,
                                _uia.CreateTrueCondition())
            if allc.Length >= TREE_READY_MIN:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _window_rect(root, hwnd):
    """窗口矩形优先取 UIA 根元素的包围盒，拿不到再退回 GetWindowRect。

    最小化时 GetWindowRect 会返回垃圾值（实测 ZCode 拿到
    (-21333,-21333,-21175,-21307)，尺寸 158x26），拿它做百分比区域过滤必然
    判「不在区域内」—— 带 region_pct 的步骤就会静默失效，报成 no-element，
    看起来像"按钮没找到"，实则窗口矩形本身是坏的。UIA 的包围盒与元素坐标同
    一坐标系，天然自洽，是更可靠的来源。
    """
    try:
        r = root.CurrentBoundingRectangle
        if r.right > r.left and r.bottom > r.top:
            return r
    except Exception:
        pass
    rect = wintypes.RECT()
    try:
        windll.user32.GetWindowRect(hwnd, byref(rect))
    except Exception:
        pass
    return rect


def _virtual_screen() -> tuple[int, int, int, int]:
    """虚拟屏幕（含全部显示器）的 (x, y, w, h)。

    SM_XVIRTUALSCREEN=76 … SM_CYVIRTUALSCREEN=79。多显示器下 x/y 可能是负数
    （副屏在主屏左侧/上方），所以判"在不在屏幕上"必须用这四个值，不能假设
    原点就是 (0,0)。
    """
    try:
        u = windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    except Exception:
        return (0, 0, 0, 0)


def _rect_usable(rect) -> bool:
    """矩形是否有可用尺寸，并且至少与虚拟屏幕有交集。

    光看 right>left、bottom>top 是不够的：最小化的窗口会拿到"尺寸看着正常、
    坐标却在屏幕外"的垃圾矩形（实测 ZCode 是 (-21333,-21333)-(-21175,-21307)，
    尺寸 158x26），只验尺寸会把它当好矩形，于是带 region_pct 的步骤在一个
    屏幕外的坐标系里做百分比过滤，必然一个都匹配不到，最后误报成 no-element。
    多加一条"与虚拟屏幕相交"就能把它挡掉；副屏上的负坐标窗口仍与虚拟屏幕
    相交，不受影响。拿不到屏幕信息时退化为只验尺寸，避免误伤。
    """
    if not rect or rect.right <= rect.left or rect.bottom <= rect.top:
        return False
    vx, vy, vw, vh = _virtual_screen()
    if vw <= 0 or vh <= 0:
        return True
    return (rect.right > vx and rect.left < vx + vw
            and rect.bottom > vy and rect.top < vy + vh)


# ------------------------------------------------- 元素查找与点击 ------
def _name_of(el) -> str:
    try:
        return (el.CurrentName or "").strip()
    except Exception:
        return ""


def _safe_props(el, name=None):
    """逐个属性独立容错读取，任何一个失败不影响已取到的其余值。

    name 由调用方取过时可以传进来，省掉一次跨进程读：Electron 客户端每读一
    个属性都是一次同步 IPC，几百个元素逐个读五个属性足以把渲染进程卡到"未
    响应"。树里绝大多数元素没有名字，先读名字就能把四次读省掉。
    """
    ctype, rect = 0, None
    offscreen, enabled = 1, False
    if name is None:
        name = _name_of(el)
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


def element_verdict(name: str, ctype: int, offscreen: bool, enabled: bool,
                    in_rgn: bool, keywords: list[str],
                    negative: tuple[str, ...]) -> str:
    """单个元素最终的 negative / candidate / skip 判定。

    顺序是有讲究的，两条都踩过坑：
      · 屏幕外元素直接跳过 —— 聊天正文和历史消息里常出现「已完成」这类字样，
        不排除就会把一条历史消息当成"今天已领"。
      · 完成标识必须先于"跳过禁用元素"判定。已领取的按钮通常是 [禁用] 的：
        TraeWork 领完变「今日已签」、WorkBuddy 变「今日已领」，CurrentIsEnabled
        都是 False。原先先按 enabled 过滤再判定，于是"今天已领"被当成"没找到
        按钮"，一路重试到预算耗尽 —— 实测白转 78 秒。
    """
    if offscreen:
        return "skip"
    verdict = classify(name, ctype, keywords, negative)
    if verdict == "negative":
        return "negative" if in_rgn else "skip"
    if not enabled or not in_rgn:
        return "skip"
    return verdict


def in_region(rect, win_rect, region_pct) -> bool:
    """元素中心是否落在窗口的指定百分比区域内。

    用于把"名字太普通"的元素圈死：TraeWork CN 的账户行名字里带用户名，而
    聊天正文也可能出现同样的字，光靠关键词会误命中，加上"只在左下角找"
    就稳了。坐标是窗口内百分比，窗口大小变了也不受影响。
    """
    if rect is None or win_rect is None:
        return False
    w = win_rect.right - win_rect.left
    h = win_rect.bottom - win_rect.top
    if w <= 0 or h <= 0:
        return False
    cx = (rect.left + rect.right) / 2 - win_rect.left
    cy = (rect.top + rect.bottom) / 2 - win_rect.top
    x0, y0, x1, y1 = region_pct
    return (x0 * w / 100 <= cx <= x1 * w / 100
            and y0 * h / 100 <= cy <= y1 * h / 100)


def find_and_click(image_name: str, keywords: list[str],
                   negative: tuple[str, ...] = ("已领", "已签", "已完成"),
                   clicked_names: set[str] | None = None,
                   point_pct: tuple[float, float] | None = None,
                   region_pct: tuple[float, float, float, float] | None = None,
                   probe: bool = False
                   ) -> tuple[str, str]:
    """在目标进程窗口中找关键词元素并点击。

    probe=True 时只看不点，用于多步点击的前置判断：菜单已经开着就不要再点
    一次触发按钮，否则会把菜单重新点关（toggle）。此时只返回 found /
    not-found，坐标模式因为无法判断可见性一律返回 not-found。

    返回 (status, detail)，status 取值：
      no-window  进程或可见窗口尚未出现
      no-tree    窗口在，但界面树没有内容（实例未带无障碍参数，或树已塌陷）
      no-element 树正常但没有匹配的元素
      already    检测到"今日已领"类完成标识，无需点击
      blocked    坐标点击被其他窗口遮挡（仅 point_pct 模式）
      clicked    已点击（detail 为元素名/坐标）
      found      仅 probe：目标（或完成标识）已在界面上
      not-found  仅 probe：目标不在界面上
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

        # 先激活目标窗口：Chromium 的 UIA 树只在窗口处于前台时存在，窗口一旦
        # 失去前台，树几秒内就塌成 8 个无名 Pane，查找必然落空。真发生前台切换
        # 时给它一点时间把树重建出来（实测 1~2 秒）。
        if _bring_to_foreground(hwnds[0]):
            _wait_tree_ready(hwnds[0])

        # 手动坐标模式：不做 UIA 查找，直接按窗口百分比坐标点
        if point_pct:
            if probe:   # 坐标没有"可见性"可查，交给调用方按顺序执行
                return "not-found", "坐标模式无法探测"
            # 与区域查找一样先还原最小化窗口：最小化时 GetWindowRect 返回屏幕外
            # 的垃圾矩形，按它算出的百分比坐标会落到别的显示器上，最后误报成
            # "被其他窗口遮挡"，用户完全看不懂。
            _restore_if_minimized(hwnds[0])
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
        region_ok, saw_bad_rect = not region_pct, False
        for hwnd in hwnds[:3]:   # 只看最大的几个窗口，忽略小工具窗
            _restore_if_minimized(hwnd)
            try:
                root = _uia.ElementFromHandle(hwnd)
                allc = root.FindAll(TREE_SCOPE_DESCENDANTS,
                                    _uia.CreateTrueCondition())
            except Exception:
                continue
            tree_total += allc.Length
            win_rect = _window_rect(root, hwnd)
            if not _rect_usable(win_rect):
                # 最小化/不可见的窗口没有可用矩形，百分比区域过滤无从谈起，
                # 换下一个窗口；一个都没有时统一报 minimized（见下）。
                saw_bad_rect = True
                continue
            region_ok = True
            for i in range(min(allc.Length, 5000)):
                el = allc.GetElement(i)
                name = _name_of(el)
                if not name:      # 无名元素占多数，先读名字省掉后面四次跨进程读
                    continue
                _, ctype, offscreen, rect, enabled = _safe_props(el, name)
                if not offscreen and enabled:
                    tree_named += 1   # 区域过滤前先计数，免得被当成"界面树为空"
                in_rgn = (not region_pct
                          or in_region(rect, win_rect, region_pct))
                verdict = element_verdict(name, ctype, offscreen, enabled,
                                          in_rgn, keywords, negative)
                if verdict == "negative":
                    already_seen = True
                    continue
                if verdict == "candidate" and name not in done_names:
                    if candidate is None:
                        candidate = (el, name)
            if candidate:
                break

        if region_pct and saw_bad_rect and not region_ok:
            # 别退化成 no-element：那不是"没找到按钮"，而是窗口本身不可用，
            # 提示用户还原窗口比让他去翻关键词更有用。
            return "minimized", "目标窗口已最小化，无法按区域查找元素"
        if tree_total and tree_named < 4:
            # 树只有个位数元素 = 只剩窗口骨架（实测塌陷后是 8 个无名 Pane）；
            # 树很大却几乎没有命名元素，说明是别的问题，别混为一谈。
            if tree_total < 15:
                return "no-tree", ("界面树没有内容（该实例未带无障碍参数启动，"
                                   "或运行中树已塌陷）")
            return "no-tree", f"界面树异常（{tree_total} 个元素里没有可交互项）"
        if probe:
            # 完成标识也算"目标已在界面上"：菜单开着且今天已领时，下一步
            # 会据此判定 already，不必再走一遍前置点击。
            if candidate is not None:
                return "found", f"“{candidate[1]}”已可见"
            if already_seen:
                return "found", "已完成标识已可见"
            return "not-found", "目标不可见"
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


def format_element(name: str, ctype: int, rect, enabled: bool,
                   offscreen: int) -> str:
    """把单个界面元素格式化成清单里的一行。

    结尾的标记很重要：[可点] 表示该元素类型在 CLICKABLE_TYPES 里，能被智能
    点击命中；[禁用]/[屏幕外] 的通常不该选。校准领取入口时就看这一列。
    """
    pos = ""
    if rect and rect.right > rect.left:
        pos = (f" @({rect.left},{rect.top}) "
               f"{rect.right - rect.left}x{rect.bottom - rect.top}")
    flags = "" if enabled else " [禁用]"
    if offscreen:
        flags += " [屏幕外]"
    if ctype in CLICKABLE_TYPES:
        flags += " [可点]"
    return f"[ct{ctype}] {name[:70]}{pos}{flags}"


def has_window(image_name: str) -> bool:
    """该进程当前是否有可见窗口（校准时要等窗口出来再枚举）。"""
    try:
        pids = set(list_pids(image_name))
        return bool(pids and _visible_windows(pids))
    except Exception:
        return False


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
            name = _name_of(el)
            if not name or (contains and contains not in name):
                continue
            if shown >= max_lines:
                lines.append("  …（超出显示上限）")
                break
            _, ctype, offscreen, rect, enabled = _safe_props(el, name)
            lines.append("    " + format_element(name, ctype, rect, enabled,
                                                  offscreen))
            shown += 1
            if i % 100 == 0:
                time.sleep(0.05)   # 让渲染进程喘口气，别把客户端读成"未响应"
    return lines


def tree_size(image_name: str) -> int:
    """目标进程最大可见窗口的元素总数；0 表示没窗口或枚举失败。

    校准用：先量一下树有多大再决定下一步 —— 运行中的普通实例树是空的
    （实测 TraeWork CN 只有 13 个标题栏按钮），带无障碍参数启动的实例有
    几百个。这里只做 FindAll，不逐个读属性，所以不会给客户端压力。
    """
    if not available():
        return 0
    global _uia
    try:
        if _uia is None:
            _uia = comtypes_client_create()
        pids = set(list_pids(image_name))
        if not pids:
            return 0
        biggest = 0
        for hwnd in _visible_windows(pids)[:3]:
            try:
                allc = _uia.ElementFromHandle(hwnd).FindAll(
                    TREE_SCOPE_DESCENDANTS, _uia.CreateTrueCondition())
            except Exception:
                continue
            biggest = max(biggest, allc.Length)
        return biggest
    except Exception:
        return 0
