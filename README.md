# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/dispatch.py`：管网调度账：事件、支路隔离凭证、关阀回执、占用去重、按命令号重试和恢复联锁。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。

管网调度账接口包括 `POST /api/network/events`、`POST /api/branches`、`GET /api/branches/<code>`、`GET /api/branches/<code>/ledger`、`POST /api/branches/<code>/occupations`（重复申请拿回原单，旧版本号返回 `409 version_conflict`）、`POST /api/vouchers`、`POST /api/vouchers/<no>/valves`（按 `command_no` 重试，已确认阀门不变）、`POST /api/vouchers/<no>/flush`（全部阀门确认关断后才放行）、`POST /api/vouchers/<no>/restore`（有别的未结束事件占着支路时返回 `409 branch_occupied` 并列出占用单号）。旧的污染响应事件没有隔离凭证，仍走原流程、照旧可查。

测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限、版本冲突、占用去重、并发申请、关阀重试、冲洗联锁和恢复拦截。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
