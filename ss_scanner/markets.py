"""市场规则：代码规范化、最小变动单位(tick)、涨跌停、T+1、做空限制、交易时段。"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Market:
    code: str
    name: str
    currency: str
    timezone: str
    open_time: time
    close_time: time
    t_plus_one: bool          # 当日买入不可当日卖出
    short_default: bool       # 默认是否输出/回测做空信号
    short_note: str


MARKETS: dict[str, Market] = {
    "US": Market("US", "美股", "USD", "America/New_York", time(9, 30), time(16, 0),
                 False, True, "可做空（需券商可借券）"),
    "CN": Market("CN", "A股", "CNY", "Asia/Shanghai", time(9, 30), time(15, 0),
                 True, False, "仅融资融券标的可融券卖出，且受 T+1 约束；默认不输出做空信号（--allow-short 开启）"),
    "HK": Market("HK", "港股", "HKD", "Asia/Hong_Kong", time(9, 30), time(16, 0),
                 False, True, "仅港交所『指定可进行卖空的证券』可卖空，需先借券"),
    "KR": Market("KR", "韩股", "KRW", "Asia/Seoul", time(9, 0), time(15, 30),
                 False, True, "2025-03-31 起恢复卖空，须先借券，个人投资者有额外限制"),
    # 其他市场（自选股清单中少量标的；tick 为近似值）
    "JP": Market("JP", "日股", "JPY", "Asia/Tokyo", time(9, 0), time(15, 30),
                 False, True, "可融券卖空（制度信用/一般信用）"),
    "UK": Market("UK", "英股", "GBp", "Europe/London", time(8, 0), time(16, 30),
                 False, True, "可借券卖空（价格单位为便士）"),
    "DE": Market("DE", "德股", "EUR", "Europe/Berlin", time(9, 0), time(17, 30),
                 False, True, "可借券卖空"),
    "AU": Market("AU", "澳股", "AUD", "Australia/Sydney", time(10, 0), time(16, 0),
                 False, True, "仅 ASX 批准名单可卖空"),
}
INDEX_NOTE = "指数不可直接交易，仅作参考（可用对应 ETF/期货执行）"
# 指数：Yahoo 代码 → 所属市场
INDEX_MARKET = {"^GSPC": "US", "^NDX": "US", "^IXIC": "US", "^DJI": "US", "^RUT": "US", "^VIX": "US",
                "^HXC": "US", "^HSI": "HK", "HSTECH.HK": "HK", "^HSCE": "HK",
                "000001.SS": "CN", "399001.SZ": "CN", "000300.SS": "CN", "^KS11": "KR", "^N225": "JP"}


@dataclass
class Instrument:
    raw: str
    market: str            # US / CN / HK / KR
    code: str              # 本地代码，如 600519 / 0700 / 005930 / AAPL
    candidates: list[str] = field(default_factory=list)  # Yahoo 代码候选
    yahoo: str = ""        # 实际使用的 Yahoo 代码
    is_index: bool = False
    name: str = ""         # 中文名（来自自选股清单）
    groups: str = ""       # 自选分组

    @property
    def mkt(self) -> Market:
        return MARKETS[self.market]

    @property
    def display(self) -> str:
        return self.yahoo or (self.candidates[0] if self.candidates else self.code)


_CN_PREFIX = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
_EXPLICIT = re.compile(r"^(US|CN|SH|SZ|BJ|HK|KR|KS|KQ|JP|UK|DE|AU)\s*:\s*(.+)$")
_OTHER_SFX = {"T": "JP", "L": "UK", "DE": "DE", "AX": "AU"}
_OTHER_YAHOO = {"JP": ".T", "UK": ".L", "DE": ".DE", "AU": ".AX"}


def _cn_suffix(code: str) -> str:
    if code.startswith(("60", "68", "90", "5")):
        return ".SS"
    if code.startswith(("00", "30", "20", "1")):
        return ".SZ"
    if code.startswith(("4", "8", "92")):
        return ".BJ"
    return ".SS"


def normalize_symbol(raw: str) -> Instrument:
    """把用户输入的代码统一成 Instrument。

    支持：AAPL / BRK.B / US:AAPL；600519 / SH600519 / 600519.SS / CN:600519；
    700 / 0700 / 00700.HK / HK:700；005930.KS / 247540.KQ / KR:005930（自动尝试 KS→KQ）。
    注意：6 位纯数字默认视为 A 股，韩股请加 .KS/.KQ 或 KR: 前缀。
    """
    s = raw.strip().upper().replace("：", ":")
    if s.startswith("^") or s in INDEX_MARKET:
        return Instrument(raw, INDEX_MARKET.get(s, "US"), s, [s], is_index=True)
    m = _EXPLICIT.match(s)
    if m:
        pfx, body = m.group(1), m.group(2).strip()
        if pfx == "US":
            return Instrument(raw, "US", body, [body.replace(".", "-")])
        if pfx in ("CN",):
            return normalize_symbol(body if "." in body else body.zfill(6))
        if pfx in ("SH", "SZ", "BJ"):
            sfx = {"SH": ".SS", "SZ": ".SZ", "BJ": ".BJ"}[pfx]
            return Instrument(raw, "CN", body, [body + sfx])
        if pfx == "HK":
            code = f"{int(body.split('.')[0]):04d}"
            return Instrument(raw, "HK", code, [code + ".HK"])
        if pfx == "KR":
            code = body.split(".")[0].zfill(6)
            return Instrument(raw, "KR", code, [code + ".KS", code + ".KQ"])
        if pfx in ("KS", "KQ"):
            code = body.split(".")[0].zfill(6)
            return Instrument(raw, "KR", code, [f"{code}.{pfx}"])
        if pfx in _OTHER_YAHOO:
            code = body.split(".")[0]
            return Instrument(raw, pfx, code, [code + _OTHER_YAHOO[pfx]])
    m = _CN_PREFIX.match(s)
    if m:
        sfx = {"SH": ".SS", "SZ": ".SZ", "BJ": ".BJ"}[m.group(1)]
        return Instrument(raw, "CN", m.group(2), [m.group(2) + sfx])
    if "." in s:
        body, sfx = s.rsplit(".", 1)
        if sfx in ("SS", "SH", "SZ", "BJ"):
            return Instrument(raw, "CN", body, [body + (".SS" if sfx in ("SS", "SH") else "." + sfx)])
        if sfx == "HK":
            code = f"{int(body):04d}"
            return Instrument(raw, "HK", code, [code + ".HK"])
        if sfx in ("KS", "KQ"):
            return Instrument(raw, "KR", body, [s])
        if sfx in _OTHER_SFX:
            return Instrument(raw, _OTHER_SFX[sfx], body, [s])
        if sfx == "US":
            return Instrument(raw, "US", body, [body.replace(".", "-")])
    if re.fullmatch(r"\d{6}", s):
        return Instrument(raw, "CN", s, [s + _cn_suffix(s)])
    if re.fullmatch(r"\d{1,5}", s):
        code = f"{int(s):04d}"
        return Instrument(raw, "HK", code, [code + ".HK"])
    return Instrument(raw, "US", s, [s.replace(".", "-")])


# ---------------------------------------------------------------- tick size
_HK_LADDER = [(0.25, 0.001), (0.5, 0.005), (10, 0.01), (20, 0.02), (100, 0.05), (200, 0.1),
              (500, 0.2), (1000, 0.5), (2000, 1), (5000, 2), (math.inf, 5)]
_JP_LADDER = [(3000, 1), (5000, 5), (30000, 10), (50000, 50), (300000, 100), (500000, 500), (math.inf, 1000)]
_KR_LADDER = [(2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500), (math.inf, 1000)]


def tick_size(market: str, price: float, code: str = "") -> float:
    p = abs(float(price))
    if market == "US":
        return 0.01 if p >= 1 else 0.0001
    if market == "CN":
        # ETF / LOF / 可转债等基金类：0.001
        if code[:2] in ("51", "56", "58", "15", "16", "11", "12"):
            return 0.001
        return 0.01
    if market == "HK":
        for up, t in _HK_LADDER:
            if p < up:
                return t
    if market == "KR":
        for up, t in _KR_LADDER:
            if p < up:
                return t
    if market == "JP":
        for up, t in _JP_LADDER:
            if p <= up:
                return t
    if market == "UK":   # 便士，近似
        for up, t in [(10, 0.02), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1), (math.inf, 2)]:
            if p < up:
                return t
    if market == "DE":   # 近似
        for up, t in [(10, 0.001), (50, 0.005), (100, 0.01), (math.inf, 0.05)]:
            if p < up:
                return t
    if market == "AU":
        for up, t in [(0.1, 0.001), (2, 0.005), (math.inf, 0.01)]:
            if p < up:
                return t
    return 0.01


def price_decimals(market: str, price: float, code: str = "") -> int:
    t = tick_size(market, price, code)
    if t >= 1:
        return 0
    return max(0, -int(math.floor(math.log10(t) + 1e-9)))


def price_limit_pct(market: str, code: str) -> float | None:
    """日涨跌幅限制（不识别 ST 的 5%）。US/HK 无涨跌停。"""
    if market == "CN":
        if code.startswith(("300", "301", "688", "689")):
            return 0.20
        if code.startswith(("4", "8", "92")):
            return 0.30
        return 0.10
    if market == "KR":
        return 0.30
    return None


def last_bar_incomplete(market: str, last_date) -> bool:
    """当前是否处于该市场交易时段且最后一根K线是今天（即未收盘）。"""
    m = MARKETS[market]
    now = datetime.now(ZoneInfo(m.timezone))
    try:
        d = last_date.date()
    except AttributeError:
        d = last_date
    return now.weekday() < 5 and d == now.date() and m.open_time <= now.time() < m.close_time


def expected_session(market: str, now: datetime | None = None):
    """该市场「最近一个已收盘的交易日」（按工作日估算，不含节假日）。

    定时任务可能延迟数小时（甚至跨过当地午夜），所以不能简单要求 K 线日期 == 当地今天：
    当地收盘前运行 → 期望上一个工作日；收盘后 → 期望今天。节假日时最新 K 线早于期望日，视为休市跳过。
    """
    m = MARKETS[market]
    now = (now or datetime.now(ZoneInfo(m.timezone))).astimezone(ZoneInfo(m.timezone))
    d = now.date()
    if now.weekday() >= 5 or now.time() < m.close_time:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d
