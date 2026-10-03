# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本、支路占用唯一索引和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/scheduling.py`：支路占用单、隔离凭证和关阀回执校验。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`POST /api/occupancies`、`POST /api/valve-receipts`、`GET /api/occupancies/<order_no>`、`GET /api/branches/<branch_id>/ledger` 和审计查询。升级后的事件冲洗必须在动作负载中带 `branch_id`，所有隔离阀门确认关断后才放行；旧事件（`ledger_version=0`）仍按无隔离凭证原流程处理，升级后历史和审计仍可查询。占用申请可携带支路 `expected_version`，冲突时读取返回的 `current_version` 后重提。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限、版本冲突、支路占用、关阀重试、恢复拦截和旧数据兼容。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
