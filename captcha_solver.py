#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""滑块验证码自动处理（路线 B：屏幕截图 + 鼠标拖拽）。

现场依据见 docs/验证码勘察报告.md：
  · 验证码是阿里云滑块拼图，渲染在客户端主窗口内的应用内浮层（不是独立窗口）；
  · 宿主进程 DPI 不感知，UIA 坐标 / 桌面 BitBlt 截图 / SetCursorPos 三者同处
    "逻辑像素"坐标系（本机 150% 缩放屏实测为 1707x1067），所以从截图量出的
    像素距离就是光标要走的距离，全程不需要 DPI 换算；
  · 拼图块与背景在无障碍树里是同一张合成图（拿不到 alpha），缺口定位走
    slider_captcha.detector.find_gap 的 piece=None（edge_only）兜底路径。

本模块只负责"一次尝试"：定位浮层 → 截图 → 找缺口 → 拖拽 → 判定。重试预算、
限频与人工回退由 token_claimer 的 ClaimEngine 负责（process_click_job /
_process_captcha）。任何失败都返回 (False, 原因) 而不抛异常，绝不阻塞领取主流程。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

import uia_click

SRCCOPY = 0x00CC0020
IMAGE_CT = 50006                      # UIA Image 控件类型
# 浮层出现时必然命中的文本（阿里云 NoCaptcha 特征）
OVERLAY_MARKERS = ("请完成安全验证", "拖动滑块完成拼图", "刷新验证码", "CertifyId")

_np = None
_cv2 = None


# ------------------------------------------------------------ 依赖探测 ------
def available() -> bool:
    """numpy + opencv 是否就绪（三态缓存：None=未探测，False=不可用）。

    这里必须显式区分"没探过"和"探过且失败"：宿主解释器（3.14）两者都有，
    但打包/换机后可能缺；一旦把失败缓存成可用，solve() 就会在真正需要时抛
    ImportError，而不是安静地走人工回退。
    """
    global _np, _cv2
    if _np is None:
        try:
            import numpy
            _np = numpy
        except Exception:
            _np = False
    if _cv2 is None:
        try:
            import cv2
            _cv2 = cv2
        except Exception:
            _cv2 = False
    return _np is not False and _cv2 is not False


# ---------------------------------------------------------- 屏幕截图 ------
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def capture_screen():
    """抓整个虚拟屏幕（逻辑像素），返回 (BGR ndarray, origin_x, origin_y)。

    只能抓桌面 DC 再按包围盒裁剪：窗口 DC 的 BitBlt 对 GPU 渲染的 Electron
    返回全黑（实测）。返回的 origin 是虚拟屏幕左上角，用于把 UIA 的绝对坐标
    换算成图像下标（多显示器下可能为负数，本机为 0,0）。
    """
    if not available():
        return None
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    w = u.GetSystemMetrics(78)        # SM_CXVIRTUALSCREEN
    h = u.GetSystemMetrics(79)        # SM_CYVIRTUALSCREEN
    ox = u.GetSystemMetrics(76)       # SM_XVIRTUALSCREEN
    oy = u.GetSystemMetrics(77)
    if w <= 0 or h <= 0:
        return None
    hdc = u.GetDC(0)
    hbmp = g.CreateCompatibleBitmap(hdc, w, h)
    mem = g.CreateCompatibleDC(hdc)
    old = g.SelectObject(mem, hbmp)
    try:
        g.BitBlt(mem, 0, 0, w, h, hdc, ox, oy, SRCCOPY)
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h    # 负数 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(mem, hbmp, 0, h, buf, ctypes.byref(bi), 0)
    except OSError:
        return None
    finally:
        g.SelectObject(mem, old)
        g.DeleteDC(mem)
        g.DeleteObject(hbmp)
        u.ReleaseDC(0, hdc)
    arr = _np.frombuffer(buf, _np.uint8).reshape(h, w, 4)
    return arr[:, :, :3].copy(), ox, oy


# ---------------------------------------------------------- 浮层定位 ------
def _rect_ok(rect) -> bool:
    return bool(rect and rect.right > rect.left and rect.bottom > rect.top)


def _elements(image_name: str) -> list:
    """目标进程可见窗口里的全部元素（含无名元素），返回属性元组列表。

    与 uia_click.find_and_click 不同，这里不能跳过无名元素 —— 滑块手柄在
    无障碍树里恰恰是没有名字的（实测 [ct50020] @(715,571) 17x17）。
    """
    if not uia_click.available():
        return []
    uia = uia_click._uia
    if uia is None:
        uia = uia_click.comtypes_client_create()
        uia_click._uia = uia
    pids = set(uia_click.list_pids(image_name))
    if not pids:
        return []
    out = []
    for hwnd in uia_click._visible_windows(pids)[:3]:
        uia_click._restore_if_minimized(hwnd)
        try:
            root = uia.ElementFromHandle(hwnd)
            allc = root.FindAll(uia_click.TREE_SCOPE_DESCENDANTS,
                                uia.CreateTrueCondition())
        except Exception:
            continue
        for i in range(min(allc.Length, 5000)):
            try:
                el = allc.GetElement(i)
            except Exception:
                continue
            name, ctype, offscreen, rect, enabled = uia_click._safe_props(el)
            out.append((name, ctype, offscreen, rect, enabled))
    return out


def _pick_handle(els, puzzle):
    """在轨道行里找滑块手柄（无名字的小方块）。

    依据 logs/ui_dump_zcode_after_banner2.txt（窗口 1707x1019）：
        拼图区  [ct50006] @(703,350) 301x201
        手柄    [ct50020] @(715,571) 17x17   ← 无名字，中心 (723.5,579.5)
        轨道文案 [ct50020] 拖动滑块完成拼图 @(793,567)
    手柄中心相对拼图区稳定在 (left+20, bottom+28)。拿不到元素时由调用方
    用这个几何关系兜底（见 _handle_center）。
    """
    best, best_d = None, None
    for _name, _ctype, offscreen, rect, _enabled in els:
        if offscreen or not _rect_ok(rect):
            continue
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if not (10 <= w <= 28 and 10 <= h <= 28):
            continue
        cx = (rect.left + rect.right) / 2.0
        cy = (rect.top + rect.bottom) / 2.0
        if not (puzzle.bottom <= cy <= puzzle.bottom + 70):
            continue
        if not (puzzle.left - 30 <= cx <= puzzle.left + 80):
            continue
        d = abs(cx - (puzzle.left + 20)) + abs(cy - (puzzle.bottom + 28))
        if best is None or d < best_d:
            best, best_d = rect, d
    if best is None:
        return None
    return ((best.left + best.right) / 2.0, (best.top + best.bottom) / 2.0)


def _handle_center(puzzle, found):
    """手柄中心：优先用元素实测值，拿不到就按固定几何关系推算。"""
    if found:
        return found
    return (puzzle.left + 20.0, puzzle.bottom + 28.0)


def find_overlay(image_name: str):
    """定位验证码浮层。返回 {puzzle, handle, marker} 或 None（浮层不在）。

    puzzle/handle 都是 UIA 的逻辑坐标（wt.RECT / 元组）；marker 是命中的特征
    文本元素的矩形，仅用于日志。浮层不在 = 没验证码 / 已通过，两种情况调用方
    都按"无需处理"走。
    """
    els = _elements(image_name)
    if not els:
        return None
    marker, images = None, []
    for name, ctype, offscreen, rect, _enabled in els:
        if offscreen or not _rect_ok(rect):
            continue
        if marker is None and any(m in name for m in OVERLAY_MARKERS):
            marker = rect
        if ctype == IMAGE_CT:
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w >= 120 and h >= 80:
                images.append(rect)
    if marker is None or not images:
        return None
    puzzle = max(images, key=lambda r: (r.right - r.left) * (r.bottom - r.top))
    return {"puzzle": puzzle, "handle": _pick_handle(els, puzzle),
            "marker": marker}


def overlay_present(image_name: str) -> bool:
    return find_overlay(image_name) is not None


# ---------------------------------------------------------- 缺口定位 ------
def _crop(img, rect, ox, oy):
    x0 = max(int(rect.left) - ox, 0)
    y0 = max(int(rect.top) - oy, 0)
    x1 = min(int(rect.right) - ox, img.shape[1])
    y1 = min(int(rect.bottom) - oy, img.shape[0])
    if x1 - x0 < 20 or y1 - y0 < 20:
        return None
    return img[y0:y1, x0:x1]


def locate_gap(comp, cfg: dict):
    """在合成图里找缺口。返回 (gap_dict, "") 或 (None, 失败原因)。

    拼图块拿不到 alpha，只能走 find_gap(bg) 的 edge_only 路径；这条路径按
    "轮廓填充率最高"选目标，而**左侧拼图块本身也是一个轮廓**，可能被误当成
    缺口。判据是缺口极少贴着左边缘：命中位置落在左侧 piece_zone_pct% 内时，
    把该轮廓整块排除后重找一次（只动裁剪、不改库）。
    """
    try:
        from slider_captcha.detector import find_gap
    except Exception as e:      # noqa: BLE001
        return None, f"求解库不可用（{e!r}）"
    min_score = float(cfg.get("min_gap_score", 0.55))
    h, w = comp.shape[:2]
    try:
        g = find_gap(comp)
    except Exception as e:      # noqa: BLE001
        return None, f"缺口定位失败（{e!r}）"
    zone = w * float(cfg.get("piece_zone_pct", 15.0)) / 100.0
    if g.x < zone:
        x0 = int(g.bbox_x + g.w + 2)
        if 0 < x0 < w - 30:
            try:
                g2 = find_gap(comp[:, x0:])
            except Exception:   # noqa: BLE001
                g2 = None
            if g2 is not None and g2.score >= min_score:
                g2.x += x0
                g2.bbox_x += x0
                g = g2
    if g.score < min_score:
        return None, (f"缺口置信度不足（{g.score:.3f} < {min_score:.2f}，"
                      f"method={g.method}）")
    return {"x": float(g.x), "y": float(g.y), "score": float(g.score),
            "method": g.method, "w": int(g.w), "h": int(g.h)}, ""


# ------------------------------------------------------------ 拖拽 ------
def drag(start, distance: float, cfg: dict) -> None:
    """从 start（逻辑屏幕坐标）按住左键，按拟人轨迹横向拖 distance 像素。

    每个 move 都用 SetCursorPos 走绝对坐标：宿主是 DPI 不感知进程，
    SetCursorPos 与 UIA 同一坐标系（uia_click 的鼠标点击已验证）。不用
    SendInput 的相对位移，避免相对移动的 DPI 语义差异。
    """
    from slider_captcha.trajectory import build_choreography

    u = ctypes.windll.user32
    sx, sy = int(round(start[0])), int(round(start[1]))
    pt = wt.POINT()
    u.GetCursorPos(ctypes.byref(pt))       # 记下原位置，拖完还回去
    u.SetCursorPos(sx, sy)
    time.sleep(0.15)
    pressed = False
    try:
        for op in build_choreography(distance):
            kind = op[0]
            if kind == "down":
                uia_click.mouse_button(uia_click.MOUSEEVENTF_LEFTDOWN)
                pressed = True
            elif kind == "move":
                u.SetCursorPos(int(round(sx + op[1])), int(round(sy + op[2])))
            elif kind == "sleep":
                time.sleep(op[1] / 1000.0)
            elif kind == "up":
                uia_click.mouse_button(uia_click.MOUSEEVENTF_LEFTUP)
                pressed = False
    finally:
        if pressed:                        # 任何异常都不能把左键卡在按下状态
            uia_click.mouse_button(uia_click.MOUSEEVENTF_LEFTUP)
        u.SetCursorPos(pt.x, pt.y)


# ------------------------------------------------------------ 求解 ------
def _debug_dump(comp, gap, cfg, log):
    """把合成图与标注图写进 logs/，供失败后人工核对（debug_save=true 时）。"""
    try:
        import cv2
        from slider_captcha.detector import debug_annotate, Gap
        log_dir = None
        try:
            from token_claimer import config_dir
            log_dir = config_dir() / "logs"
        except Exception:      # noqa: BLE001 - 独立运行时退回当前目录
            pass
        if log_dir is None:
            return
        log_dir.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        raw = log_dir / f"captcha_{stamp}_raw.png"
        cv2.imwrite(str(raw), comp)
        if gap is not None:
            g = Gap(gap["x"], gap["y"], int(gap["x"]), int(gap["y"]),
                    gap["w"], gap["h"], gap["score"], gap["method"])
            debug_annotate(comp, g, str(log_dir / f"captcha_{stamp}_gap.png"))
        log(f"· 验证码调试图已存：{raw.name}")
    except Exception:          # noqa: BLE001 - 调试输出失败不影响主流程
        pass


def solve(image_name: str, cfg: dict, log=print):
    """对当前浮层做一次尝试。返回 (ok, detail)；ok=True 表示浮层已消失。"""
    if not available():
        return False, "缺少 numpy/opencv，无法自动处理验证码"
    geo = find_overlay(image_name)
    if geo is None:
        # 刚点开入口时浮层可能还没渲染完，等一下再确认一次，避免误判成功
        time.sleep(0.8)
        geo = find_overlay(image_name)
        if geo is None:
            return True, "未检测到验证码浮层（可能无需验证或已通过）"
    shot = capture_screen()
    if shot is None:
        return False, "屏幕截图失败"
    img, ox, oy = shot
    comp = _crop(img, geo["puzzle"], ox, oy)
    if comp is None or comp.size == 0:
        return False, "验证码区域裁剪失败（浮层几何异常）"
    gap, err = locate_gap(comp, cfg)
    if gap is None:
        _debug_dump(comp, None, cfg, log)
        return False, err
    distance = ((gap["x"] - float(cfg.get("piece_x0", 0.0)))
                * float(cfg.get("drag_scale", 1.0))
                + float(cfg.get("drag_bias", 0.0)))
    if distance <= 5:
        _debug_dump(comp, gap, cfg, log)
        return False, f"拖动距离异常（{distance:.1f}px）"
    if cfg.get("debug_save"):
        _debug_dump(comp, gap, cfg, log)
    start = _handle_center(geo["puzzle"], geo["handle"])
    log(f"· 验证码：method={gap['method']} score={gap['score']:.3f} "
        f"距离={distance:.1f}px 起点=({start[0]:.0f},{start[1]:.0f})")
    drag(start, distance, cfg)
    time.sleep(float(cfg.get("settle_seconds", 1.2)))
    if overlay_present(image_name):
        return False, (f"浮层仍在（method={gap['method']} "
                       f"score={gap['score']:.3f} 距离={distance:.1f}px）")
    return True, (f"method={gap['method']} score={gap['score']:.3f} "
                  f"距离={distance:.1f}px")
