# -*- coding: utf-8 -*-
"""页面驱动层：把"解一个滑块"的流程与具体自动化框架解耦。

设计：
  - 驱动接口（鸭子类型，见 PageDriver 文档）：eval_js / grab / box /
    mouse_move / mouse_down / mouse_up / sleep / wait_seq_beyond。
    内置 AsyncPageDriver（playwright.async_api.Page）与 SyncPageDriver
    （playwright.sync_api.Page）两个实现；宿主若用别的通道
    （Win32 SendInput、CDP、远程控制等），实现同一组方法即可复用整个求解流程。
  - 求解流程只写一遍：抓图 -> find_gap -> 换算拖动距离 -> 解释动作序列。
  - 不在模块顶层导入 playwright：宿主只用图像定位时无需安装它。

公开入口：
  solve_on_page(page, ...)        异步：在 playwright.async_api 的页面上解一次
  solve_on_page_sync(page, ...)   同步：在 playwright.sync_api 的页面上解一次
"""
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .detector import Gap, find_gap
from .trajectory import Tuning, build_choreography

try:
    import cv2
except ImportError:
    cv2 = None

log = logging.getLogger(__name__)


def _require_cv2():
    if cv2 is None:
        raise ImportError('求解需要 OpenCV：pip install opencv-python（或 opencv-python-headless）')


@dataclass
class Selectors:
    """验证码页面元素与状态探针。默认值对应内置 demo；适配真实站点时按需替换。

    ready/new_challenge/solved/error 都是 JS 表达式而非固定变量名，
    使宿主无需改库代码就能适配不同站点的状态标记。
    """

    bg: str = '#bgCanvas'                     # 背景大图（canvas 或 img 选择器）
    piece: str = '#pieceCanvas'               # 拼图块小图选择器
    slider: str = '#btn'                      # 可按住的滑块按钮选择器
    ready_js: str = 'window.captchaSeq'       # 题目版本号（int，换题自增）
    solved_js: str = 'window.captchaSolved === true'
    error_js: str = 'window.captchaLastError'
    new_challenge_js: str = 'typeof newChallenge === "function" && newChallenge()'


@dataclass
class SolveResult:
    ok: bool
    gap: Gap
    distance: float           # 实际执行的拖动距离（页面 CSS 像素，含校准偏置）
    err: Optional[float]      # 页面回报的偏差像素（若可得）
    elapsed: float            # 本单次求解耗时（秒）


# 一次往返抓齐：背景图、拼图块图（canvas 用 toDataURL 保留 alpha）、两者几何
GRAB_JS = """
([bgSel, pieceSel]) => {
  const grab = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    if (el.tagName === 'CANVAS' && el.toDataURL) return el.toDataURL('image/png');
    return null;
  };
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height};
  };
  return {bg: grab(bgSel), piece: grab(pieceSel),
          bgBox: box(bgSel), pieceBox: box(pieceSel)};
}
"""


def decode_image(b64_or_data_url) -> np.ndarray:
    """base64（或 data URL）PNG/JPEG -> ndarray（BGR/BGRA）。"""
    _require_cv2()
    payload = b64_or_data_url.split(',', 1)[1] if ',' in b64_or_data_url[:64] \
        else b64_or_data_url
    buf = np.frombuffer(base64.b64decode(payload), np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)


class AsyncPageDriver:
    """playwright.async_api.Page 适配器。"""

    def __init__(self, page):
        self.page = page

    async def eval_js(self, expr, arg=None):
        return await self.page.evaluate(expr, arg)

    async def grab(self, bg_sel, piece_sel) -> Dict[str, Any]:
        d = await self.page.evaluate(GRAB_JS, [bg_sel, piece_sel])
        for key, sel in (('bg', bg_sel), ('piece', piece_sel)):
            if d[key] is None:            # 非 canvas 元素退化为元素截图
                png = await self.page.locator(sel).screenshot()
                d[key] = base64.b64encode(png).decode('ascii')
        return d

    async def box(self, sel) -> Dict[str, float]:
        return await self.page.locator(sel).bounding_box()

    async def mouse_move(self, x, y):
        await self.page.mouse.move(x, y)

    async def mouse_down(self):
        await self.page.mouse.down()

    async def mouse_up(self):
        await self.page.mouse.up()

    async def sleep(self, ms):
        await self.page.wait_for_timeout(int(ms))

    async def wait_seq_beyond(self, seq, ready_js, timeout=15000):
        await self.page.wait_for_function(f'(({ready_js}) | 0) > {int(seq)}',
                                          timeout=timeout)


class SyncPageDriver:
    """playwright.sync_api.Page 适配器，接口与 AsyncPageDriver 一致（同步签名）。"""

    def __init__(self, page):
        self.page = page

    def eval_js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def grab(self, bg_sel, piece_sel) -> Dict[str, Any]:
        d = self.page.evaluate(GRAB_JS, [bg_sel, piece_sel])
        for key, sel in (('bg', bg_sel), ('piece', piece_sel)):
            if d[key] is None:
                png = self.page.locator(sel).screenshot()
                d[key] = base64.b64encode(png).decode('ascii')
        return d

    def box(self, sel) -> Dict[str, float]:
        return self.page.locator(sel).bounding_box()

    def mouse_move(self, x, y):
        self.page.mouse.move(x, y)

    def mouse_down(self):
        self.page.mouse.down()

    def mouse_up(self):
        self.page.mouse.up()

    def sleep(self, ms):
        self.page.wait_for_timeout(int(ms))

    def wait_seq_beyond(self, seq, ready_js, timeout=15000):
        self.page.wait_for_function(f'(({ready_js}) | 0) > {int(seq)}',
                                    timeout=timeout)


def _plan_and_drag_inputs(d, bias) -> Tuple[Gap, float]:
    """抓图 -> 定位 -> 换算拖动距离（同步/异步流程共用）。"""
    bg = decode_image(d['bg'])
    piece = decode_image(d['piece'])
    gap = find_gap(bg, piece)
    # 画布像素 -> 页面 CSS 像素（处理高分屏/缩放），bias 为校准偏置
    scale = d['bgBox']['w'] / float(bg.shape[1])
    piece_x = d['pieceBox']['x'] - d['bgBox']['x']
    distance = gap.x * scale - piece_x - bias
    return gap, distance


def _center(box) -> Tuple[float, float]:
    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2


async def solve_with_driver(drv, selectors=None, tuning=None, bias=0.0) -> SolveResult:
    """在驱动上解一次当前题目（异步）。题目就绪/换题由宿主或 runner 负责。"""
    sel = selectors or Selectors()
    t0 = time.perf_counter()
    d = await drv.grab(sel.bg, sel.piece)
    gap, distance = _plan_and_drag_inputs(d, bias)
    cx, cy = _center(await drv.box(sel.slider))

    await drv.mouse_move(cx, cy)
    for op in build_choreography(distance, tuning):
        kind = op[0]
        if kind == 'down':
            await drv.mouse_down()
        elif kind == 'up':
            await drv.mouse_up()
        elif kind == 'move':
            await drv.mouse_move(cx + op[1], cy + op[2])
        else:
            await drv.sleep(op[1])

    ok = bool(await drv.eval_js(sel.solved_js))
    err = await drv.eval_js(sel.error_js)
    return SolveResult(ok=ok, gap=gap, distance=distance, err=err,
                       elapsed=time.perf_counter() - t0)


def solve_with_driver_sync(drv, selectors=None, tuning=None, bias=0.0) -> SolveResult:
    """在驱动上解一次当前题目（同步），流程与异步版完全一致。"""
    sel = selectors or Selectors()
    t0 = time.perf_counter()
    d = drv.grab(sel.bg, sel.piece)
    gap, distance = _plan_and_drag_inputs(d, bias)
    cx, cy = _center(drv.box(sel.slider))

    drv.mouse_move(cx, cy)
    for op in build_choreography(distance, tuning):
        kind = op[0]
        if kind == 'down':
            drv.mouse_down()
        elif kind == 'up':
            drv.mouse_up()
        elif kind == 'move':
            drv.mouse_move(cx + op[1], cy + op[2])
        else:
            drv.sleep(op[1])

    ok = bool(drv.eval_js(sel.solved_js))
    err = drv.eval_js(sel.error_js)
    return SolveResult(ok=ok, gap=gap, distance=distance, err=err,
                       elapsed=time.perf_counter() - t0)


async def solve_on_page(page, selectors=None, tuning=None, bias=0.0) -> SolveResult:
    """便捷入口：在 playwright.async_api 的页面上解一次当前题目。"""
    return await solve_with_driver(AsyncPageDriver(page), selectors, tuning, bias)


def solve_on_page_sync(page, selectors=None, tuning=None, bias=0.0) -> SolveResult:
    """便捷入口：在 playwright.sync_api 的页面上解一次当前题目。

    注意：同步 Page 不能在运行中的 asyncio 事件循环里使用。
    """
    return solve_with_driver_sync(SyncPageDriver(page), selectors, tuning, bias)
