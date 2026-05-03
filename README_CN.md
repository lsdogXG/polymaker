# Polymarket 自动套利系统

一个基于 Polymarket CLOB API 的全自动低风险套利交易系统。

## 系统概述

```
┌─────────────────────────────────────────────────────────────────────┐
│                         polymarket_arb2                             │
├─────────────────────────────────────────────────────────────────────┤
│  ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌─────────────────┐   │
│  │ 数据层  │ → │  策略层  │ → │  执行层  │ → │     持久化      │   │
│  │ clients │   │ strategy │   │execution │   │       db        │   │
│  └─────────┘   └──────────┘   └──────────┘   └─────────────────┘   │
│       ↑                                              ↓              │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    监控层 (api + frontend)                   │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 目录结构

```
polymarket_arb2/
├── app/                    # 核心应用
│   ├── main.py            # 入口 + 协调器
│   ├── config.py          # 配置管理
│   ├── logging.py         # 日志系统
│   ├── monitor.py         # 运行时统计
│   ├── api.py             # Dashboard API
│   │
│   ├── clients/           # 外部服务客户端
│   ├── strategy/          # 套利策略
│   ├── execution/         # 订单执行
│   ├── model/             # 数据模型
│   └── db/                # 数据库
│
├── frontend/              # React 监控面板
├── tests/                 # 测试用例
├── .env.example           # 配置模板
└── requirements.txt       # 依赖
```

---

## 核心模块

### 1. 数据层 (`app/clients/`)

负责与外部服务通信，获取市场数据和执行交易。

| 文件 | 功能 |
|------|------|
| `clob.py` | Polymarket CLOB API 封装：下单、撤单、查询仓位、合并仓位 |
| `gamma.py` | Gamma API 市场发现：轮询新市场、过滤目标市场 |
| `ws_market.py` | WebSocket 行情推送：实时订单簿更新、价格变动 |
| `ws_user.py` | WebSocket 用户推送：订单状态、成交通知 |

**数据流向**：
```
Polymarket API  ──WebSocket──>  ws_market.py  ──>  策略层
                              ws_user.py   ──>  执行层
Gamma API       ──HTTP──────>  gamma.py    ──>  协调器
```

---

### 2. 策略层 (`app/strategy/`)

实现套利逻辑，评估交易机会。

| 文件 | 功能 |
|------|------|
| `fullset.py` | **FULLSET 策略**：当 Yes + No < 1 时同时买入双方，赚取差价 |
| `single_leg.py` | **SINGLE_LEG 策略**：新市场低价捡漏，等待对冲机会 |
| `enhanced.py` | **增强策略**：仓位合并、波动率过滤、止损、冷却期 |
| `sizing.py` | 仓位计算：根据订单簿深度计算最优下单量 |
| `context.py` | 策略上下文：全局配置访问 |

**策略评估流程**：
```
订单簿更新 → evaluate_fullset() → 有机会? → 生成 TradeIntent
                    ↓ 无
            evaluate_single_leg() → 有机会? → 生成 TradeIntent
                    ↓ 无
            等待下次更新
```

**FULLSET 套利原理**：
```
正常情况: Yes($0.60) + No($0.40) = $1.00  (无套利空间)
套利机会: Yes($0.50) + No($0.45) = $0.95  (买入后保证盈利$0.05)
```

---

### 3. 执行层 (`app/execution/`)

管理订单生命周期，处理成交和异常。

| 文件 | 功能 |
|------|------|
| `executor.py` | **执行引擎**：状态机管理，订单提交，成交处理 |
| `rescue.py` | **救援模块**：部分成交时尝试补仓或平仓 |
| `risk.py` | **风控模块**：熔断器、日亏损限制、敞口控制 |

**订单状态机**：
```
CREATED → SUBMITTED → PARTIAL → HEDGED → CONFIRMED
                ↓         ↓
            REJECTED   RESCUE_SUBMITTED → RESCUED/FLATTENED/FAILED
```

**风控检查**：
- 单笔最大金额
- 单市场最大敞口
- 总敞口限制
- 日亏损熔断
- 连续失败熔断

---

### 4. 数据模型 (`app/model/`)

定义核心数据结构。

| 文件 | 功能 |
|------|------|
| `market.py` | 市场元数据：condition_id, token_ids, outcomes |
| `orderbook.py` | 订单簿：bids/asks, best_bid/ask, tick_size |
| `intent.py` | 交易意图：策略生成的交易计划 |
| `state.py` | 状态定义：Cycle状态、订单状态常量 |

**核心数据流**：
```
MarketMeta (市场信息)
     ↓
Orderbook (订单簿) + 策略逻辑
     ↓
TradeIntent (交易意图)
     ↓
CycleContext (执行上下文) → 提交到 CLOB
```

---

### 5. 持久化层 (`app/db/`)

MongoDB 数据存储。

| 文件 | 功能 |
|------|------|
| `mongo.py` | MongoDB 连接管理 |
| `repo.py` | 数据仓库：市场、周期、订单、审计日志 CRUD |

**存储的数据**：
- `markets`: 市场元数据
- `cycles`: 交易周期状态
- `orders`: 订单记录
- `audit_logs`: 操作日志（用于回溯分析）

---

### 6. 监控层 (`app/api.py` + `frontend/`)

实时监控 Dashboard。

**API 功能**：
- WebSocket 推送：仓位、订单簿、交易记录
- REST 接口：历史查询、统计数据
- Token 认证：保护 API 安全

**前端功能**：
- 实时仓位显示
- 订单簿可视化
- 交易历史
- 系统状态

---

### 7. 入口协调器 (`app/main.py`)

系统核心，协调所有模块。

**主要职责**：
- 初始化所有组件
- 管理市场订阅
- 分发订单簿更新到策略
- 启动后台任务（轮询、清理、合并）
- 优雅关闭处理

**启动的异步任务**：
```
┌─ market_ws      : WebSocket 行情接收
├─ user_ws        : WebSocket 用户消息接收
├─ gamma_poll     : 定期轮询新市场
├─ status_reporter: 状态日志输出
├─ cycle_cleanup  : 清理卡住的周期
├─ position_merge : 定期合并对冲仓位
└─ api_server     : Dashboard API 服务
```

---

## 配置参数

### 必需配置

```bash
# 钱包
PRIVATE_KEY=0x...          # 私钥
FUNDER_ADDRESS=0x...       # 钱包地址
SIGNATURE_TYPE=2           # 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE

# 数据库
MONGODB_URI=mongodb://127.0.0.1:27017
MONGODB_DB=polymarket_arb
```

### 策略参数

```bash
ENTRY_BUFFER=0.05          # 入场缓冲 (Yes+No < 0.95 才入场)
FEE_BUFFER=0.002           # 费用预估
GIFT_PRICE=0.02            # SINGLE_LEG 低价阈值
```

### 风控参数

```bash
MAX_USDC_PER_TRADE=200     # 单笔最大 ($)
MAX_USDC_PER_MARKET=500    # 单市场最大 ($)
MAX_TOTAL_USDC=2000        # 总敞口最大 ($)
MAX_DAILY_LOSS=100         # 日亏损熔断 ($)
MAX_CONSECUTIVE_FAILURES=5 # 连续失败熔断
```

### 增强策略参数

```bash
ENABLE_POSITION_MERGE=1    # 启用仓位合并
MIN_MERGE_SIZE=5           # 最小合并量

ENABLE_VOLATILITY_FILTER=1 # 启用波动率过滤
MAX_VOLATILITY_PCT=0.15    # 最大波动率 (15%)
MAX_SPREAD_PCT=0.05        # 最大价差 (5%)

ENABLE_STOP_LOSS=1         # 启用止损
STOP_LOSS_PCT=-0.05        # 止损阈值 (-5%)

TRADE_COOLDOWN_SEC=30      # 交易冷却期 (秒)
MAX_HOLD_SEC=900           # 最大持仓时间 (秒)
```

### 运行参数

```bash
DRY_RUN=0                  # 1=模拟, 0=实盘
LOG_DIR=./logs             # 日志目录
DASHBOARD_PORT=8080        # Dashboard 端口
```

---

## 使用指南

### 前置要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.11+ | 推荐 3.11 或 3.12 |
| MongoDB | 6.0+ | 存储交易数据 |
| Node.js | 18+ | 仅 Dashboard 前端需要（可选） |

### 第一步：克隆并进入项目

```bash
cd D:\01-Code-Projects\python\polymarket_money_maker\better\polymarket_arb2
```

### 第二步：创建 Python 虚拟环境

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**Windows (CMD):**
```cmd
python -m venv venv
venv\Scripts\activate.bat
```

**Linux/macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 第三步：安装依赖

```bash
pip install -r requirements.txt
```

如果安装失败，尝试：
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 第四步：启动 MongoDB

**方式一：Docker（推荐）**
```bash
docker run -d -p 27017:27017 --name polymarket-mongo mongo:7
```

**方式二：本地安装**
- Windows: 下载 [MongoDB Community Server](https://www.mongodb.com/try/download/community)
- 安装后启动服务：`net start MongoDB`

**验证 MongoDB 运行：**
```bash
# 应该能连接成功
mongosh --eval "db.runCommand({ping:1})"
```

### 第五步：配置环境变量

```bash
# 复制配置模板
copy .env.example .env
```

**编辑 `.env` 文件，填入必需配置：**

```bash
# ========== 必填 ==========
# 钱包私钥（不含 0x 前缀也可以）
PRIVATE_KEY=0x你的私钥

# 钱包地址
FUNDER_ADDRESS=0x你的钱包地址

# 签名类型：0=EOA普通钱包, 1=POLY_PROXY, 2=GNOSIS_SAFE
SIGNATURE_TYPE=0

# ========== 可选（有默认值） ==========
# API 凭证（留空会自动生成）
API_KEY=
API_SECRET=
API_PASSPHRASE=

# MongoDB 连接
MONGODB_URI=mongodb://127.0.0.1:27017
MONGODB_DB=polymarket_arb

# 风控参数
MAX_USDC_PER_TRADE=200
MAX_USDC_PER_MARKET=500
MAX_TOTAL_USDC=2000
MAX_DAILY_LOSS=100

# 运行模式：1=模拟（不真实下单），0=实盘
DRY_RUN=1
```

### 第六步：首次运行（模拟模式）

**建议先以模拟模式运行，确认一切正常：**

```bash
# 确保 .env 中 DRY_RUN=1
python -m app.main
```

**正常启动输出：**
```
14:30:00 INFO     app.main             Logging initialized: console=20 file=./logs
14:30:00 INFO     app.main             Settings loaded
14:30:01 INFO     app.clients.clob     CLOB client initialized
14:30:01 INFO     app.db.mongo         Connected to MongoDB
14:30:02 INFO     app.main             Starting dashboard API server on port 8080
14:30:02 INFO     app.main             Connected to market WebSocket
14:30:02 INFO     app.main             Connected to user WebSocket
14:30:03 INFO     app.main             Sent market subscription for 2 tokens
14:30:05 INFO     app.main             status: markets=1 conditions=1 tokens=2 cycles=0
```

### 第七步：切换到实盘模式

确认模拟运行无误后：

1. 编辑 `.env`，设置 `DRY_RUN=0`
2. 确保钱包有足够 USDC（Polygon 链）
3. 确保钱包有少量 MATIC 作为 Gas 费
4. 重新启动程序

```bash
python -m app.main
```

### 第八步：查看 Dashboard（可选）

启动后访问：http://localhost:8080

如需启用认证，在 `.env` 中设置：
```bash
# 生成 token
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 将生成的 token 填入
DASHBOARD_TOKEN=你生成的token
```

---

## 运行命令汇总

```bash
# 激活虚拟环境
.\venv\Scripts\Activate.ps1          # Windows PowerShell
source venv/bin/activate              # Linux/macOS

# 启动 MongoDB（Docker）
docker start polymarket-mongo         # 如果已创建
docker run -d -p 27017:27017 --name polymarket-mongo mongo:7  # 首次

# 运行主程序
python -m app.main

# 运行测试
pytest tests/ -v

# 查看日志
type logs\arb_2024-01-05.log          # Windows
tail -f logs/arb_2024-01-05.log       # Linux/macOS
```

---

## 停止程序

**优雅停止（推荐）：**
- 按 `Ctrl+C`，程序会完成当前周期后退出

**强制停止：**
- 按 `Ctrl+C` 两次

---

## 常见问题

### Q1: 提示 "CLOB client initialization failed"
**原因**：私钥或钱包地址配置错误
**解决**：
1. 检查 `PRIVATE_KEY` 格式（应为 64 位十六进制）
2. 检查 `FUNDER_ADDRESS` 格式（应为 42 位，以 0x 开头）
3. 确保私钥和地址匹配

### Q2: 提示 "MongoDB connection failed"
**原因**：MongoDB 未启动或连接配置错误
**解决**：
```bash
# 检查 MongoDB 是否运行
docker ps | findstr mongo

# 如果未运行，启动它
docker start polymarket-mongo
```

### Q3: 提示 "No markets found"
**原因**：当前没有符合条件的 BTC 15分钟市场
**解决**：这是正常情况，程序会持续轮询，等待新市场出现

### Q4: 提示 "Circuit breaker tripped"
**原因**：触发风控熔断（日亏损或连续失败）
**解决**：
1. 查看日志确认原因
2. 如需重置，重启程序（日亏损会在次日自动重置）

### Q5: WebSocket 频繁断开重连
**原因**：网络不稳定
**解决**：
1. 检查网络连接
2. 如使用代理，确保代理稳定
3. 程序会自动重连，少量断开属正常

### Q6: 如何查看交易记录？
**方式一**：查看日志文件
```bash
type logs\trades_2024-01-05.log
```

**方式二**：查询 MongoDB
```bash
mongosh polymarket_arb
db.cycles.find().sort({created_at: -1}).limit(10)
```

**方式三**：Dashboard 界面
访问 http://localhost:8080

---

## 生产环境部署建议

### 1. 使用 systemd 服务（Linux）

创建 `/etc/systemd/system/polymarket-arb.service`：
```ini
[Unit]
Description=Polymarket Arbitrage Bot
After=network.target mongod.service

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/polymarket_arb2
Environment=PATH=/home/trader/polymarket_arb2/venv/bin
ExecStart=/home/trader/polymarket_arb2/venv/bin/python -m app.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl enable polymarket-arb
sudo systemctl start polymarket-arb
sudo systemctl status polymarket-arb
```

### 2. 使用 PM2（跨平台）

```bash
npm install -g pm2
pm2 start "python -m app.main" --name polymarket-arb
pm2 save
pm2 startup
```

### 3. Docker 部署

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "app.main"]
```

```bash
docker build -t polymarket-arb .
docker run -d --env-file .env --name arb polymarket-arb
```

---

## 日志系统

日志输出到 `./logs/` 目录：

| 文件 | 内容 |
|------|------|
| `arb_YYYY-MM-DD.log` | 完整运行日志 |
| `trades_YYYY-MM-DD.log` | 交易相关日志 |
| `errors_YYYY-MM-DD.log` | 错误日志 |

控制台实时输出格式：
```
HH:MM:SS INFO     app.main             status: markets=1 cycles=0 book_updates=1234
HH:MM:SS INFO     app.main             risk: spend=0.00/2000.00 daily_pnl=0.00 cb=OK
```

---

## 扩展开发

### 添加新策略

1. 在 `app/strategy/` 下创建新文件
2. 实现评估函数：
```python
def evaluate_xxx(market: MarketMeta, ob_yes: Orderbook, ob_no: Orderbook) -> TradeIntent | None:
    # 策略逻辑
    if 有机会:
        return TradeIntent(...)
    return None
```
3. 在 `main.py` 的 `on_book_update` 中调用

### 添加新市场发现源

修改 `app/clients/gamma.py`：
- `poll_new_markets()`: 修改轮询逻辑
- `_is_target_market()`: 修改过滤规则

### 添加新风控规则

修改 `app/execution/risk.py`：
- `CircuitBreaker`: 添加新的熔断条件
- `RiskManager.can_open()`: 添加新的检查逻辑

---

## 注意事项

1. **资金安全**：私钥请妥善保管，建议使用专用交易钱包
2. **风险控制**：首次运行建议设置 `DRY_RUN=1` 进行模拟
3. **网络要求**：需要稳定的网络连接到 Polymarket API
4. **API 限制**：注意 Polymarket API 的速率限制

---

## 技术栈

- **语言**: Python 3.11+
- **异步框架**: asyncio
- **WebSocket**: websockets
- **HTTP**: httpx
- **数据库**: MongoDB (motor)
- **API**: FastAPI + uvicorn
- **前端**: React + Vite + Tailwind CSS
- **交易 SDK**: py-clob-client
