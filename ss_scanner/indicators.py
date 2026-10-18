"""技术指标（全部可对'镜像价格'使用，从而用同一份做多逻辑生成做空信号）。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def true_range(h: pd.Series, l: pd.Series, c: pd.Series) -> pd.Series:
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def dmi(h: pd.Series, l: pd.Series, c: pd.Series, n: int):
    """Wilder DMI：返回 (ADX, +DI, -DI)。"""
    up = h.diff()
    dn = -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    atr = wilder(true_range(h, l, c), n)
    pdi = 100 * wilder(pdm, n) / atr
    mdi = 100 * wilder(mdm, n) / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return wilder(dx.fillna(0), n), pdi, mdi


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    ag = wilder(d.clip(lower=0), n)
    al = wilder((-d).clip(lower=0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(al == 0, np.where(ag == 0, 50.0, 100.0), 100 - 100 / (1 + ag / al))
    out = pd.Series(out, index=s.index)
    out[ag.isna() | al.isna()] = np.nan
    return out


def stochastic(h, l, c, k: int = 7, smooth: int = 4, d: int = 10):
    """LBR 'Anti' 使用的随机指标：%K(7) 经 4 期平滑，%D 为 %K 的 10 期均线。"""
    ll = l.rolling(k).min()
    hh = h.rolling(k).max()
    raw = 100 * (c - ll) / (hh - ll).replace(0, np.nan)
    raw = raw.where(~((hh - ll) == 0), 50.0)
    K = raw.rolling(smooth).mean()
    D = K.rolling(d).mean()
    return K, D


def hist_vol(c: pd.Series, n: int) -> pd.Series:
    """历史波动率（年化，365 天，与书中一致；比值不受年化系数影响）。"""
    r = np.log(c.abs()).diff()
    return r.rolling(n).std() * np.sqrt(365) * 100


def build_indicators(o: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series, abs_close: pd.Series) -> dict:
    tr = true_range(h, l, c)
    adx14, pdi14, mdi14 = dmi(h, l, c, 14)
    adx12, _, _ = dmi(h, l, c, 12)
    _, pdi28, mdi28 = dmi(h, l, c, 28)
    K, D = stochastic(h, l, c)
    rng = h - l
    hv6, hv100 = hist_vol(abs_close, 6), hist_vol(abs_close, 100)
    return {
        "o": o, "h": h, "l": l, "c": c,
        "rng": rng,
        "avg_rng10": rng.rolling(10).mean(),
        "atr": tr.rolling(10).mean(),
        "ema20": c.ewm(span=20, adjust=False).mean(),
        "adx14": adx14, "pdi14": pdi14, "mdi14": mdi14,
        "adx12": adx12, "pdi28": pdi28, "mdi28": mdi28,
        "K": K, "D": D,
        "lbr_rsi": rsi(c.diff(), 3),       # LBR/RSI：1日净变动的3期RSI
        "roc2": c - c.shift(2),
        "hv_ratio": hv6 / hv100,
    }
