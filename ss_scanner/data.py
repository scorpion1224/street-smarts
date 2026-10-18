"""行情数据：Yahoo Finance（四市场通用，默认）/ AkShare（A股可选）/ 本地 CSV。"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

from .markets import Instrument

log = logging.getLogger(__name__)
CACHE_DIR = Path(".cache")


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)[["open", "high", "low", "close"] + (["volume"] if "volume" in df.columns.str.lower() else [])]
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df["high"] > 0) & (df["low"] > 0)]
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # 修正脏数据：最高价/最低价须包住开收
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    return df


def _yahoo(sym: str, period: str, start: str | None, end: str | None) -> pd.DataFrame:
    import yfinance as yf
    t = yf.Ticker(sym)
    if start:
        df = t.history(start=start, end=end, auto_adjust=True)
    else:
        df = t.history(period=period, auto_adjust=True)
    return df


def _akshare(code: str, start: str | None, end: str | None) -> pd.DataFrame:
    import akshare as ak
    s = (start or "1990-01-01").replace("-", "")
    e = (end or date.today().isoformat()).replace("-", "")
    if code[:2] in ("51", "56", "58", "15", "16"):
        df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=s, end_date=e, adjust="qfq")
    else:
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=s, end_date=e, adjust="qfq")
    df = df.rename(columns={"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume"})
    return df.set_index("date")


def _period_to_start(period: str) -> str | None:
    if period == "max":
        return None
    if period.endswith("mo"):
        days = int(period[:-2]) * 30
    elif period.endswith("y"):
        days = int(period[:-1]) * 365
    elif period.endswith("d"):
        days = int(period[:-1])
    else:
        days = 730
    return (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def load_history(inst: Instrument, period: str = "2y", start: str | None = None, end: str | None = None,
                 source: str = "yahoo", csv_dir: str | None = None, use_cache: bool = True) -> pd.DataFrame:
    """返回按日期升序、列为 open/high/low/close(/volume) 的前复权日线。"""
    if source == "csv":
        base = Path(csv_dir or "data")
        for name in [inst.raw, inst.code, *inst.candidates]:
            f = base / f"{name}.csv"
            if f.exists():
                df = pd.read_csv(f, index_col=0, parse_dates=True)
                inst.yahoo = name
                return _clean(df)
        raise FileNotFoundError(f"未在 {base} 找到 {inst.raw} 的 CSV")

    if source == "akshare" and inst.market == "CN":
        try:
            df = _clean(_akshare(inst.code, start or _period_to_start(period), end))
            if len(df):
                inst.yahoo = inst.candidates[0]
                return df
        except Exception as e:  # 网络/接口失败则回退 Yahoo
            log.warning("AkShare 获取 %s 失败(%s)，回退 Yahoo", inst.code, e)

    last_err = None
    for sym in inst.candidates:
        cache = CACHE_DIR / f"{sym}_{start or period}_{end or ''}_{date.today().isoformat()}.csv"
        if use_cache and cache.exists():
            inst.yahoo = sym
            return _clean(pd.read_csv(cache, index_col=0, parse_dates=True))
        try:
            df = _yahoo(sym, period, start, end)
        except Exception as e:
            last_err = e
            continue
        if df is not None and len(df):
            df = _clean(df)
            if use_cache:
                CACHE_DIR.mkdir(exist_ok=True)
                df.to_csv(cache)
            inst.yahoo = sym
            return df
    raise ValueError(f"无法获取 {inst.raw} 的行情（尝试 {inst.candidates}）{': ' + str(last_err) if last_err else ''}")
