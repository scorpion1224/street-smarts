"""信号生成、方向空间转换、日线级成交模拟（回测 & 近期触发跟踪）。"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import build_indicators
from .markets import Instrument, price_decimals, price_limit_pct, tick_size
from .strategies import PARAMS, STRATEGY_MAP, Ctx, Signal

WARMUP = 110  # 100日HV 等指标的预热长度


# ------------------------------------------------------------------ 方向空间
def build_frame(df: pd.DataFrame, side: str) -> dict:
    if side == "long":
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    else:  # 镜像
        o, h, l, c = -df["open"], -df["low"], -df["high"], -df["close"]
    F = {k: np.asarray(v, dtype=float) for k, v in build_indicators(o, h, l, c, df["close"]).items()}
    F["dates"] = df.index
    F["n"] = len(df)
    F["prev_close_abs"] = np.asarray(df["close"].shift(1), dtype=float)
    F["close_abs"] = np.asarray(df["close"], dtype=float)
    F["high_abs"] = np.asarray(df["high"], dtype=float)
    F["low_abs"] = np.asarray(df["low"], dtype=float)
    return F


def make_ctx(inst: Instrument, params: dict | None = None) -> Ctx:
    P = dict(PARAMS)
    if params:
        P.update(params)
    return Ctx(lambda x: tick_size(inst.market, abs(x), inst.code), P)


def sides_for(inst: Instrument, long_only: bool, allow_short: bool) -> list[str]:
    if long_only:
        return ["long"]
    if inst.mkt.short_default or allow_short:
        return ["long", "short"]
    return ["long"]


def generate(F: dict, i: int, strategy_ids: list[str], side: str, cx: Ctx) -> list[Signal]:
    out = []
    for sid in strategy_ids:
        try:
            sigs = STRATEGY_MAP[sid].fn(F, i, cx)
        except (IndexError, ValueError):
            sigs = []
        for s in sigs:
            s.side = side
            out.append(s)
    return out


def real(x, side):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return float(x) if side == "long" else -float(x)


# ------------------------------------------------------------------ 成交模拟
@dataclass
class Trade:
    strategy: str
    side: str
    setup_i: int
    fill_i: int
    fill: float
    stop: float
    exit_i: int | None = None
    exit: float | None = None
    reason: str = ""
    is_open: bool = False

    def ret(self, cost_bps: float = 0.0) -> float | None:
        if self.exit is None:
            return None
        return (self.exit - self.fill) / abs(self.fill) - 2 * cost_bps / 1e4

    def r_mult(self) -> float | None:
        risk = self.fill - self.stop
        if self.exit is None or risk <= 0:
            return None
        return (self.exit - self.fill) / risk


def _locked(F, j, limit):
    """A股/韩股一字涨跌停：最高=最低 且 涨跌幅≈限制 → 无法成交。"""
    if limit is None:
        return False
    pc = F["prev_close_abs"][j]
    if not (pc and pc > 0):
        return False
    return F["high_abs"][j] == F["low_abs"][j] and abs(F["close_abs"][j] / pc - 1) >= limit - 0.005


def simulate(F: dict, sig: Signal, inst: Instrument, cx: Ctx, last: int | None = None) -> Trade | None:
    """在方向空间内按日线模拟一笔交易（做空已被镜像为做多）。

    假设（保守/简化）：
    - 止损入场若开盘跳过触发价，以开盘价成交；
    - trade_below/gap_below 条件单：满足条件且回到触发价即按触发价成交；
    - 固定止损的突破单，入场当天若同时触及止损，按止损出场（最坏情形）；
    - fill_bar_low 止损在入场当天不可能触发（当日低点在入场之前形成）；
    - T+1 市场入场当天不能卖出；一字涨跌停K线既不能成交也不能出场。
    """
    if not sig.backtestable:
        return None
    n = F["n"] if last is None else last + 1
    o, h, l, c = F["o"], F["h"], F["l"], F["c"]
    limit = price_limit_pct(inst.market, inst.code)
    t1 = inst.mkt.t_plus_one

    fill_i, fill = None, None
    if sig.entry_type == "moc":
        if not _locked(F, sig.bar, limit):
            fill_i, fill = sig.bar, float(c[sig.bar])
    else:
        E = sig.entry
        for j in range(sig.bar + 1, min(n, sig.bar + 1 + sig.valid_days)):
            if _locked(F, j, limit):
                continue
            if sig.precondition == "gap_below":
                if o[j] < sig.precond_level and h[j] >= E:
                    fill_i, fill = j, E
                break
            if sig.precondition == "trade_below":
                if l[j] <= sig.precond_level and h[j] >= E:
                    fill_i, fill = j, E
                break
            if o[j] >= E:
                fill_i, fill = j, float(o[j])
                break
            if h[j] >= E:
                fill_i, fill = j, E
                break
    if fill_i is None:
        return None

    tk = cx.tick(l[fill_i])
    if sig.stop_rule == "fill_bar_low":
        stop = cx.dn(l[fill_i] - tk)
    elif sig.stop_rule == "min_setup_fill_low":
        stop = cx.dn(min(l[sig.bar], l[fill_i]) - tk)
    else:
        stop = sig.stop
    if stop is None or stop >= fill:
        stop = cx.dn(fill - tk)
    tr = Trade(sig.strategy, sig.side, sig.bar, fill_i, fill, stop)

    def done(j, px, why):
        tr.exit_i, tr.exit, tr.reason = j, float(px), why
        return tr

    # 入场当天
    if not t1:
        if sig.entry_type == "stop" and sig.stop_rule == "fixed" and l[fill_i] <= stop:
            return done(fill_i, stop, "入场当日止损")
        if sig.max_hold == 0:
            if sig.target is not None and h[fill_i] >= sig.target:
                return done(fill_i, sig.target, "目标")
            return done(fill_i, c[fill_i], "日内收盘平仓")
    for j in range(fill_i + 1, n):
        if _locked(F, j, limit):
            continue
        held = j - fill_i
        if sig.max_hold == 0:          # T+1 下的日内策略：次日开盘平仓
            return done(j, o[j], "T+1 次日开盘平仓")
        if sig.exit_next_open_if_below_entry and held == 1 and o[j] < fill:
            return done(j, o[j], "次日低开出场")
        if o[j] <= stop:
            return done(j, o[j], "跳空止损")
        if l[j] <= stop:
            return done(j, stop, "止损")
        if sig.target is not None and h[j] >= sig.target:
            return done(j, max(sig.target, o[j]), "目标")
        if sig.unprofitable_exit_days and held == sig.unprofitable_exit_days and c[j] <= fill:
            return done(j, c[j], f"{held}天未盈利出场")
        if held >= sig.max_hold:
            return done(j, c[j], "到期出场")
    tr.is_open = True
    return tr


# ------------------------------------------------------------------ 扫描
def _fmt(inst, x):
    if x is None:
        return None
    d = price_decimals(inst.market, x, inst.code)
    return int(round(x)) if d == 0 else round(x, d)


WORDS = {
    "long": dict(act="买入", cross="跌破", ext="最低价", off="下方", gap="低于", stop_ord="买入止损单"),
    "short": dict(act="卖空", cross="涨破", ext="最高价", off="上方", gap="高于", stop_ord="卖出止损单"),
}


def describe(sig: Signal, inst: Instrument) -> str:
    w = WORDS[sig.side]
    e = _fmt(inst, real(sig.entry, sig.side))
    st = _fmt(inst, real(sig.stop, sig.side))
    if sig.entry_type == "manual":  # Momentum Pinball
        v = sig.extra.get("lbr_rsi")
        if v is not None and sig.side == "short":
            v = round(100 - v, 1)
        hi, lo = ("高点", "低点") if sig.side == "long" else ("低点", "高点")
        beyond = "上方" if sig.side == "long" else "下方"
        return (f"LBR/RSI={v}；明日第一小时{hi}{beyond}1 tick 挂"
                f"{w['stop_ord']}，止损=第一小时{lo}；盈利可持有过夜，第3天收盘前出场")
    if sig.entry_type == "moc":
        s = f"今日收盘价附近{w['act']}（MOC）≈{e}；止损 {st}"
        if "next_pivot" in sig.extra:
            s += f"；明日枢轴 {_fmt(inst, real(sig.extra['next_pivot'], sig.side))}"
        return s
    if sig.stop_rule == "fill_bar_low":
        stop_txt = f"止损=入场当日{w['ext']}{w['off']}1 tick"
    elif sig.stop_rule == "min_setup_fill_low":
        stop_txt = f"止损=设置日/入场日较极端{w['ext']}{w['off']}1 tick（约 {_fmt(inst, real(sig_stop_hint(sig), sig.side))}）"
    else:
        stop_txt = f"止损 {st}"
    days = f"（有效 {sig.valid_days} 个交易日）" if sig.valid_days > 1 else "（仅下一交易日有效）"
    lvl = _fmt(inst, real(sig.precond_level, sig.side))
    if sig.precondition == "trade_below":
        return f"若下一交易日价格{w['cross']} {lvl}，再在 {e} 挂{w['stop_ord']}{days}；{stop_txt}"
    if sig.precondition == "gap_below":
        return f"若下一交易日开盘跳空{w['gap']} {lvl}，在 {e} 挂{w['stop_ord']}；{stop_txt}"
    s = f"{e} 挂{w['stop_ord']}{days}；{stop_txt}"
    if sig.target is not None:
        s += f"；目标 {_fmt(inst, real(sig.target, sig.side))}"
    return s


def sig_stop_hint(sig: Signal):
    return sig.extra.get("stop_hint")


def scan_instrument(df: pd.DataFrame, inst: Instrument, strategy_ids: list[str], sides: list[str],
                    params: dict | None = None, recent: int = 5, watch_atr: float | None = None,
                    min_bars: int | None = None):
    """返回 (新信号列表, 近期触发列表)。min_bars 可低于 WARMUP（新股），依赖长周期指标的策略会自动不触发。"""
    cx = make_ctx(inst, params)
    watch_atr = cx.P["watch_atr"] if watch_atr is None else watch_atr
    new_rows, recent_rows = [], []
    warm = WARMUP if min_bars is None else min(WARMUP, min_bars)
    if len(df) < warm:
        return new_rows, recent_rows
    i = len(df) - 1
    date = df.index[i]
    for side in sides:
        F = build_frame(df, side)
        for sig in generate(F, i, strategy_ids, side, cx):
            if sig.kind == "conditional" and sig.proximity is not None and sig.proximity > watch_atr:
                continue
            if sig.stop_rule == "min_setup_fill_low":
                sig.extra["stop_hint"] = cx.dn(F["l"][i] - cx.tick(F["l"][i]))
            meta = STRATEGY_MAP[sig.strategy]
            e, st = real(sig.entry, side), real(sig.stop, side)
            risk = None
            ref_stop = st if st is not None else real(sig.extra.get("stop_hint"), side)
            if e is not None and ref_stop is not None and e:
                risk = abs(e - ref_stop) / abs(e) * 100
            new_rows.append({
                "市场": inst.mkt.name, "代码": inst.display, "日期": date.strftime("%Y-%m-%d"),
                "策略": meta.name, "章节": meta.chapter, "方向": "做多" if side == "long" else "做空",
                "类型": {"setup": "挂单", "conditional": "条件单/预警", "moc": "收盘入场", "manual": "需盘中数据"}[sig.kind],
                "入场价": _fmt(inst, e), "止损": _fmt(inst, st), "风险%": None if risk is None else round(risk, 2),
                "收盘价": _fmt(inst, float(df["close"].iloc[i])),
                "操作": describe(sig, inst), "备注": sig.note,
                "做空限制": inst.mkt.short_note if side == "short" else "",
                "strategy_id": sig.strategy,
            })
        # 近期触发：过去 recent 根K线产生的信号，用截至今天的数据模拟
        for b in range(max(warm, i - recent), i):
            for sig in generate(F, b, strategy_ids, side, cx):
                tr = simulate(F, sig, inst, cx, last=i)
                if tr is None:
                    continue
                meta = STRATEGY_MAP[sig.strategy]
                cur = real(float(F["c"][i]), side)
                fill = real(tr.fill, side)
                pnl = (tr.exit if tr.exit is not None else F["c"][i]) - tr.fill
                recent_rows.append({
                    "市场": inst.mkt.name, "代码": inst.display, "策略": meta.name,
                    "方向": "做多" if side == "long" else "做空",
                    "设置日": df.index[b].strftime("%Y-%m-%d"), "入场日": df.index[tr.fill_i].strftime("%Y-%m-%d"),
                    "入场价": _fmt(inst, fill), "止损": _fmt(inst, real(tr.stop, side)),
                    "状态": "持有中" if tr.is_open else f"已出场({tr.reason})",
                    "出场价": _fmt(inst, real(tr.exit, side)) if tr.exit is not None else None,
                    "最新价": _fmt(inst, cur), "浮盈/盈亏%": round(pnl / abs(tr.fill) * 100, 2),
                })
    recent_rows.sort(key=lambda r: (r["状态"] != "持有中", r["市场"], r["代码"], r["入场日"]))
    return new_rows, recent_rows


# ------------------------------------------------------------------ 回测
def backtest_instrument(df: pd.DataFrame, inst: Instrument, strategy_ids: list[str], sides: list[str],
                        params: dict | None = None, start: pd.Timestamp | None = None,
                        no_overlap: bool = True) -> list[dict]:
    cx = make_ctx(inst, params)
    rows = []
    if len(df) < WARMUP + 5:
        return rows
    first = WARMUP
    if start is not None:
        first = max(first, int(df.index.searchsorted(start)))
    for side in sides:
        F = build_frame(df, side)
        busy_until: dict[str, int] = {}
        for i in range(first, len(df) - 1):
            for sig in generate(F, i, strategy_ids, side, cx):
                if no_overlap and busy_until.get(sig.strategy, -1) >= i:
                    continue
                tr = simulate(F, sig, inst, cx)
                if tr is None or tr.exit is None:
                    continue
                busy_until[sig.strategy] = tr.exit_i
                rows.append({
                    "market": inst.market, "symbol": inst.display, "strategy": sig.strategy, "side": side,
                    "setup_date": df.index[i].date(), "fill_date": df.index[tr.fill_i].date(),
                    "exit_date": df.index[tr.exit_i].date(),
                    "fill": real(tr.fill, side), "stop": real(tr.stop, side), "exit": real(tr.exit, side),
                    "reason": tr.reason, "bars": tr.exit_i - tr.fill_i,
                    "ret_gross": tr.ret(0.0), "r_mult": tr.r_mult(),
                })
    return rows


def summarize(trades: pd.DataFrame, cost_bps: float, by: list[str]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    t = trades.copy()
    t["ret"] = t["ret_gross"] - 2 * cost_bps / 1e4

    def agg(g):
        wins, losses = g["ret"][g["ret"] > 0], g["ret"][g["ret"] <= 0]
        pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else np.inf
        return pd.Series({
            "交易数": len(g), "胜率%": round(len(wins) / len(g) * 100, 1),
            "平均收益%": round(g["ret"].mean() * 100, 3), "收益中位数%": round(g["ret"].median() * 100, 3),
            "盈亏比(PF)": round(pf, 2), "平均R": round(g["r_mult"].mean(), 3),
            "平均持有K线": round(g["bars"].mean(), 1), "累计收益%(单利)": round(g["ret"].sum() * 100, 1),
        })

    return t.groupby(by).apply(agg, include_groups=False).reset_index()
