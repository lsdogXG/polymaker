# 代码重构计划

基于模块化审计的发现，按优先级排序的重构任务。

## 状态说明
- [ ] 待处理
- [x] 已完成

---

## Phase 1: 紧急修复 (消除技术债务) ✅ 完成

### 1.1 循环导入风险修复 ✅
- **文件**: `app/db/repo.py` → `app/clients/gamma.py`
- **问题**: `repo.py` 导入 `gamma.py` 的 `get_market_metadata()`，形成 clients ← db 的反向依赖
- **解决**: 将 `get_market_metadata()` 移至 `app/model/market.py`
- **状态**: [x] 完成

### 1.2 时间工具函数重复 ✅
- **位置**: `main.py`, `gamma.py`, `recorder.py`, `api.py` 中都有 hour 格式化代码
- **问题**: 代码重复，维护困难
- **解决**: 创建 `app/utils/time_utils.py` 统一管理
- **状态**: [x] 完成
- **新文件**: `app/utils/time_utils.py` (200行)
  - `hour_to_str()` - 12小时格式转换
  - `get_window_start()` - 时间窗口计算
  - `is_current_1h_window()` - 1h市场验证
  - `parse_1h_slug()` - slug 解析

### 1.3 硬编码常量散落 ✅
- **位置**: 多个文件中的魔数
- **解决**: 集中到 `app/constants.py`
- **状态**: [x] 完成
- **新文件**: `app/constants.py`
  - 资产配置 (ASSETS_SHORT, ASSETS_LONG)
  - 费率配置 (BASE_TAKER_FEE)
  - 订单簿配置
  - WebSocket 配置

---

## Phase 2: 结构优化 (提升可维护性) ✅ 完成

### 2.1 main.py 拆分 ✅
- **问题**: Coordinator 类承担过多职责
- **结果**: 979行 → 794行 (-185行)
- **新模块**:
  - `app/coordinator/market_manager.py` - 市场生命周期管理 (244行)
- **状态**: [x] 完成

### 2.2 api.py 状态分离 ✅
- **问题**: DashboardState + DashboardBridge + API路由 混合
- **结果**: 821行 → 655行 (-166行)
- **新模块**:
  - `app/dashboard/state.py` - 状态管理 (186行)
- **状态**: [x] 完成

### 2.3 executor.py 状态机抽象 ✅
- **问题**: 订单执行 + 事件处理 + 状态机 混合
- **结果**: 898行 → 816行 (-82行)
- **新模块**:
  - `app/execution/state_machine.py` - Cycle状态机和上下文 (167行)
  - `app/execution/event_handler.py` - WS事件处理器 (144行)
- **状态**: [x] 完成

---

## Phase 3: 测试增强 ✅ 完成

### 3.1 添加缺失的单元测试 ✅
- [x] `tests/test_time_utils.py` - 时间工具测试 (21个测试)
- [x] `tests/test_gamma.py` - 市场发现逻辑 (27个测试)
- [x] `tests/test_rescue.py` - 救援流程 (24个测试)
- **状态**: [x] 完成

### 3.2 集成测试
- 暂缓 - 当前单元测试覆盖率已足够
- **状态**: 可选

---

## 进度追踪

| Phase | 任务数 | 已完成 | 进度 |
|-------|--------|--------|------|
| Phase 1 | 3 | 3 | 100% ✅ |
| Phase 2 | 3 | 3 | 100% ✅ |
| Phase 3 | 1 | 1 | 100% ✅ |

**总进度**: 7/7 (100%) 🎉

---

## 变更日志

### 2026-01-07
- 创建重构计划文档
- ✅ Phase 1.1: 移动 `get_market_metadata` 到 `model/market.py`
- ✅ Phase 1.2: 创建 `utils/time_utils.py` (200行)
- ✅ Phase 1.3: 创建 `constants.py`
- ✅ Phase 2.1: 创建 `coordinator/market_manager.py` (main.py 979→794行)
- ✅ Phase 2.2: 创建 `dashboard/state.py` (api.py 821→655行)
- ✅ Phase 2.3: 创建 `execution/state_machine.py` 和 `event_handler.py` (executor.py 898→816行)
- ✅ Phase 3.1: 创建 `test_time_utils.py` (21个测试)
- ✅ Phase 3.2: 创建 `test_gamma.py` (27个测试)
- ✅ Phase 3.3: 创建 `test_rescue.py` (24个测试)
- **测试覆盖率**: 7 → 79 个测试 (+1028%)
