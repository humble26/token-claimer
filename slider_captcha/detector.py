# -*- coding: utf-8 -*-
"""滑块验证码缺口定位（OpenCV，纯图像函数，不绑定任何自动化框架）。

输入背景大图与拼图块小图（RGBA，透明背景），输出拼图块目标位置
（元素原点在背景图坐标系中应到达的坐标，即拖动距离的依据）。

准确性设计（v2）：
  - 主力 dark_shape：把背景转为"暗度图"（暗于 Otsu 阈值越多值越大，连续灰度），
    与拼图块 alpha 形状做相关匹配。连续响应的峰是平滑的，可做抛物线亚像素插值
    （实测 300 样本平均误差 0.46px、最大 0.63px；二值掩码上的插值无效，勿用）；
  - 互证候选：dark_shape_bin（二值 Otsu）、dark_shape_ad（自适应阈值，抗光照不均）、
    outline（浅色描边轮廓）、edge_template（边缘模板）、color_sqdiff（掩码 SQDIFF）；
  - 集成决策（ensemble）：候选各自过置信度门槛后按"3px 内互证"聚类，
    簇得分 = 最高分 + 0.05×(簇员数-1)，胜出簇内取优先级最高成员的结果
    （不做均值融合——描边方法的系统性偏差会拖累均值，实测有害）。
  refine=False, ensemble=False 复现 v1 行为，用于 A/B 基准。

本模块只依赖 numpy + opencv（cv2 惰性导入，缺失时给出明确提示），
命令行可独立使用：
  python -m slider_captcha.detector bg.png
  python -m slider_captcha.detector bg.png piece.png --debug
"""
import argparse
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

try:
    import cv2
except ImportError:          # 允许"只装了 numpy"的环境先 import 本包
    cv2 = None


def _require_cv2():
    if cv2 is None:
        raise ImportError(
            '缺口定位需要 OpenCV：pip install opencv-python（服务器/嵌入式可用 opencv-python-headless）')


# 各方法的置信度门槛（低于门槛认为不可信）
GATES = {
    'dark_shape': 0.60,
    'dark_shape_bin': 0.60,
    'dark_shape_ad': 0.60,
    'outline': 0.50,
    'edge_template': 0.35,
    'color_sqdiff': 0.55,
    'edge_only': 0.55,
}

# 方法优先级（同簇内取优先级最高成员的结果；集成决策的平局也按它裁决）
METHOD_ORDER = ['dark_shape', 'dark_shape_bin', 'dark_shape_ad', 'outline',
                'edge_template', 'color_sqdiff', 'edge_only']


@dataclass
class Gap:
    x: float         # 拼图块"元素原点"应到达的 x（浮点，拖动距离以它为准）
    y: float
    bbox_x: int      # 缺口内容包围盒左上角（整数，调试/标注用）
    bbox_y: int
    w: int           # 缺口内容包围盒尺寸
    h: int
    score: float
    method: str


def _to_bgr(img):
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img


def _crop_to_alpha(piece):
    """按 alpha 通道把拼图块裁到外形包围盒，去掉透明边距。返回 (裁剪图, x偏移, y偏移)。"""
    if piece.ndim != 3 or piece.shape[2] != 4:
        return piece, 0, 0
    alpha = piece[:, :, 3]
    ys, xs = np.where(alpha > 10)
    if len(xs) == 0:
        return piece, 0, 0
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    return piece[y0:y1, x0:x1], x0, y0


def _alpha_shape(tpl):
    """拼图块的不透明区域二值掩码（uint8 0/255）。"""
    if tpl.ndim == 3 and tpl.shape[2] == 4:
        return np.where(tpl[:, :, 3] > 128, 255, 0).astype(np.uint8)
    return np.full(tpl.shape[:2], 255, np.uint8)


def _subpixel_peak(res, loc):
    """对响应图做抛物线插值，返回峰位的亚像素偏移 (dx, dy)，越界/非峰返回 (0, 0)。

    只对连续响应有效；二值掩码上的相关峰不是平滑抛物线，插值会引入误差。
    """
    x, y = loc
    dx = dy = 0.0
    h, w = res.shape
    if 0 < x < w - 1:
        l, c, r = float(res[y, x - 1]), float(res[y, x]), float(res[y, x + 1])
        den = l - 2 * c + r
        if den < -1e-12:                       # 只有真正的峰（二阶差分为负）才插值
            dx = max(-0.5, min(0.5, 0.5 * (l - r) / den))
    if 0 < y < h - 1:
        u, c, d = float(res[y - 1, x]), float(res[y, x]), float(res[y + 1, x])
        den = u - 2 * c + d
        if den < -1e-12:
            dy = max(-0.5, min(0.5, 0.5 * (u - d) / den))
    return dx, dy


def _match_response(res, w, h, method, refine):
    """从响应图取峰位（可选亚像素），包装成 Gap。"""
    res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, score, _, loc = cv2.minMaxLoc(res)
    x, y = float(loc[0]), float(loc[1])
    if refine:
        dx, dy = _subpixel_peak(res, loc)
        x, y = x + dx, y + dy
    return Gap(x, y, loc[0], loc[1], w, h, float(score), method)


def _match_shape(mask, tpl, method):
    """二值图上的形状匹配（整数像素，不做亚像素）。"""
    shape = _alpha_shape(tpl)
    res = cv2.matchTemplate(mask, shape, cv2.TM_CCORR_NORMED)
    h, w = shape.shape
    return _match_response(res, w, h, method, refine=False)


def _darkness_map(bg_bgr):
    """灰度暗度图：暗于 Otsu 阈值越多值越大（0..255 连续），响应峰平滑可插值。"""
    g8 = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
    t, _ = cv2.threshold(g8, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    g = g8.astype(np.float32)
    span = max(float(t) - float(g.min()), 1.0)
    return np.clip((float(t) - g) / span, 0.0, 1.0) * 255.0


def _detect_dark_shape(bg_bgr, tpl, refine):
    """暗度图 x 拼图块形状（连续响应 + 亚像素）：缺口压暗处理时最稳。"""
    dark = _darkness_map(bg_bgr)
    shape = _alpha_shape(tpl).astype(np.float32)
    res = cv2.matchTemplate(dark, shape, cv2.TM_CCORR_NORMED)
    h, w = shape.shape
    return _match_response(res, w, h, 'dark_shape', refine)


def _detect_dark_shape_bin(bg_bgr, tpl, refine):
    """二值 Otsu 暗区 x 拼图块形状：v1 主力，保留作互证候选与旧行为基准。"""
    g = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
    _, dark = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return _match_shape(dark, tpl, 'dark_shape_bin')


def _detect_dark_shape_ad(bg_bgr, tpl, refine):
    """自适应阈值的暗区 x 拼图块形状：光照不均/低对比背景下的备份。"""
    g = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
    dark = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 51, 5)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return _match_shape(dark, tpl, 'dark_shape_ad')


def _detect_outline(bg_bgr, tpl, refine):
    """浅色描边检测：缺口外圈通常有一圈高亮描边。"""
    g = cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
    if tpl.ndim == 3 and tpl.shape[2] == 4:
        ys, xs = np.where(tpl[:, :, 3] > 10)
        if len(xs) == 0:
            return None
        pw, ph = int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)
    else:
        ph, pw = tpl.shape[:2]
    for thr in (235, 205):
        _, bw = cv2.threshold(g, thr, 255, cv2.THRESH_BINARY)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if abs(w - pw) > pw * 0.3 or abs(h - ph) > ph * 0.3:
                continue
            fill = cv2.contourArea(c) / float(w * h)
            if fill < 0.5:
                continue
            sc = fill * (1 - abs(w - pw) / pw) * (1 - abs(h - ph) / ph)
            if best is None or sc > best.score:
                best = Gap(float(x), float(y), x, y, w, h, float(sc), 'outline')
        if best is not None:
            return best
    return None


def _detect_edge_template(bg_bgr, tpl, refine):
    """边缘模板匹配：拼图块与缺口亮度一致时有效。"""

    def _edges(img):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(g, (3, 3), 0)
        return cv2.Canny(g, 60, 170)

    res = cv2.matchTemplate(_edges(bg_bgr), _edges(_to_bgr(tpl)), cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    h, w = tpl.shape[:2]
    return Gap(float(loc[0]), float(loc[1]), loc[0], loc[1], w, h, float(score), 'edge_template')


def _detect_color_sqdiff(bg_bgr, tpl, refine):
    """带 alpha 掩码的 SQDIFF 匹配：拼图块与缺口像素一致时最准。"""
    if tpl.ndim != 3 or tpl.shape[2] != 4:
        return None
    alpha = tpl[:, :, 3]
    mask3 = cv2.merge([alpha, alpha, alpha])
    tpl_bgr = cv2.cvtColor(tpl, cv2.COLOR_BGRA2BGR)
    try:
        res = cv2.matchTemplate(bg_bgr, tpl_bgr, cv2.TM_SQDIFF_NORMED, mask=mask3)
    except cv2.error:
        return None
    res = np.nan_to_num(res, nan=1.0, posinf=1.0, neginf=1.0)
    mn, _, loc, _ = cv2.minMaxLoc(res)
    h, w = tpl.shape[:2]
    return Gap(float(loc[0]), float(loc[1]), loc[0], loc[1], w, h, float(1.0 - mn), 'color_sqdiff')


def _detect_edge_only(bg_bgr, tpl=None, refine=True):
    """无拼图块时的兜底：Canny 后按轮廓找疑似缺口（尺寸接近先验、形状接近矩形）。"""
    g = bg_bgr if bg_bgr.ndim == 2 else cv2.cvtColor(bg_bgr, cv2.COLOR_BGR2GRAY)
    e = cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 60, 170)
    cnts, _ = cv2.findContours(e, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bh, bw = bg_bgr.shape[:2]
    best = None
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < 18 or ch < 18 or cw > bw * 0.6 or ch > bh * 0.95:
            continue
        fill = cv2.contourArea(c) / float(cw * ch)
        if fill < 0.4:
            continue
        if best is None or fill > best.score:
            best = Gap(float(x), float(y), x, y, cw, ch, float(fill), 'edge_only')
    return best


def _decide(cands):
    """集成决策：过门槛者按 3px 互证聚类，簇分 = 最高分 + 0.05×(簇员数-1)。"""
    passing = [g for g in cands if g.score >= GATES[g.method]]
    if not passing:
        return max(cands, key=lambda g: g.score)
    if len(passing) == 1:
        return passing[0]

    clusters = []
    for g in passing:
        for cl in clusters:
            if all(abs(g.x - m.x) <= 3 for m in cl):
                cl.append(g)
                break
        else:
            clusters.append([g])

    def cluster_key(cl):
        top = max(g.score for g in cl)
        prio = -min(METHOD_ORDER.index(g.method) for g in cl)
        return (top + 0.05 * (len(cl) - 1), prio)

    best_cl = max(clusters, key=cluster_key)
    return min(best_cl, key=lambda g: METHOD_ORDER.index(g.method))


def find_gap(bg, piece=None, refine=True, ensemble=True) -> Gap:
    """定位缺口。bg 为 BGR/BGRA ndarray，piece 为 BGRA 拼图块（可选）。

    refine=False, ensemble=False 复现 v1 行为（二值 Otsu 单方法、整数像素），
    用于 A/B 基准；正式使用保持默认即可。
    """
    _require_cv2()
    bg_bgr = _to_bgr(bg)
    bh, bw = bg_bgr.shape[:2]
    cands = []
    ox = oy = 0
    if piece is not None:
        tpl, ox, oy = _crop_to_alpha(piece)
        th, tw = tpl.shape[:2]
        if not (0 < tw < bw and 0 < th < bh):
            raise ValueError('拼图块尺寸不合法或与背景图比例失配')
        if ensemble:      # 集成模式：暗度图主力 + 各互证候选，交给 _decide 裁决
            builders = (_detect_dark_shape, _detect_dark_shape_bin, _detect_dark_shape_ad,
                        _detect_outline, _detect_edge_template, _detect_color_sqdiff)
        else:             # v1 行为：四个方法按优先级取第一个过门槛的
            builders = (_detect_dark_shape_bin, _detect_outline, _detect_edge_template,
                        _detect_color_sqdiff)
        for fn in builders:
            g = fn(bg_bgr, tpl, refine)
            if g is not None:
                cands.append(g)
    else:
        g = _detect_edge_only(bg_bgr)
        if g is not None:
            cands.append(g)
    if not cands:
        raise ValueError('无法定位缺口：请检查输入图像')

    if ensemble:
        best = _decide(cands)
    else:
        best = None
        for g in cands:   # v1 行为：按优先级取第一个过门槛的
            if g.score >= GATES[g.method]:
                best = g
                break
        if best is None:
            best = max(cands, key=lambda g: g.score)

    # "内容包围盒"坐标 -> "拼图块元素原点"坐标
    return Gap(best.x - ox, best.y - oy, best.bbox_x, best.bbox_y, best.w, best.h,
               best.score, best.method)


def debug_annotate(bg, gap: Gap, out_path='gap_debug.png') -> str:
    """在背景图上标注定位结果并保存，返回保存路径。"""
    _require_cv2()
    vis = _to_bgr(bg).copy()
    cv2.rectangle(vis, (gap.bbox_x, gap.bbox_y),
                  (gap.bbox_x + gap.w, gap.bbox_y + gap.h), (0, 0, 255), 2)
    cv2.imwrite(out_path, vis)
    return out_path


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description='滑块验证码缺口定位')
    ap.add_argument('bg', help='背景大图路径')
    ap.add_argument('piece', nargs='?', help='拼图块小图路径（RGBA 更佳）')
    ap.add_argument('--debug', action='store_true', help='输出标注图 gap_debug.png')
    ap.add_argument('--no-refine', action='store_true', help='关闭亚像素细化')
    ap.add_argument('--no-ensemble', action='store_true', help='关闭集成决策（v1 行为）')
    a = ap.parse_args()
    _require_cv2()
    bg = cv2.imread(a.bg, cv2.IMREAD_UNCHANGED)
    if bg is None:
        raise SystemExit(f'读取失败: {a.bg}')
    piece = None
    if a.piece:
        piece = cv2.imread(a.piece, cv2.IMREAD_UNCHANGED)
        if piece is None:
            raise SystemExit(f'读取失败: {a.piece}')
    g = find_gap(bg, piece, refine=not a.no_refine, ensemble=not a.no_ensemble)
    print(f'目标位置: x={g.x:.1f}, y={g.y:.1f}  (score={g.score:.3f}, method={g.method})')
    if a.debug:
        debug_annotate(bg, g)
        print('标注图已保存: gap_debug.png')


if __name__ == '__main__':
    main()
