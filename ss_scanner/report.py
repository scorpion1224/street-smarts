"""报告输出：控制台、CSV（Excel 友好）、Markdown。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from .markets import MARKETS

DISCLAIMER = ("> ⚠️ 仅用于学习与研究，不构成投资建议。书中规则源自 1990 年代期货/美股，"
              "参数需在当前市场重新验证；回测为日线级近似，未计入滑点冲击、停牌、融券成本等。")


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无_\n"
    def f(x):
        if isinstance(x, float):
            if x != x:
                return ""
            return f"{x:.6f}".rstrip("0").rstrip(".")
        return "" if x is None else str(x)
    d = df.map(f)
    d = d.apply(lambda col: col.str.replace("|", "\\|", regex=False))
    head = "| " + " | ".join(d.columns) + " |\n| " + " | ".join("---" for _ in d.columns) + " |\n"
    return head + "".join("| " + " | ".join(r) + " |\n" for r in d.values)


def write_scan_report(new: pd.DataFrame, recent: pd.DataFrame, errors: list[str], notes: list[str],
                      out_dir: str, strategies: list[str]) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    paths = {}
    if not new.empty:
        paths["signals_csv"] = out / f"signals_{stamp}.csv"
        new.drop(columns=["strategy_id"], errors="ignore").to_csv(paths["signals_csv"], index=False, encoding="utf-8-sig")
    if not recent.empty:
        paths["recent_csv"] = out / f"recent_{stamp}.csv"
        recent.to_csv(paths["recent_csv"], index=False, encoding="utf-8-sig")

    cols = ["市场", "代码", "日期", "策略", "方向", "类型", "收盘价", "入场价", "止损", "风险%", "操作"]
    md = [f"# Street Smarts 信号扫描报告 · {datetime.now():%Y-%m-%d %H:%M}\n",
          f"启用策略：{', '.join(strategies)}\n"]
    if notes:
        md.append("**数据提示**\n\n" + "".join(f"- {n}\n" for n in notes))
    for kind, title in [("挂单", "## 1. 下一交易日挂单（设置已完成）"),
                        ("收盘入场", "## 2. 收盘入场信号（需在收盘前确认）"),
                        ("需盘中数据", "## 3. 需盘中数据确认的设置"),
                        ("条件单/预警", "## 4. 条件单 / 预警（满足前提才挂单）")]:
        part = new[new["类型"] == kind] if not new.empty else new
        md.append(f"\n{title}\n\n" + _md_table(part[cols] if not part.empty else part))
    md.append("\n## 5. 近期已触发的信号（持仓管理）\n\n" + _md_table(recent))
    md.append("\n## 6. 各市场做空限制\n\n" + "".join(f"- **{m.name}**：{m.short_note}\n" for m in MARKETS.values()))
    if errors:
        md.append("\n## 数据错误\n\n" + "".join(f"- {e}\n" for e in errors))
    md.append("\n" + DISCLAIMER + "\n")
    paths["report_md"] = out / f"report_{stamp}.md"
    paths["report_md"].write_text("".join(md), encoding="utf-8")
    return paths


def write_backtest_report(trades: pd.DataFrame, summary: pd.DataFrame, by_market: pd.DataFrame,
                          out_dir: str, cost_bps: float, errors: list[str]) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    paths = {"trades_csv": out / f"bt_trades_{stamp}.csv", "summary_csv": out / f"bt_summary_{stamp}.csv",
             "report_md": out / f"bt_report_{stamp}.md"}
    trades.to_csv(paths["trades_csv"], index=False, encoding="utf-8-sig")
    summary.to_csv(paths["summary_csv"], index=False, encoding="utf-8-sig")
    md = [f"# Street Smarts 策略回测 · {datetime.now():%Y-%m-%d %H:%M}\n\n",
          f"单边成本 {cost_bps} bp；信号级统计（同一标的同一策略不重叠持仓），收益为每笔交易的百分比收益。\n\n",
          "## 按策略 × 方向\n\n", _md_table(summary),
          "\n## 按市场 × 策略\n\n", _md_table(by_market)]
    if errors:
        md.append("\n## 数据错误\n\n" + "".join(f"- {e}\n" for e in errors))
    md.append("\n" + DISCLAIMER + "\n")
    paths["report_md"].write_text("".join(md), encoding="utf-8")
    return paths
