# Security Policy / 安全策略

## 报告漏洞

请**不要**在公开 Issue 中报告安全问题。请通过 GitHub 的私密渠道报告：
仓库页面 → **Security** → **Report a vulnerability**（Private vulnerability reporting）。

请尽量包含：受影响的文件/功能、复现步骤、可能的影响。一般会在 7 天内回复。

## 支持范围

只维护 `main` 分支的最新版本。

## 使用者须知（保护你自己的数据）

本项目会处理一些私密信息，请勿把它们提交到仓库：

| 内容 | 正确的存放位置 |
| --- | --- |
| 真实自选股 `config/watchlist.csv` | 本地（已在 `.gitignore`）；GitHub Actions 用 Secret `WATCHLIST_CSV_GZ` |
| Server酱 SendKey、SMTP 密码等 | 本地 `.env`（已忽略，参照 `.env.example`）；Actions 用仓库 Secrets |
| 网页地址 | 环境变量 / Secret `SS_WEB_URL` |
| 扫描结果 `latest/`、`history/` | 只在本地生成；Actions 产物在公开仓库中需设置 `ARTIFACT_PASSWORD` 加密 |

如果不小心提交了密钥：立即在对应平台**重置/吊销**该密钥（仅删除提交并不够，历史和缓存中仍可能存在），
再用 `git filter-repo` 清理历史。

## 免责声明

本项目仅用于学习与研究，不构成投资建议。
