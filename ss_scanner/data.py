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


def load_many(insts: list[Instrument], period: str = "2y", batch: int = 80, retries: int = 2,
              use_cache: bool = False, refresh_stale: bool = True) -> tuple[dict[int, pd.DataFrame], dict[int, str]]:
    """批量下载 Yahoo 日线（自选股几百只时比逐只快很多）。

    返回 ({列表下标: DataFrame}, {列表下标: 错误信息})。
    - 按市场分批（yf.download 混合不同时区时会丢掉部分最新 K 线）；
    - 批量失败/缺失的标的逐只回退到 load_history；
    - refresh_stale：批量结果的最新 K 线早于该市场最近收盘日时，再逐只用 Ticker.history 复查一次
      （批量接口偶尔返回滞后数据；真正休市的市场复查后仍然是旧日期）。
    """
    import time

    import yfinance as yf

    # 不在（可能公开的）CI 日志里打印具体代码
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    out: dict[int, pd.DataFrame] = {}
    errs: dict[int, str] = {}
    todo = list(range(len(insts)))
    for attempt in range(retries + 1):
        missing = []
        groups: dict[str, list[int]] = {}
        for j in todo:
            groups.setdefault(insts[j].market, []).append(j)
        chunks = [g[k:k + batch] for g in groups.values() for k in range(0, len(g), batch)]
        for chunk in chunks:
            syms = [insts[j].candidates[0] for j in chunk]
            try:
                raw = yf.download(syms, period=period, auto_adjust=True, group_by="ticker", threads=True,
                                  progress=False, timeout=30)
            except Exception as e:  # 整批失败
                log.warning("批量下载失败（%d 只）：%s", len(syms), type(e).__name__)
                missing += chunk
                continue
            for j, sym in zip(chunk, syms):
                try:
                    df = raw[sym] if isinstance(raw.columns, pd.MultiIndex) else raw
                    df = _clean(df)
                except Exception:
                    df = None
                if df is not None and len(df):
                    insts[j].yahoo = sym
                    out[j] = df
                else:
                    missing.append(j)
        todo = missing
        if not todo:
            break
        if attempt < retries:
            time.sleep(3)
    # 逐只回退（含 KR 的 .KS→.KQ 等候选）
    for j in todo:
        try:
            out[j] = load_history(insts[j], period=period, use_cache=use_cache)
        except Exception as e:
            errs[j] = f"{insts[j].raw}: {e}"
    if refresh_stale:
        from .markets import expected_session
        exp = {m: expected_session(m) for m in {i.market for i in insts}}
        for j, df in list(out.items()):
            if df.index[-1].date() < exp[insts[j].market]:
                try:
                    df2 = _clean(_yahoo(insts[j].yahoo or insts[j].candidates[0], period, None, None))
                    if len(df2) and df2.index[-1] > df.index[-1]:
                        out[j] = df2
                except Exception:
                    pass
    return out, errs
