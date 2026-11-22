"""响应解析领域服务：管理响应解析规则，与Schema差异检测、数据缓存、数据投递联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.field_quality import FieldQualityService
from app.store import store

TODAY = "2026-11-22"


class ResponseParseService(BaseModuleService):
    MODULE = "response_parse"
    ENTITY = "解析规则"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["规则编号", "目标端点", "JSON路径", "字段别名", "数据类型", "默认值", "解析顺序", "映射描述"]
    REQUIRED_FIELDS = ["规则编号", "目标端点", "JSON路径"]
    STATUS_ORDER = ["草稿", "生效中", "解析异常", "已废弃"]
    ACTION_RULES = {"启效规则": "生效中", "标记异常": "解析异常", "废弃规则": "已废弃"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str):
        """启效规则时同步创建Schema差异检测记录并生成字段转换质量清单；标记异常时写调用日志。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "启效规则":
            self._trigger_schema_diff(entry)
            self._sync_cache_policy(entry)
            checked = self._prepare_quality_checklist(entry)
            message += f"，已触发Schema差异检测、刷新缓存策略并生成字段转换质量清单（{checked}项）"
        elif action == "标记异常":
            self._log_parse_error(entry)
        elif action == "废弃规则":
            self._trigger_data_delivery_retry(entry)
        return entry, message

    def _trigger_schema_diff(self, rule: dict):
        """解析规则生效后登记Schema差异；登记失败写入重放队列并重试。"""
        rid = str(rule["id"]).zfill(4)
        diff = {
            "记录编号": f"SCHE-PARSE-{rid}",
            "关联端点": rule.get("目标端点", ""),
            "变更字段": rule.get("JSON路径", ""),
            "旧定义": "pre_parse",
            "新定义": rule.get("映射描述", ""),
            "差异等级": "INFO",
            "影响评估": f"解析规则{rid}生效触发",
            "检测时间": TODAY,
            "status": "已检测", "pending": True, "abnormal": False,
        }
        self._register_diff_with_retry(diff, rule)

    def _register_diff_with_retry(self, diff: dict, rule: dict, max_retries: int = 3):
        """差异登记失败后重试；每次失败在重放队列留痕，成功后闭环重试任务。"""
        attempts = 0
        while True:
            try:
                self._persist_diff(diff)
                if attempts:
                    self._close_retry_task(rule, attempts)
                return
            except Exception as exc:  # 登记失败要进重放队列并可回查
                attempts += 1
                self._record_retry_task(rule, attempts, exc)
                if attempts >= max_retries:
                    raise

    def _persist_diff(self, diff: dict):
        self._diff_commit_gate(diff)
        diff_rows = store.rows("schema_diff")
        row = dict(diff)
        row["id"] = max((int(r.get("id", 0)) for r in diff_rows), default=0) + 1
        diff_rows.append(row)

    def _diff_commit_gate(self, diff: dict):
        """提交闸口：默认放行；演练或测试可注入一次性故障模拟登记失败。"""

    def _record_retry_task(self, rule: dict, attempt: int, exc: Exception):
        rows = store.rows("retry_queue")
        rid = str(rule["id"]).zfill(4)
        task = next((t for t in rows if t.get("任务编号") == f"RETR-DIFF-{rid}"), None)
        reason = f"Schema差异登记失败:{exc}"
        if task is None:
            task = {
                "id": max((int(t.get("id", 0)) for t in rows), default=0) + 1,
                "任务编号": f"RETR-DIFF-{rid}",
                "失败端点": rule.get("目标端点", ""),
                "失败原因": reason,
                "重试次数": str(attempt),
                "最大重试": "3",
                "退避策略": "立即重试",
                "最后重试": TODAY,
                "队列状态": "重试中",
                "status": "重试中", "pending": True, "abnormal": True,
            }
            rows.append(task)
        else:
            task["重试次数"] = str(attempt)
            task["失败原因"] = reason
            task["最后重试"] = TODAY

    def _close_retry_task(self, rule: dict, attempts: int):
        rid = str(rule["id"]).zfill(4)
        for task in store.rows("retry_queue"):
            if task.get("任务编号") == f"RETR-DIFF-{rid}":
                task["失败原因"] += f"；第{attempts + 1}次尝试登记成功"
                task["队列状态"] = "已成功"
                task["status"] = "已成功"
                task["pending"] = False
                task["abnormal"] = False

    def _prepare_quality_checklist(self, rule: dict) -> int:
        """启效时用已登记的原始响应样本生成字段转换质量清单。"""
        service = FieldQualityService()
        if not service.mapping_spec(rule).get("fields"):
            return 0
        entries = service.validate_rule(rule.get("规则编号", ""))
        rule["质量清单"] = f"已校验{len(entries)}项" if entries else "待样本"
        return len(entries)

    def _sync_cache_policy(self, rule: dict):
        """解析规则变更后更新关联缓存策略的命中标记。"""
        endpoint = rule.get("目标端点", "")
        for cache in store.rows("data_cache"):
            if cache.get("目标端点") == endpoint:
                cache["最后刷新"] = "2026-11-17"
                cache["命中次数"] = str(int(cache.get("命中次数", 0)) + 1)

    def _log_parse_error(self, rule: dict):
        """解析异常时写入调用日志。"""
        log_rows = store.rows("call_log")
        rid = str(rule["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-PARSE-{rid}",
            "接口端点": rule.get("目标端点", ""),
            "请求参数": rule.get("JSON路径", ""),
            "响应状态": "parse_error",
            "响应耗时(ms)": 0,
            "返回行数": 0,
            "错误信息": f"解析规则{rid}异常",
            "调用时间": "2026-11-17",
            "status": "超时", "pending": True, "abnormal": True,
        })

    def _trigger_data_delivery_retry(self, rule: dict):
        """废弃规则时通知数据投递管道重新采集。"""
        endpoint = rule.get("目标端点", "")
        for delivery in store.rows("data_delivery"):
            if delivery.get("数据来源") == endpoint:
                delivery["status"] = "投递中"
                delivery["pending"] = True
                delivery["最后投递"] = "待重采"