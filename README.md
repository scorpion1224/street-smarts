# Street Smarts Scanner · 中美港韩股票短线信号扫描器

把《Street Smarts: High Probability Short-Term Trading Strategies》（Connors & Raschke）里的策略规则化，
判断 **美股 / A股 / 港股 / 韩股** 标的在最新交易日是否满足书中交易信号，并给出下一交易日的挂单价、止损与操作说明；
同时提供日线级回测，方便按书中建议先『纸上交易』验证。

## 1. 快速开始

```bash
pip install -r requirements.txt          # 可选：pip install akshare（A股数据源）
python -m ss_scanner list                # 查看全部策略
python -m ss_scanner scan AAPL 0700.HK 600519 005930.KS
python -m ss_scanner scan -w config/watchlist.yaml
python -m ss_scanner backtest AAPL 600519 0700 KR:005930 --period 10y
python -m unittest discover -s tests     # 单元测试
```

输出在 `reports/`：`report_*.md`（分组报告）、`signals_*.csv`（新信号，Excel 可直接打开）、`recent_*.csv`（近期已触发信号的持仓状态）。

### 代码写法

| 市场 | 写法示例 | 说明 |
| --- | --- | --- |
| 美股 | `AAPL`、`BRK.B`、`US:SPY` | |
| A股 | `600519`、`SH600519`、`600519.SS`、`300750.SZ`、`510300` | 6 位纯数字默认视为 A 股，自动识别沪/深/北 |
| 港股 | `700`、`0700`、`00700.HK`、`HK:9988` | |
| 韩股 | `005930.KS`、`247540.KQ`、`KR:005930` | `KR:` 前缀会自动尝试 KOSPI→KOSDAQ |

### 常用参数

| 参数 | 作用 |
| --- | --- |
| `-s holy_grail,id_nr4` | 只跑指定策略 |
| `--all-strategies` | 包含默认关闭的 `roc2` |
| `--long-only` / `--allow-short` | 只做多 / A股也输出做空 |
| `--asof 2025-06-30` | 以历史某天为『今天』复盘 |
| `--source akshare` / `--source csv --csv-dir data` | 切换数据源（CSV 列：date,open,high,low,close） |
| `--params config/params.yaml` | 覆盖策略参数（见 `config/params.example.yaml`） |
| `--drop-incomplete` | 盘中运行时剔除未收盘K线 |
| `--cost-bps 10` | 回测单边成本 |

## 1.5 网页版（输入代码即可查信号）

```bash
pip install -r requirements.txt
streamlit run app.py          # 浏览器打开 http://localhost:8501 ；同一 Wi-Fi 下手机访问 http://电脑IP:8501
```

- 输入一个或多个代码（空格/逗号分隔，最多 10 个）→ 显示每只股票的今日信号卡片（入场、止损、风险、操作说明）、
  近 120 日 K 线（标注入场/止损线）、最近已触发信号；
- 「历史回测」页可一键回测并下载交易明细；「策略说明」页是规则速查；
- 链接可分享：`http://…/?s=AAPL%200700%20600519` 打开即查询这些代码；
- 左侧栏可选策略、只看做多、A股做空、数据源。

### 免费部署到公网（手机随时打开）

1. 把整个项目上传到你的 GitHub 仓库（可设为私有）；
2. 打开 <https://share.streamlit.io> 用 GitHub 登录 → **Create app** → 选仓库，Main file 填 `app.py` → Deploy；
3. 得到 `https://xxx.streamlit.app` 网址，手机浏览器打开后「添加到主屏幕」，用起来就像一个 App。

> 也可部署到 Hugging Face Spaces（选 Streamlit SDK）或自己的云服务器（`streamlit run app.py --server.port 80`）。
> 海外服务器上 AkShare（东方财富接口）可能不通，会自动回退 Yahoo。

## 2. 已实现策略与规则

| ID | 书中章节 | 设置（以做多为例） | 输出 |
| --- | --- | --- | --- |
| `turtle_soup` | Ch4 | 明日若跌破 20 日低点（该低点距明日 ≥4 天），回到低点上方 5 tick 买入；止损=当日低点下 1 tick | 条件单/预警（收盘距低点 ≤1 ATR 才显示） |
| `turtle_soup_plus_one` | Ch5 | 今日创 20 日新低（前低 ≥3 天前）且收盘 ≤ 前低 → 明日在前低挂买入止损 | 挂单 |
| `eighty_twenty` | Ch6 | 昨日开在区间上 20%、收在下 20% → 今日先跌破昨低再回到昨低买入，日内平仓 | 挂单（带前提） |
| `momentum_pinball` | Ch7 | LBR/RSI（1日涨跌的3期RSI）< 30 → 明日第一小时高点上方买入 | 仅提示（需盘中数据，不回测） |
| `roc2` | Ch8 | 2 日 ROC 拐头向上收盘买入、次日出场；ADX<16 或 >30 且上升时回避；输出明日枢轴 | 收盘入场（默认关闭） |
| `anti` | Ch9 | 随机指标 %K(7,平滑4)/%D(10)：%D 上升，%K 回调后拐头 → 今日高点上方 1 tick 买入，持有 2-4 根 | 挂单 |
| `holy_grail` | Ch10 | ADX(14)>30 且此前上升、+DI>-DI，首次回撤触及 20EMA → 高点上方买入，目标前波段高点 | 挂单 |
| `adx_gapper` | Ch11 | ADX(12)>30 且 +DI(28)>-DI(28)，明日若跳空低于今日低点 → 回到今日低点买入 | 条件单 |
| `whiplash` | Ch12 | 跳空低于昨低，收盘>开盘且在区间上 50% → 收盘买入；次日低开即出场 | 收盘入场 |
| `gap_reversal_3day` | Ch13 | 向下跳空当日未回补，此后 3 天内突破缺口日高点 1 tick 买入 | 挂单 |
| `id_nr4` | Ch19 | 内包日 + 4 日最窄 → 次日双向突破挂单（反向单兼作止损），2 天不盈利出场 | 挂单（双向） |
| `hv_crabel` | Ch20 | HV6/HV100 < 50% 且 内包日或 NR4 → 次日双向突破挂单，持有 1-4 天 | 挂单（双向） |

**未实现**：新闻反转（Ch16-18，需事件/盘中数据）、Spike & Ledge / Wolfe Waves 等主观形态（Ch14-15）、
Smart Money / TICK / TRIN（Ch21，需盘中与市场宽度数据）、A/D Market Buster（需 NYSE 涨跌家数）。

**做空**：所有策略只写做多逻辑，做空通过『镜像价格』（high↔-low 等）自动得到，指标随之对称（+DI/-DI 互换、%K→100-%K），
单元测试验证了两侧信号严格对称。

## 3. 市场规则处理

| 市场 | tick | 涨跌停 | T+1 | 做空 |
| --- | --- | --- | --- | --- |
| 美股 | 0.01（<$1 为 0.0001） | 无 | 否 | 默认输出 |
| A股 | 股票 0.01，ETF/LOF 0.001 | 主板 10%、创业板/科创板 20%、北交所 30%（未识别 ST 5%） | 是：入场当日不能卖，日内策略改为次日开盘平仓 | 默认不输出（需融券，`--allow-short` 开启） |
| 港股 | 港交所价位表（0.001～5） | 无（有 VCM） | 否 | 默认输出，仅限指定可卖空证券 |
| 韩股 | 韩国交易所价位表（1～1000 韩元） | 30% | 否 | 默认输出，需借券 |

『1 tick』的止损/入场偏移均按各市场真实价位表取整；书中针对期货的『5-15 tick』偏移改为 `max(N tick, 比例×ATR)`，避免高价股偏移过小。

## 4. 回测假设（日线近似）

- 止损单若开盘跳过触发价，按开盘价成交；条件单满足前提后按触发价成交。
- 固定止损的突破单，若入场当天同时触及止损，按止损计（最坏情形）；『入场当日低点下 1 tick』类止损视为当日不会触发。
- 一字涨跌停K线既不能成交也不能出场；A股 T+1 入场当天不能卖出。
- 同一标的、同一策略、同一方向不重叠持仓；统计为信号级（非组合资金曲线）；默认单边成本 10bp。
- 使用 Yahoo 前复权数据（`auto_adjust`），最新K线价格与实际价格一致。

> 实测提示：用默认参数在 2016-2026 年 9 只大盘股（AAPL、NVDA、SPY、茅台、宁德、腾讯、阿里、三星、海力士）上回测，
> 大部分策略扣成本后并无显著优势，做多侧的 Whiplash / ID-NR4 / ADX Gapper 相对较好。
> 这与书中『先纸上交易、只精通一个策略、参数需重新验证』的建议一致——请把它当作研究工具，而不是自动交易系统。

## 5. 项目结构

```
ss_scanner/
  markets.py      代码规范化、tick、涨跌停、T+1、做空限制、交易时段
  data.py         Yahoo / AkShare / CSV 数据与本地缓存
  indicators.py   ADX/DMI、RSI、LBR/RSI、随机指标、EMA、ATR、历史波动率
  strategies.py   12 个策略 + 参数 PARAMS + 注册表
  engine.py       方向空间(镜像做空)、信号生成、成交模拟、扫描、回测、统计
  report.py       Markdown / CSV 报告
  cli.py          命令行
app.py            Streamlit 网页版
config/watchlist.yaml       四市场示例自选股
config/params.example.yaml  参数覆盖示例
tests/test_core.py          单元测试（代码识别、tick、策略、对称性、T+1）
```

扩展新策略：在 `strategies.py` 写一个 `fn(F, i, cx) -> list[Signal]`（只写做多逻辑），加入 `STRATEGIES` 即可自动支持扫描、做空和回测。

---

⚠️ 仅用于学习与研究，不构成投资建议。
