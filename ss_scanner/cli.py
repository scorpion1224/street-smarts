"""命令行入口：

  python -m ss_scanner list                         # 列出策略
  python -m ss_scanner scan AAPL 0700.HK 600519 005930.KS
  python -m ss_scanner scan -w config/watchlist.yaml
  python -m ss_scanner backtest AAPL 600519 --period 10y
"""
from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import yaml

from .data import load_history
from .engine import backtest_instrument, scan_instrument, sides_for, summarize
from .markets import Instrument, last_bar_incomplete, normalize_symbol
from .report import write_backtest_report, write_scan_report
from .strategies import PARAMS, STRATEGIES, STRATEGY_MAP


def load_watchlist(path: str) -> list[str]:
    cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
    out = []
    for mkt, syms in cfg.items():
        for s in syms or []:
            s = str(s).strip()
            if ":" in s or "." in s or mkt.upper() == "US":
                out.append(s if ":" in s or "." in s else f"US:{s}")
            else:
                out.append(f"{mkt.upper()}:{s}")
    return out


def resolve_strategies(arg: str | None, all_: bool) -> list[str]:
    if arg:
        ids = [x.strip() for x in arg.split(",") if x.strip()]
        bad = [x for x in ids if x not in STRATEGY_MAP]
        if bad:
            sys.exit(f"未知策略: {bad}，可用: {list(STRATEGY_MAP)}")
        return ids
    return [s.id for s in STRATEGIES if all_ or s.default]


def load_params(path: str | None) -> dict:
    if not path:
        return {}
    p = yaml.safe_load(open(path, encoding="utf-8")) or {}
    unknown = set(p) - set(PARAMS)
    if unknown:
        print(f"[警告] 忽略未知参数: {sorted(unknown)}")
    return {k: v for k, v in p.items() if k in PARAMS}


def _instruments(args) -> list[Instrument]:
    syms = list(args.symbols or [])
    if args.watchlist:
        syms += load_watchlist(args.watchlist)
    if not syms:
        sys.exit("请提供代码或 -w watchlist.yaml")
    seen, out = set(), []
    for s in syms:
        inst = normalize_symbol(s)
        key = (inst.market, inst.code)
        if key not in seen:
            seen.add(key)
            out.append(inst)
    return out


def _fetch_all(insts, args, period):
    def job(inst):
        try:
            return inst, load_history(inst, period=period, start=getattr(args, "start", None),
                                      end=getattr(args, "end", None), source=args.source, csv_dir=args.csv_dir,
                                      use_cache=not args.no_cache), None
        except Exception as e:
            return inst, None, f"{inst.raw}: {e}"
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        return list(ex.map(job, insts))


def cmd_list(_args):
    print(f"{'ID':<22}{'章节':<6}{'类别':<12}{'默认':<6}名称 / 说明")
    for s in STRATEGIES:
        print(f"{s.id:<22}{s.chapter:<7}{s.category:<12}{'是' if s.default else '否':<6}{s.name}  {s.note}")


def cmd_scan(args):
    insts = _instruments(args)
    strategies = resolve_strategies(args.strategies, args.all_strategies)
    params = load_params(args.params)
    new_all, recent_all, errors, notes = [], [], [], []
    for inst, df, err in _fetch_all(insts, args, args.period):
        if err:
            errors.append(err)
            continue
        if args.asof:
            df = df[df.index <= pd.Timestamp(args.asof)]
        if len(df) and not args.asof and last_bar_incomplete(inst.market, df.index[-1]):
            if args.drop_incomplete:
                df = df.iloc[:-1]
                notes.append(f"{inst.display}: 已剔除未收盘K线")
            else:
                notes.append(f"{inst.display}: 最后一根K线尚未收盘，信号为盘中临时结果")
        if len(df) < 120:
            errors.append(f"{inst.display}: 数据不足（{len(df)} 根K线，至少需 120）")
            continue
        new, recent = scan_instrument(df, inst, strategies, sides_for(inst, args.long_only, args.allow_short),
                                      params, recent=args.recent, watch_atr=args.watch_atr)
        new_all += new
        recent_all += recent
    new_df, recent_df = pd.DataFrame(new_all), pd.DataFrame(recent_all)

    print(f"\n扫描 {len(insts)} 个标的 · 策略 {len(strategies)} 个 · 新信号 {len(new_df)} 条 · 近期触发 {len(recent_df)} 条")
    if not new_df.empty:
        show = new_df[["市场", "代码", "策略", "方向", "类型", "入场价", "止损", "风险%"]]
        print(show.to_string(index=False))
    if errors:
        print("\n数据错误：\n  " + "\n  ".join(errors))
    for n in notes:
        print("提示:", n)
    paths = write_scan_report(new_df, recent_df, errors, notes, args.out, strategies)
    print("\n输出文件：")
    for k, p in paths.items():
        print(f"  {k}: {p}")


def cmd_backtest(args):
    insts = _instruments(args)
    strategies = resolve_strategies(args.strategies, args.all_strategies)
    params = load_params(args.params)
    rows, errors = [], []
    for inst, df, err in _fetch_all(insts, args, args.period):
        if err:
            errors.append(err)
            continue
        rows += backtest_instrument(df, inst, strategies, sides_for(inst, args.long_only, args.allow_short), params,
                                    start=pd.Timestamp(args.start) if args.start else None)
        print(f"  {inst.display}: {len(df)} 根K线，累计交易 {len(rows)}")
    trades = pd.DataFrame(rows)
    if trades.empty:
        print("无交易。", "\n".join(errors))
        return
    summary = summarize(trades, args.cost_bps, ["strategy", "side"])
    by_mkt = summarize(trades, args.cost_bps, ["market", "strategy"])
    pd.set_option("display.width", 200)
    print("\n按策略 × 方向：\n", summary.to_string(index=False))
    paths = write_backtest_report(trades, summary, by_mkt, args.out, args.cost_bps, errors)
    print("\n输出文件：")
    for k, p in paths.items():
        print(f"  {k}: {p}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ss_scanner", description="《Street Smarts》中美港韩股票短线信号扫描器")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出全部策略").set_defaults(func=cmd_list)

    def common(p):
        p.add_argument("symbols", nargs="*", help="代码，如 AAPL 0700.HK 600519 005930.KS KR:247540")
        p.add_argument("-w", "--watchlist", help="YAML 自选股文件")
        p.add_argument("-s", "--strategies", help="逗号分隔的策略 ID（默认启用全部默认策略）")
        p.add_argument("--all-strategies", action="store_true", help="包含默认关闭的策略（如 roc2）")
        p.add_argument("--long-only", action="store_true", help="只做多")
        p.add_argument("--allow-short", action="store_true", help="A股也输出做空信号（需融券）")
        p.add_argument("--source", default="yahoo", choices=["yahoo", "akshare", "csv"], help="数据源")
        p.add_argument("--csv-dir", help="--source csv 时的目录（文件名为 代码.csv）")
        p.add_argument("--params", help="覆盖策略参数的 YAML 文件")
        p.add_argument("--out", default="reports", help="输出目录")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--no-cache", action="store_true")
        p.add_argument("-v", "--verbose", action="store_true")

    sp = sub.add_parser("scan", help="扫描最新交易日的信号")
    common(sp)
    sp.add_argument("--period", default="2y")
    sp.add_argument("--asof", help="以某个历史日期为'今天'扫描（YYYY-MM-DD），用于复盘")
    sp.add_argument("--recent", type=int, default=5, help="跟踪最近 N 根K线已触发的信号")
    sp.add_argument("--watch-atr", type=float, default=None, help="条件单距触发位的最大 ATR 倍数")
    sp.add_argument("--drop-incomplete", action="store_true", help="剔除未收盘的当日K线")
    sp.set_defaults(func=cmd_scan)

    bp = sub.add_parser("backtest", help="日线级历史回测")
    common(bp)
    bp.add_argument("--period", default="10y")
    bp.add_argument("--start", help="回测起始日期")
    bp.add_argument("--end", help="回测结束日期")
    bp.add_argument("--cost-bps", type=float, default=10.0, help="单边交易成本（基点）")
    bp.set_defaults(func=cmd_backtest)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
