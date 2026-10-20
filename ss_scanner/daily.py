"""每日收盘后扫描自选股并邮件提醒。

  python -m ss_scanner daily --session asia            # 亚洲收盘后（A股/港股/韩股/日股/澳股）
  python -m ss_scanner daily --session us_eu --email   # 美欧收盘后，并发送邮件
  python -m ss_scanner daily --session all --preview mail.html --any-date

微信通过 Server酱 推送（--wechat）：环境变量 SERVERCHAN_KEY = SendKey（Turbo 的 SCT… 或 Server酱³ 的 sctp…）。

邮件通过 SMTP 发送，读取环境变量：
  SMTP_USER  发件邮箱（如 xxx@gmail.com）      SMTP_PASS  邮箱应用专用密码
  MAIL_TO    收件人（逗号分隔，默认 = SMTP_USER） MAIL_FROM  发件人（默认 = SMTP_USER）
  SMTP_HOST  默认 smtp.gmail.com                SMTP_PORT  默认 465（SSL）；587 走 STARTTLS
"""
from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from .data import load_many
from .engine import scan_instrument, sides_for
from .markets import INDEX_NOTE, MARKETS, expected_session
from .report import DISCLAIMER
from .strategies import STRATEGIES, STRATEGY_MAP
from .watchlist import load_instruments

SESSION_NAME = {"asia": "亚洲收盘", "us_eu": "美欧收盘", "all": "全部市场"}
KIND_ORDER = ["挂单", "收盘入场", "条件单/预警", "需盘中数据"]
KIND_HINT = {
    "挂单": "下一交易日按价格挂止损单（突破/回落触发才成交）",
    "收盘入场": "策略要求在当日收盘价附近入场（MOC）；收到邮件时已收盘，可参考次日开盘，风险自行评估",
    "条件单/预警": "需先满足条件（如先跌破某价位）再挂单，属于预警",
    "需盘中数据": "需要明日第一小时K线才能确定入场位",
}
DEFAULT_CFG = {
    "watchlist": "config/watchlist.csv", "web_url": "", "strategies": [], "long_only": False,
    "allow_short_cn": False, "groups": [], "min_bars": 60, "watch_atr": 0.5, "min_atr_pct": 0.6,
    "sessions": {"asia": ["CN", "HK", "KR", "JP", "AU"], "us_eu": ["US", "UK", "DE"]},
    "email": {"send_when": "actionable", "body_kinds": KIND_ORDER, "max_rows": 30, "max_rows_per_strategy": 12,
              "include_open_positions": True, "subject_prefix": "[Street Smarts]"},
}


EXAMPLE_WATCHLIST = "config/watchlist.example.csv"


def load_cfg(path: str | None) -> dict:
    """读取 alerts.yaml；环境变量优先：SS_WEB_URL（网页地址）、SS_WATCHLIST（自选股 CSV 路径）。"""
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if path and Path(path).exists():
        user = yaml.safe_load(open(path, encoding="utf-8")) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if os.environ.get("SS_WEB_URL"):
        cfg["web_url"] = os.environ["SS_WEB_URL"].strip()
    if os.environ.get("SS_WATCHLIST"):
        cfg["watchlist"] = os.environ["SS_WATCHLIST"].strip()
    cfg["web_url"] = (cfg.get("web_url") or "").strip()
    if cfg["web_url"] and not cfg["web_url"].endswith("/"):
        cfg["web_url"] += "/"
    if not Path(cfg["watchlist"]).exists() and Path(EXAMPLE_WATCHLIST).exists():
        print(f"[提示] 未找到 {cfg['watchlist']}（真实自选股不入库），改用示例 {EXAMPLE_WATCHLIST}")
        cfg["watchlist"] = EXAMPLE_WATCHLIST
    return cfg


def local_today(market: str):
    return datetime.now(ZoneInfo(MARKETS[market].timezone)).date()


# ------------------------------------------------------------------ 扫描
def run_scan(cfg: dict, session: str, any_date: bool = False, limit: int | None = None) -> dict:
    markets = set(sum(cfg["sessions"].values(), [])) if session == "all" else set(cfg["sessions"][session])
    insts = load_instruments(cfg["watchlist"], markets)
    if cfg.get("groups"):
        want = set(cfg["groups"])
        insts = [x for x in insts if want & set(x.groups.split(";"))]
    if limit:
        insts = insts[:limit]
    strategies = cfg.get("strategies") or [s.id for s in STRATEGIES if s.default]
    data, errs = load_many(insts, refresh_stale=not any_date)
    new_all, open_all, stale, skipped, lowvol, bar_dates = [], [], [], [], [], []
    errors = list(errs.values())
    for j, inst in enumerate(insts):
        df = data.get(j)
        if df is None or df.empty:
            continue
        if not any_date:
            exp = expected_session(inst.market)
            df = df[df.index.date <= exp]          # 去掉盘中未收盘的 K 线（任务延迟到次日开盘后时）
            if df.empty or df.index[-1].date() < exp:
                last = df.index[-1].date() if len(df) else "无"
                stale.append(f"{inst.display}（最新 {last}）")
                continue
        bar_dates.append(str(df.index[-1].date()))
        if len(df) < cfg["min_bars"]:
            skipped.append(f"{inst.display}（仅 {len(df)} 根K线）")
            continue
        atr_pct = float(((df["high"] - df["low"]) / df["close"]).tail(20).mean() * 100)
        if atr_pct < float(cfg.get("min_atr_pct", 0) or 0):
            lowvol.append(f"{inst.display}（{atr_pct:.2f}%）")
            continue
        sides = sides_for(inst, cfg["long_only"], cfg["allow_short_cn"])
        new, recent = scan_instrument(df, inst, strategies, sides, {"watch_atr": cfg["watch_atr"]},
                                      recent=5, min_bars=cfg["min_bars"])
        extra = {"名称": inst.name, "分组": inst.groups.replace(";", "、"), "指数": "是" if inst.is_index else ""}
        for r in new:
            r.update(extra)
            if inst.is_index:
                r["备注"] = (INDEX_NOTE + "；" + r["备注"]).strip("；")
            if len(df) < 120:
                r["备注"] = (r["备注"] + f"；新股仅 {len(df)} 根K线").strip("；")
        new_all += new
        open_all += [dict(r, **extra) for r in recent if r["状态"] == "持有中"]
    cols_first = ["市场", "代码", "名称", "分组", "策略", "方向", "类型", "入场价", "止损", "风险%", "距入场%", "收盘价", "操作"]
    sig = pd.DataFrame(new_all)
    if not sig.empty:
        e, c = pd.to_numeric(sig["入场价"], errors="coerce"), pd.to_numeric(sig["收盘价"], errors="coerce")
        sig["距入场%"] = ((e - c) / c * 100).round(2)
        sig["_k"] = sig["类型"].map({k: i for i, k in enumerate(KIND_ORDER)})
        sig["_d"] = sig["距入场%"].abs()
        sig = sig.sort_values(["_k", "指数", "_d"], na_position="last").drop(columns=["_k", "_d"])
        sig = sig[[c for c in cols_first if c in sig] + [c for c in sig.columns if c not in cols_first]]
    pos = pd.DataFrame(open_all)
    dates = sorted(set(bar_dates))
    return dict(session=session, markets=sorted(markets), time=datetime.now(ZoneInfo("Asia/Shanghai")),
                n_inst=len(insts), n_scanned=len(insts) - len(errors) - len(stale) - len(skipped) - len(lowvol),
                signals=sig, positions=pos, errors=errors, stale=stale, skipped=skipped, lowvol=lowvol,
                bar_date=dates[-1] if dates else "", strategies=strategies)


def save_outputs(res: dict, out_dir: str = "latest", history_dir: str | None = "history") -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    s = res["session"]
    paths = {"signals": out / f"{s}_signals.csv", "positions": out / f"{s}_positions.csv",
             "summary": out / f"{s}_summary.json"}
    sig = res["signals"].drop(columns=["strategy_id"], errors="ignore")
    sig.to_csv(paths["signals"], index=False, encoding="utf-8-sig")
    res["positions"].to_csv(paths["positions"], index=False, encoding="utf-8-sig")
    counts = sig["类型"].value_counts().to_dict() if not sig.empty else {}
    summary = {k: res[k] for k in ("session", "markets", "n_inst", "n_scanned", "bar_date", "errors", "stale",
                                   "skipped", "lowvol", "strategies")}
    summary.update(time=res["time"].strftime("%Y-%m-%d %H:%M"), counts=counts, n_signals=len(sig),
                   n_positions=len(res["positions"]))
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    if history_dir and not sig.empty and res["bar_date"]:
        h = Path(history_dir) / res["bar_date"][:7]
        h.mkdir(parents=True, exist_ok=True)
        paths["history"] = h / f"{res['bar_date']}_{s}.csv"
        sig.drop(columns=["操作", "备注", "做空限制"], errors="ignore").to_csv(paths["history"], index=False,
                                                                       encoding="utf-8-sig")
    return paths


# ------------------------------------------------------------------ 邮件
def _link(cfg, code):
    base = cfg.get("web_url") or ""
    return f'<a href="{html.escape(base)}?s={quote(str(code))}">{html.escape(str(code))}</a>' if base else html.escape(str(code))


def _table(df: pd.DataFrame, cols: list[str], cfg: dict, max_rows: int) -> str:
    cols = [c for c in cols if c in df.columns]
    th = "".join(f"<th>{c}</th>" for c in cols)
    body = []
    for _, r in df.head(max_rows).iterrows():
        tds = []
        for c in cols:
            v = r[c]
            v = "" if v is None or (isinstance(v, float) and v != v) else v
            cell = _link(cfg, v) if c == "代码" else html.escape(str(v))
            if c == "方向":
                color = {"做多": "#0a7d32", "做空": "#c62828"}.get(v, "#1565c0")
                cell = f'<b style="color:{color}">{cell}</b>'
            elif c == "操作":
                cell = f'<small>{cell}</small>'
            tds.append(f'<td nowrap>{cell}</td>' if c in ("市场", "代码", "方向") else f"<td>{cell}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    more = (f'<p style="color:#888;font-size:12px;margin:2px 0">…另有 {len(df) - max_rows} 条，见附件 CSV / 网页版「自选股」页</p>'
            if len(df) > max_rows else "")
    return (f'<table border="1" cellpadding="4" cellspacing="0" bordercolor="#e3e3e3" '
            f'style="border-collapse:collapse;font-size:13px;width:100%">'
            f'<tr bgcolor="#f3f3f3" align="left">{th}</tr>{"".join(body)}</table>{more}')


BRACKET = {"id_nr4", "hv_crabel"}


def merge_brackets(d: pd.DataFrame) -> pd.DataFrame:
    """ID/NR4、HV meets Crabel 是同一标的的双向突破单：合并为一行「双向」。"""
    if d.empty or "strategy_id" not in d:
        return d
    keep, rows = [], []
    for (code, sid), g in d.groupby(["代码", "strategy_id"], sort=False):
        if sid in BRACKET and set(g["方向"]) == {"做多", "做空"}:
            lo, sh = g[g["方向"] == "做多"].iloc[0], g[g["方向"] == "做空"].iloc[0]
            r = lo.copy()
            r["方向"] = "双向"
            r["入场价"] = f"↑{lo['入场价']} / ↓{sh['入场价']}"
            r["止损"] = f"{lo['止损']} / {sh['止损']}"
            r["操作"] = f"上破买入：{lo['操作']}｜下破卖空：{sh['操作']}"
            r["距入场%"] = min(abs(lo["距入场%"]), abs(sh["距入场%"]))
            rows.append(r)
        else:
            keep.append(g)
    out = pd.concat(keep + ([pd.DataFrame(rows)] if rows else []))
    out["_d"] = pd.to_numeric(out["距入场%"], errors="coerce").abs()
    return out.sort_values(["指数", "_d"], na_position="last").drop(columns="_d")


def should_send(res: dict, cfg: dict) -> bool:
    sig = res["signals"]
    if sig.empty:
        return False
    if cfg["email"].get("send_when") == "any":
        return True
    return bool(sig["类型"].isin(["挂单", "收盘入场"]).any())


def build_email(res: dict, cfg: dict) -> tuple[str, str, str]:
    """返回 (主题, HTML 正文, 纯文本正文)。"""
    sig, pos, ec = res["signals"], res["positions"], cfg["email"]
    counts = sig["类型"].value_counts().to_dict() if not sig.empty else {}
    name = SESSION_NAME.get(res["session"], res["session"])
    parts = [f"{k} {counts[k]}" for k in KIND_ORDER if counts.get(k)]
    subject = f"{ec.get('subject_prefix', '')} {name} {res['bar_date']}：{' · '.join(parts) or '无新信号'}".strip()
    n_today = int((pos["入场日"] == res["bar_date"]).sum()) if len(pos) else 0
    if n_today:
        subject += f" · 今日触发 {n_today}"
    mk = "、".join(MARKETS[m].name for m in res["markets"])
    h = [f'<div style="font-family:-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;max-width:980px">',
         f"<h2 style=\"margin:0 0 6px\">《Street Smarts》自选股信号 · {name} {res['bar_date']}</h2>",
         f'<p style="color:#555;margin:0 0 12px">市场：{mk} ｜ 自选 {res["n_inst"]} 只，有效扫描 {res["n_scanned"]} 只'
         f' ｜ 生成于 {res["time"]:%Y-%m-%d %H:%M}（北京时间）'
         + (f' ｜ <a href="{html.escape(cfg["web_url"])}">打开网页版</a>' if cfg.get("web_url") else "") + "</p>"]
    cols = ["代码", "名称", "策略", "方向", "入场价", "止损", "风险%", "距入场%", "收盘价", "操作", "备注"]
    max_rows = int(ec.get("max_rows", 30))
    if not sig.empty:
        st = sig.groupby(["策略", "类型"]).size().reset_index(name="条数").sort_values("条数", ascending=False)
        h.append('<p style="font-size:12px;color:#555;margin:4px 0">按策略：'
                 + "；".join(f"{r['策略']}（{r['类型']}）{r['条数']}" for _, r in st.iterrows()) + "</p>")
    per = int(ec.get("max_rows_per_strategy", 12))
    cols = ["市场", "代码", "名称", "方向", "入场价", "止损", "风险%", "距入场%", "收盘价", "操作", "备注"]
    for k in KIND_ORDER:
        if k not in ec.get("body_kinds", KIND_ORDER) or not counts.get(k):
            continue
        d = sig[sig["类型"] == k]
        h.append(f'<h3 style="margin:20px 0 2px;border-bottom:2px solid #333">{k}（{len(d)}）</h3>'
                 f'<p style="color:#777;margin:2px 0 6px;font-size:12px">{KIND_HINT[k]}</p>')
        order = d.groupby("strategy_id").size().sort_values().index   # 少见（更挑剔）的策略排前面
        for sid in order:
            g = merge_brackets(d[d["strategy_id"] == sid])
            meta = STRATEGY_MAP[sid]
            more = f"，显示距入场价最近的 {per} 条" if len(g) > per else ""
            h.append(f'<h4 style="margin:12px 0 2px">{html.escape(meta.name)}（第{meta.chapter}章 · {len(g)}{more}）</h4>')
            h.append(_table(g, cols, cfg, per))
    if sig.empty:
        h.append("<p>今日没有新信号。</p>")
    today_pos = pos[pos["入场日"] == res["bar_date"]] if len(pos) else pos
    if ec.get("include_open_positions", True) and len(today_pos):
        h.append(f'<h3 style="margin:18px 0 4px">今日已触发入场（按规则模拟，{len(today_pos)}）</h3>'
                 f'<p style="color:#777;margin:0 0 6px;font-size:12px">前几日的挂单今天被触发；近 5 日全部持有中模拟仓位'
                 f' {len(pos)} 个见附件。</p>')
        h.append(_table(today_pos, ["代码", "名称", "策略", "方向", "设置日", "入场价", "止损", "最新价", "浮盈/盈亏%"],
                        cfg, max_rows))
    notes = []
    if res["stale"]:
        notes.append(f"今日无新K线（休市/停牌/数据延迟）{len(res['stale'])} 只：" + "、".join(res["stale"][:30])
                     + ("…" if len(res["stale"]) > 30 else ""))
    if res.get("lowvol"):
        notes.append(f"波动过低（20日平均振幅 < {cfg.get('min_atr_pct')}%，如货币/短债 ETF）跳过："
                     + "、".join(res["lowvol"]))
    if res["skipped"]:
        notes.append("上市时间过短跳过：" + "、".join(res["skipped"]))
    if res["errors"]:
        notes.append("数据获取失败：" + "；".join(res["errors"][:20]))
    if notes:
        h.append('<h3 style="margin:18px 0 4px">数据说明</h3><ul style="color:#666;font-size:12px">'
                 + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul>")
    h.append(f'<p style="color:#999;font-size:12px;margin-top:18px">{html.escape(DISCLAIMER.lstrip("> "))}</p></div>')

    text = [f"{subject}\n"]
    for k in KIND_ORDER:
        d = sig[sig["类型"] == k] if not sig.empty else sig
        if len(d):
            text.append(f"\n== {k}（{len(d)}）==")
            text += [f"{r['市场']} {r['代码']} {r.get('名称', '')} | {r['策略']} {r['方向']} | {r['操作']}"
                     for _, r in d.iterrows()]
    return subject, "\n".join(h), "\n".join(text)


def send_email(subject: str, html_body: str, text_body: str, attachments: list[Path]) -> str:
    user, pwd = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", "")
    if not user or not pwd:
        raise RuntimeError("未设置 SMTP_USER / SMTP_PASS 环境变量")
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)
    to = [x.strip() for x in (os.environ.get("MAIL_TO") or user).split(",") if x.strip()]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("MAIL_FROM") or user, ", ".join(to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    for p in attachments:
        if p and Path(p).exists() and Path(p).stat().st_size > 0:
            msg.add_attachment(Path(p).read_bytes(), maintype="text", subtype="csv", filename=Path(p).name)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.send_message(msg)
    return ", ".join(to)


# ------------------------------------------------------------------ 微信（Server酱）
def build_wechat(res: dict, cfg: dict, per: int | None = None, limit: int = 20000) -> tuple[str, str, str]:
    """返回 (标题≤32字, Markdown 正文, 卡片摘要)。"""
    sig, pos = res["signals"], res["positions"]
    counts = sig["类型"].value_counts().to_dict() if not sig.empty else {}
    name = SESSION_NAME.get(res["session"], res["session"])
    short_kind = {"挂单": "挂单", "收盘入场": "收盘", "条件单/预警": "预警", "需盘中数据": "盘中"}
    parts = [f"{short_kind[k]}{counts[k]}" for k in KIND_ORDER if counts.get(k)]
    title = f"{name}{res['bar_date'][5:]} " + (" ".join(parts) or "无新信号")
    per = per or int(cfg.get("wechat", {}).get("max_rows_per_strategy", 8))
    base = cfg.get("web_url") or ""
    n_today = int((pos["入场日"] == res["bar_date"]).sum()) if len(pos) else 0

    def build(per):
        md = [f"**{res['bar_date']} {name}** · 有效扫描 {res['n_scanned']}/{res['n_inst']} 只"
              + (f" · [网页版]({base})" if base else ""), ""]
        for k in KIND_ORDER:
            if not counts.get(k):
                continue
            d = sig[sig["类型"] == k]
            md += [f"### {k}（{len(d)}）", ""]
            order = d.groupby("strategy_id").size().sort_values().index
            for sid in order:
                g = merge_brackets(d[d["strategy_id"] == sid])
                md.append(f"**{STRATEGY_MAP[sid].name}**（{len(g)}）")
                for _, r in g.head(per).iterrows():
                    code = f"[{r['代码']}]({base}?s={quote(str(r['代码']))})" if base else str(r["代码"])
                    risk = "" if pd.isna(r.get("风险%")) else f"（风险{r['风险%']}%）"
                    idx = "（指数）" if r.get("指数") == "是" else ""
                    md.append(f"- {code} {r.get('名称', '')}{idx} **{r['方向']}**：{r['操作']}{risk}")
                if len(g) > per:
                    md.append(f"- …另 {len(g) - per} 条")
                md.append("")
        if n_today:
            t = pos[pos["入场日"] == res["bar_date"]]
            md += [f"### 今日已触发入场（{n_today}）", ""]
            md += [f"- {r['代码']} {r.get('名称', '')} {r['策略']} {r['方向']} 入场{r['入场价']} 止损{r['止损']}"
                   for _, r in t.head(per * 2).iterrows()]
            md.append("")
        md.append("> 完整操作说明见网页「自选股每日扫描」页。仅供学习研究，不构成投资建议。")
        return "\n".join(md)

    desp = build(per)
    while len(desp.encode("utf-8")) > limit and per > 2:
        per -= 2
        desp = build(per)
    short = "，".join(f"{k}{counts[k]}" for k in KIND_ORDER if counts.get(k)) + (f"，今日触发{n_today}" if n_today else "")
    return title[:32], desp, short[:64]


def send_serverchan(title: str, desp: str, short: str = "") -> str:
    import re
    import urllib.parse
    import urllib.request
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not key:
        raise RuntimeError("未设置 SERVERCHAN_KEY 环境变量")
    m = re.match(r"^sctp(\d+)t", key)
    url = (f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m      # Server酱³
           else f"https://sctapi.ftqq.com/{key}.send")                     # Server酱 Turbo
    data = urllib.parse.urlencode({"title": title, "desp": desp, "short": short, "tags": "Street Smarts"}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data), timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"Server酱返回错误：{resp}")
    return "Server酱³" if m else "Server酱 Turbo"


def cmd_daily(args):
    cfg = load_cfg(args.config)
    if args.watchlist:
        cfg["watchlist"] = args.watchlist
    sessions = list(cfg["sessions"]) if args.session == "auto" else [args.session]
    if args.session == "auto":  # 按当前时间选择：北京时间 12:00 前跑美欧，之后跑亚洲
        sessions = ["us_eu"] if datetime.now(ZoneInfo("Asia/Shanghai")).hour < 12 else ["asia"]
    state_path = Path(args.state) if args.state else None
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path and state_path.exists() else {}
    for s in sessions:
        res = run_scan(cfg, s, any_date=args.any_date, limit=args.limit)
        paths = save_outputs(res, args.out, None if args.no_history else "history")
        sig = res["signals"]
        counts = sig["类型"].value_counts().to_dict() if not sig.empty else {}
        print(f"[{s}] 自选 {res['n_inst']} 只 · 有效扫描 {res['n_scanned']} · K线日期 {res['bar_date']} · "
              f"新信号 {len(sig)} {counts} · 持有中 {len(res['positions'])} · 无新K线 {len(res['stale'])} · "
              f"失败 {len(res['errors'])}")
        subject, body, text = build_email(res, cfg)
        if args.preview:
            Path(args.preview).write_text(body, encoding="utf-8")
            print("邮件预览：", args.preview)
        notify = args.always or should_send(res, cfg)
        if notify and not args.always and res["bar_date"] and state.get(s) == res["bar_date"]:
            print(f"{s} {res['bar_date']} 已推送过（备用定时任务），不重复推送")
            notify = False
        sent = False
        if args.wechat:
            if notify:
                title, desp, short = build_wechat(res, cfg)
                if args.preview:
                    Path(args.preview).with_suffix(".wechat.md").write_text(desp, encoding="utf-8")
                print(f"已推送微信（{send_serverchan(title, desp, short)}）：{title}")
                sent = True
            else:
                print("无需提醒的信号，未推送微信")
        if args.email:
            if notify:
                to = send_email(subject, body, text, [paths["signals"], paths["positions"]])
                print(f"已发送邮件（{len(to.split(','))} 个收件人）：{subject}")
                sent = True
            else:
                print("无需提醒的信号，未发送邮件（--always 可强制发送）")
        if sent and state_path and res["bar_date"]:
            state[s] = res["bar_date"]
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state), encoding="utf-8")
