"""书中策略的规则化实现。

所有策略只写『做多』逻辑，并在『方向空间』中运行：
做空时把价格镜像（high'=-low, low'=-high, open'=-open, close'=-close），
指标随之自然镜像（+DI/-DI 互换、%K→100-%K、RSI→100-RSI），
因此同一份代码即可得到严格对称的做空信号。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

# ------------------------------------------------------------------ 参数
PARAMS: dict = {
    # Turtle Soup (Ch4)
    "ts_lookback": 20, "ts_min_age": 4, "ts_entry_ticks": 5, "ts_entry_atr": 0.02, "ts_hold": 3,
    # Turtle Soup +1 (Ch5)
    "ts1_min_age": 3, "ts1_hold": 4,
    # 80-20's (Ch6)
    "e80_pct": 0.20, "e80_pen_ticks": 10, "e80_pen_atr": 0.05,
    # Momentum Pinball (Ch7)
    "pinball_level": 30,
    # 2-Period ROC (Ch8)
    "roc2_adx_low": 16, "roc2_adx_high": 30,
    # The Anti (Ch9)
    "anti_pullback": 8, "anti_hold": 3,
    # Holy Grail (Ch10)
    "hg_adx": 30, "hg_valid": 2, "hg_hold": 10, "hg_target_lookback": 20,
    # ADX Gapper (Ch11)
    "gapper_adx": 30, "gapper_hold": 2,
    # Whiplash (Ch12)
    "whip_hold": 3, "whip_min_gap_atr": 0.0,   # 可要求跳空幅度 >= N×ATR 以减少噪音
    # 3-Day Unfilled Gap Reversal (Ch13)
    "gap3_days": 3, "gap3_hold": 5,
    # ID/NR4 (Ch19)
    "idnr4_hold": 4, "idnr4_unprofit_days": 2,
    # Historical Volatility meets Crabel (Ch20)
    "hv_ratio": 0.5, "hv_hold": 4,
    # 扫描：条件单（预警）只在收盘价距触发位 <= N 倍 ATR 时显示
    "watch_atr": 1.0,
}


# ------------------------------------------------------------------ 数据结构
@dataclass
class Signal:
    strategy: str
    side: str                      # long / short
    bar: int                       # 设置K线索引
    kind: str                      # setup=下一交易日挂单  conditional=条件单/预警  moc=当日收盘入场  manual=需盘中数据
    entry_type: str                # stop / moc / manual
    entry: float | None = None     # 方向空间价格
    stop: float | None = None
    stop_rule: str = "fixed"       # fixed / fill_bar_low / min_setup_fill_low
    precondition: str | None = None    # trade_below / gap_below
    precond_level: float | None = None
    valid_days: int = 1
    max_hold: int = 3              # 入场K线之后最多持有的K线数；0=当日平仓
    target: float | None = None
    exit_next_open_if_below_entry: bool = False
    unprofitable_exit_days: int | None = None
    backtestable: bool = True
    proximity: float | None = None  # 距触发位的 ATR 倍数（用于过滤预警）
    note: str = ""
    extra: dict = field(default_factory=dict)


class Ctx:
    """策略运行上下文：tick 计算与价格取整。"""

    def __init__(self, tick_fn: Callable[[float], float], params: dict):
        self.tick = tick_fn
        self.P = params

    def up(self, x: float) -> float:
        t = self.tick(x)
        return round(math.ceil(x / t - 1e-9) * t, 6)

    def dn(self, x: float) -> float:
        t = self.tick(x)
        return round(math.floor(x / t + 1e-9) * t, 6)


def _ok(*vals) -> bool:
    return all(v is not None and not (isinstance(v, float) and math.isnan(v)) for v in vals)


# ------------------------------------------------------------------ 策略
def turtle_soup(F, i, cx: Ctx):
    """Ch4 Turtle Soup：明日若跌破『20日低点』(该低点距明日≥4天) 后回升，在低点上方 5 tick 买入。"""
    P, n = cx.P, cx.P["ts_lookback"]
    if i < n:
        return []
    win = F["l"][i - n + 1:i + 1]
    L = float(win.min())
    k = i - n + 1 + int(np.flatnonzero(win == L)[-1])
    if (i + 1) - k < P["ts_min_age"]:
        return []
    atr = F["atr"][i]
    t = cx.tick(L)
    entry = cx.up(L + max(P["ts_entry_ticks"] * t, P["ts_entry_atr"] * atr))
    return [Signal("turtle_soup", "", i, "conditional", "stop", entry=entry, stop_rule="fill_bar_low",
                   precondition="trade_below", precond_level=cx.dn(L - t), valid_days=1, max_hold=P["ts_hold"],
                   proximity=(F["c"][i] - L) / atr if atr > 0 else None,
                   note=f"20日低点 {L:g} 形成于 {(i + 1) - k} 个交易日前；可在第1-2天按原价再入场；用移动止损")]


def turtle_soup_plus_one(F, i, cx: Ctx):
    """Ch5 Turtle Soup +1：今日创20日新低(前低≥3天前)且收盘≤前低 → 明日在前低挂买入止损。"""
    P, n = cx.P, cx.P["ts_lookback"]
    if i < n + 1:
        return []
    win = F["l"][i - n:i]
    L = float(win.min())
    k = i - n + int(np.flatnonzero(win == L)[-1])
    if not (F["l"][i] < L and i - k >= P["ts1_min_age"] and F["c"][i] <= L):
        return []
    return [Signal("turtle_soup_plus_one", "", i, "setup", "stop", entry=cx.up(L), stop_rule="min_setup_fill_low",
                   valid_days=1, max_hold=P["ts1_hold"],
                   note="未成交则收盘撤单；止损=第1/2天较低低点下1tick；2-6根K线内部分止盈，余仓移动止损")]


def eighty_twenty(F, i, cx: Ctx):
    """Ch6 80-20's：昨日开在区间上20%、收在下20% → 今日先跌破昨低再回到昨低时买入（日内）。"""
    P = cx.P
    o, h, l, c, r = F["o"][i], F["h"][i], F["l"][i], F["c"][i], F["rng"][i]
    if not (r > 0 and o >= l + (1 - P["e80_pct"]) * r and c <= l + P["e80_pct"] * r):
        return []
    t = cx.tick(l)
    pen = max(P["e80_pen_ticks"] * t, P["e80_pen_atr"] * F["atr"][i])
    big = _ok(F["avg_rng10"][i]) and r > 1.2 * F["avg_rng10"][i]
    return [Signal("eighty_twenty", "", i, "setup", "stop", entry=cx.up(l), stop_rule="fill_bar_low",
                   precondition="trade_below", precond_level=cx.dn(l - pen), valid_days=1, max_hold=0,
                   note=("设置K线区间大于均值，质量较高；" if big else "") + "日内交易，收盘平仓；盈利可移动止损")]


def momentum_pinball(F, i, cx: Ctx):
    """Ch7 Momentum Pinball：LBR/RSI(1日ROC的3期RSI) < 30 → 明日第一小时高点上方买入。需盘中数据。"""
    v = F["lbr_rsi"][i]
    if not (_ok(v) and v < cx.P["pinball_level"]):
        return []
    return [Signal("momentum_pinball", "", i, "manual", "manual", backtestable=False,
                   note="入场依赖盘中第一小时高低点", extra={"lbr_rsi": round(float(v), 1)})]


def roc2(F, i, cx: Ctx):
    """Ch8 2-Period ROC：2日ROC今日拐头向上 → 收盘买入，次日出场。不适合新手，默认关闭。"""
    P = cx.P
    r0, r1, r2, adx, adx1 = F["roc2"][i], F["roc2"][i - 1], F["roc2"][i - 2], F["adx14"][i], F["adx14"][i - 1]
    if not _ok(r0, r1, r2, adx, adx1):
        return []
    if adx < P["roc2_adx_low"] or (adx > P["roc2_adx_high"] and adx > adx1):
        return []
    if not (r0 > r1 and r1 <= r2):
        return []
    pivot = F["c"][i] - F["c"][i - 2] + F["c"][i - 1]
    return [Signal("roc2", "", i, "moc", "moc", entry=float(F["c"][i]), stop=cx.dn(F["l"][i] - cx.tick(F["l"][i])),
                   max_hold=1, extra={"next_pivot": float(pivot)},
                   note="收盘入场、次日出场；需在临近收盘时运行扫描")]


def anti(F, i, cx: Ctx):
    """Ch9 The 'Anti'：%D 向上，%K 回调后拐头向上 → 次日在今日高点上方 1 tick 买入。"""
    P = cx.P
    K, D = F["K"], F["D"]
    if i < 8 or not _ok(K[i], K[i - 1], K[i - 2], D[i], D[i - 1], D[i - 2]):
        return []
    if not (D[i] > D[i - 1] > D[i - 2] and K[i - 1] < K[i - 2] and K[i] > K[i - 1]):
        return []
    if np.nanmax(K[i - 6:i]) - K[i - 1] < P["anti_pullback"]:
        return []
    h = F["h"][i]
    sl = float(np.min(F["l"][i - 2:i + 1]))
    return [Signal("anti", "", i, "setup", "stop", entry=cx.up(h + cx.tick(h)), stop=cx.dn(sl - cx.tick(sl)),
                   valid_days=1, max_hold=P["anti_hold"], note="持有2-4根K线，出现高潮K线即了结")]


def holy_grail(F, i, cx: Ctx):
    """Ch10 Holy Grail：ADX(14)>30 且此前上升，+DI>-DI，价格首次回撤触及 20EMA → 前高上方买入。"""
    P = cx.P
    adx = F["adx14"]
    if i < 30 or not _ok(adx[i], adx[i - 10], F["ema20"][i]):
        return []
    recent = adx[i - 4:i + 1]
    if not (np.nanmax(recent) > P["hg_adx"] and adx[i] > P["hg_adx"] * 0.9 and np.nanmax(recent) > adx[i - 10]):
        return []
    if not (F["pdi14"][i] > F["mdi14"][i]):
        return []
    ema = F["ema20"]
    if not (F["l"][i] <= ema[i] and np.all(F["l"][i - 3:i] > ema[i - 3:i])):
        return []
    h = F["h"][i]
    tgt = float(np.max(F["h"][i - P["hg_target_lookback"]:i]))
    return [Signal("holy_grail", "", i, "setup", "stop", entry=cx.up(h + cx.tick(h)), stop_rule="min_setup_fill_low",
                   valid_days=P["hg_valid"], max_hold=P["hg_hold"], target=tgt if tgt > h else None,
                   note=f"ADX={adx[i]:.1f}；目标=近期波段高点；未成交可持续挂在前一根K线高点上方；"
                        "完成后需 ADX 重新>30 才做下一笔")]


def adx_gapper(F, i, cx: Ctx):
    """Ch11 ADX Gapper：ADX(12)>30 且 +DI(28)>-DI(28)，明日若跳空低开于今日低点 → 回到今日低点时买入。"""
    P = cx.P
    if not (_ok(F["adx12"][i], F["pdi28"][i], F["mdi28"][i]) and F["adx12"][i] > P["gapper_adx"]
            and F["pdi28"][i] > F["mdi28"][i]):
        return []
    l = F["l"][i]
    return [Signal("adx_gapper", "", i, "conditional", "stop", entry=cx.up(l), stop_rule="fill_bar_low",
                   precondition="gap_below", precond_level=float(l), valid_days=1, max_hold=P["gapper_hold"],
                   note=f"ADX12={F['adx12'][i]:.1f}；源自 Larry Williams 'Oops'；移动止损，收盘强可过夜")]


def whiplash(F, i, cx: Ctx):
    """Ch12 Whiplash：跳空低于昨低，收盘>开盘且位于当日区间上 50% → 收盘买入。"""
    o, h, l, c = F["o"][i], F["h"][i], F["l"][i], F["c"][i]
    gap_ok = F["l"][i - 1] - o >= cx.P["whip_min_gap_atr"] * F["atr"][i]
    if i < 1 or not (o < F["l"][i - 1] and gap_ok and c > o and c >= l + 0.5 * (h - l)):
        return []
    return [Signal("whiplash", "", i, "moc", "moc", entry=float(c), stop=cx.dn(l - cx.tick(l)),
                   max_hold=cx.P["whip_hold"], exit_next_open_if_below_entry=True,
                   note="次日若低开于入场价立即出场，否则移动止损")]


def gap_reversal_3day(F, i, cx: Ctx):
    """Ch13 三日未回补缺口反转：向下跳空当日未回补，此后3天内突破缺口日高点上方 1 tick 买入。"""
    P = cx.P
    h, l = F["h"], F["l"]
    for g in range(i, i - P["gap3_days"], -1):       # 取最近一个缺口
        if g < 1:
            break
        if h[g] < l[g - 1]:
            remaining = P["gap3_days"] - (i - g)
            trig = cx.up(h[g] + cx.tick(h[g]))
            if remaining <= 0 or (g < i and np.max(h[g + 1:i + 1]) >= trig):
                return []
            # 期间是否回补缺口（回补后不再是"未回补"）
            if g < i and np.max(h[g + 1:i + 1]) >= l[g - 1]:
                return []
            sl = float(np.min(l[g:i + 1]))
            return [Signal("gap_reversal_3day", "", i, "setup", "stop", entry=trig, stop=cx.dn(sl - cx.tick(sl)),
                           valid_days=remaining, max_hold=P["gap3_hold"],
                           note=f"缺口日 index={g}，剩余 {remaining} 天有效；书中适用于高波动动量股")]
    return []


def _inside(F, i):
    return F["h"][i] < F["h"][i - 1] and F["l"][i] > F["l"][i - 1]


def _nr4(F, i):
    return F["rng"][i] <= np.min(F["rng"][i - 3:i])


def id_nr4(F, i, cx: Ctx):
    """Ch19 ID/NR4：内包日 + 4日最窄 → 次日高点上方/低点下方双向挂止损单。"""
    if i < 4 or not (_inside(F, i) and _nr4(F, i)):
        return []
    h, l = F["h"][i], F["l"][i]
    return [Signal("id_nr4", "", i, "setup", "stop", entry=cx.up(h + cx.tick(h)), stop=cx.dn(l - cx.tick(l)),
                   valid_days=1, max_hold=cx.P["idnr4_hold"], unprofitable_exit_days=cx.P["idnr4_unprofit_days"],
                   note="双向挂单（反向单兼作止损/反手）；2天内不盈利则收盘出场")]


def hv_crabel(F, i, cx: Ctx):
    """Ch20 HV meets Crabel：6日HV/100日HV < 50% 且 内包日或NR4 → 次日双向突破单。"""
    v = F["hv_ratio"][i]
    if i < 4 or not (_ok(v) and v < cx.P["hv_ratio"] and (_inside(F, i) or _nr4(F, i))):
        return []
    h, l = F["h"][i], F["l"][i]
    return [Signal("hv_crabel", "", i, "setup", "stop", entry=cx.up(h + cx.tick(h)), stop=cx.dn(l - cx.tick(l)),
                   valid_days=1, max_hold=cx.P["hv_hold"], extra={"hv_ratio": round(float(v), 3)},
                   note=f"HV6/HV100={v:.2f}；双向挂单，反向止损仅在入场当天有效；持有1-4天")]


# ------------------------------------------------------------------ 注册表
@dataclass(frozen=True)
class StrategyMeta:
    id: str
    name: str
    chapter: str
    category: str
    fn: Callable
    default: bool = True
    note: str = ""


STRATEGIES: list[StrategyMeta] = [
    StrategyMeta("turtle_soup", "Turtle Soup 海龟汤", "Ch4", "测试/假突破", turtle_soup),
    StrategyMeta("turtle_soup_plus_one", "Turtle Soup +1", "Ch5", "测试/假突破", turtle_soup_plus_one),
    StrategyMeta("eighty_twenty", "80-20's", "Ch6", "日内反转", eighty_twenty),
    StrategyMeta("momentum_pinball", "Momentum Pinball 动量弹珠", "Ch7", "动量", momentum_pinball,
                 note="入场需盘中第一小时数据，仅提示设置，不回测"),
    StrategyMeta("roc2", "2-Period ROC", "Ch8", "节奏/收盘", roc2, default=False,
                 note="作者称不适合新手，默认关闭"),
    StrategyMeta("anti", "The \"Anti\"", "Ch9", "回撤", anti),
    StrategyMeta("holy_grail", "Holy Grail 圣杯", "Ch10", "回撤(顺势)", holy_grail),
    StrategyMeta("adx_gapper", "ADX Gapper", "Ch11", "趋势+跳空", adx_gapper),
    StrategyMeta("whiplash", "Whiplash 甩鞭", "Ch12", "跳空反转", whiplash),
    StrategyMeta("gap_reversal_3day", "三日未回补缺口反转", "Ch13", "跳空反转", gap_reversal_3day),
    StrategyMeta("id_nr4", "ID/NR4", "Ch19", "波动收缩", id_nr4),
    StrategyMeta("hv_crabel", "HV meets Crabel", "Ch20", "波动收缩", hv_crabel),
]
STRATEGY_MAP = {s.id: s for s in STRATEGIES}
