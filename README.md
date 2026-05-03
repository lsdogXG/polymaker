# Polymaker / Polymarket Arb v2

Polymarket 多资产预测市场套利机器人。

## 功能

- **多资产**：BTC / ETH / SOL / XRP
- **多周期**：15m / 1h / 4h
- **混合双模式**：CLOB 限价单 + 市价对冲
- **完整管线**：clients → coordinator → strategy → execution → persistence
- **Real-time monitoring**：FastAPI dashboard + WebSocket frontend

## 架构

```
app/
├── main.py            主协调器
├── clients/           Polymarket CLOB / Gamma / WS market / WS user
├── coordinator/       market_manager (订单簿 + 信号路由)
├── strategy/          fullset / sizing / hedging
├── execution/         executor / state_machine / event_handler / risk / rescue
├── model/             intent / market / orderbook / state
├── db/                MongoDB 持久化
├── dashboard/         FastAPI 监控
└── api.py             REST 接口

frontend/              Vite + React 看板
scripts/               部署脚本
*.service              systemd 部署文件
```

## 部署

```bash
# 1. 安装
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 2. 配置（基于 .env.example）
cp .env.example .env
# 填入 PRIVATE_KEY / API_KEY / API_SECRET / API_PASSPHRASE / FUNDER_ADDRESS

# 3. MongoDB
mongod  # 或使用 docker

# 4. 启动 dev
python -m app.main

# 5. 启动 frontend dev
cd frontend && npm install && npm run dev

# 6. 生产部署 (systemd)
sudo bash install-services.sh
```

## License

Private repo, all rights reserved.
