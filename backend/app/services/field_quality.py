"""字段转换质量清单领域服务：用原始响应样本校验解析规则的字段映射。

与 response_parse（解析规则）、schema_diff（差异确认）、data_delivery（投递待办）联动：
- 解析规则启效后，按映射规格对登记的原始响应样本逐字段试跑转换；
- 字段缺失与实际空值分别给出判定条件，不混为一谈；
- 前导零等影响业务标识的值在清单中保留原值，可从清单还原原样；
- 每列实际启效的取值范围与清单标出的原始值对照，供 Schema 差异确认可发布范围；
- 校验用过的样本与已验字段可通过 quality_trace 回查。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

TODAY = "2026-11-22"

MISSING = object()  # 字段缺失哨兵：路径不存在，与显式空值 None 严格区分

# 校验结论取值
PASS = "通过"
WARN_LEADING_ZERO = "告警:前导零丢失"
WARN_CONFLICT = "告警:语义冲突"
MISSING_FIELD = "字段缺失"
EXPLICIT_NULL = "实际空值"

# 规则校验给出字段缺失与实际空值各自的判定条件
CRITERIA = {
    "字段缺失": "JSON路径在样本中不存在（键本身缺失，不是值为null）",
    "实际空值": "JSON路径在样本中存在且值为null",
}

# 结论属于已验（可进入发布范围）的集合；告警类一律待核
VERIFIED_CONCLUSIONS = {PASS, MISSING_FIELD, EXPLICIT_NULL}

# 缺失/空值在清单中的展示占位，避免与真实字符串混淆
MISSING_LABEL = "<缺失>"
NULL_LABEL = "<空值>"


def _extract(sample: dict, path: str) -> Any:
    """按 $.a.b 形式的路径取值；路径不存在返回 MISSING 哨兵。"""
    node: Any = sample
    for key in path.lstrip("$.").split("."):
        if not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node


def _convert(raw: Any, spec: dict) -> tuple[Any, str, list[str]]:
    """按映射规格转换单个值，返回 (转换值, 转换方式描述, 告警列表)。"""
    warnings: list[str] = []
    target = spec.get("type", "string")
    if target == "int":
        if isinstance(raw, bool):
            raise ValueError("布尔值不按整数映射")
        if isinstance(raw, str):
            if re.fullmatch(r"0\d+", raw):
                warnings.append(WARN_LEADING_ZERO)
            converted: Any = int(raw)
            method = "字符串→整数"
        else:
            converted = int(raw)
            method = "数值直取→整数"
    elif target == "string":
        # 保留原始字符串：前导零等业务标识原样保留
        converted = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        method = "保留原始字符串"
    else:
        converted = raw
        method = f"直取({target})"
    unit = spec.get("unit")
    if unit:
        divisor = unit.get("divisor", 1)
        converted = converted / divisor
        if isinstance(converted, float) and converted.is_integer():
            converted = int(converted)
        method += f"；{unit.get('from')}→{unit.get('to')}÷{divisor}"
    return converted, method, warnings


class FieldQualityService(BaseModuleService):
    MODULE = "field_quality"
    ENTITY = "转换清单"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["清单编号", "关联规则", "字段名", "原始值", "转换值", "转换方式", "校验结论", "样本编号"]
    REQUIRED_FIELDS = ["清单编号", "关联规则", "字段名"]
    STATUS_ORDER = ["待校验", "已校验", "待核", "已确认"]
    ACTION_RULES = {"执行校验": "已校验", "标记待核": "待核", "确认映射": "已确认"}
    NEGATIVE_ACTIONS = ["标记待核"]

    # ---------- 样本登记与回查 ----------

    def register_sample(self, rule_no: str, endpoint: str, sample_no: str, content: dict) -> dict:
        """登记原始响应样本，供规则校验试跑；样本入库后可通过 quality_trace 回查。"""
        rows = store.rows("parse_sample")
        existing = next((s for s in rows if s.get("样本编号") == sample_no), None)
        if existing is not None:
            return existing
        row = {
            "id": max((int(s.get("id", 0)) for s in rows), default=0) + 1,
            "样本编号": sample_no,
            "关联规则": rule_no,
            "目标端点": endpoint,
            "样本内容": content,
            "用途": "规则校验",
            "登记时间": TODAY,
        }
        rows.append(row)
        return row

    def mapping_spec(self, rule: dict | None) -> dict:
        """解析规则映射描述中的结构化映射规格；非JSON时按无规格处理。"""
        if not rule:
            return {}
        try:
            spec = json.loads(rule.get("映射描述") or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return spec if isinstance(spec, dict) else {}

    # ---------- 校验引擎 ----------

    def validate_rule(self, rule_no: str) -> list[dict]:
        """用登记的全部原始样本试跑规则映射，逐字段 upsert 清单条目。"""
        rule = next((r for r in store.rows("response_parse") if r.get("规则编号") == rule_no), None)
        spec = self.mapping_spec(rule)
        samples = [s for s in store.rows("parse_sample") if s.get("关联规则") == rule_no]
        entries: list[dict] = []
        if not rule or not spec.get("fields"):
            return entries
        for field in spec["fields"]:
            for sample in samples:
                entries.append(self._validate_one(rule, field, sample))
        self._detect_conflicts(rule_no, spec)
        self._refresh_ranges(rule_no, spec)
        return entries

    def _validate_one(self, rule: dict, field: dict, sample: dict) -> dict:
        rows = store.rows(self.MODULE)
        entry = next(
            (r for r in rows
             if r.get("关联规则") == rule.get("规则编号")
             and r.get("字段名") == field["name"]
             and r.get("样本编号") == sample.get("样本编号")),
            None,
        )
        if entry is None:
            new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            entry = {
                "id": new_id,
                "清单编号": f"FQUA-{new_id:04d}",
                "关联规则": rule.get("规则编号", ""),
                "字段名": field["name"],
                "样本编号": sample.get("样本编号", ""),
                "目标端点": rule.get("目标端点", ""),
                "输出列": field.get("alias") or field["name"],
                "status": "待校验",
                "pending": True,
                "abnormal": False,
            }
            rows.append(entry)
        raw = _extract(sample.get("样本内容", {}), field.get("path") or f"$.{field['name']}")
        entry["校验时间"] = TODAY
        entry["待核原因"] = ""
        if raw is MISSING:
            entry.update({
                "原始值": MISSING_LABEL, "原值类型": "缺失",
                "转换值": f"{field.get('default')}（默认值）",
                "转换方式": "路径缺失→默认值",
                "校验结论": MISSING_FIELD,
                "缺失判定": f"JSON路径{field.get('path')}在样本{sample.get('样本编号')}中不存在",
                "空值判定": "不适用（路径不存在，无从判定空值）",
                "可还原": "不适用",
            })
        elif raw is None:
            entry.update({
                "原始值": NULL_LABEL, "原值类型": "空值",
                "转换值": f"{field.get('default')}（默认值）",
                "转换方式": "显式空值→默认值",
                "校验结论": EXPLICIT_NULL,
                "缺失判定": "不适用（路径存在，非缺失）",
                "空值判定": f"JSON路径{field.get('path')}在样本{sample.get('样本编号')}中存在且值为null",
                "可还原": "不适用",
            })
        else:
            converted, method, warnings = _convert(raw, field)
            entry.update({
                "原始值": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
                "原值类型": "字符串" if isinstance(raw, str) else "数字",
                "转换值": str(converted),
                "转换方式": method,
                "校验结论": warnings[0] if warnings else PASS,
                "缺失判定": "不适用（路径存在且有值）",
                "空值判定": "不适用（值非null）",
                # 前导零等业务标识原值已留在清单，可还原原样
                "可还原": "是",
            })
        if entry.get("status") != "已确认":
            entry["status"] = "已校验"
            entry["pending"] = True
            entry["abnormal"] = str(entry.get("校验结论", "")).startswith("告警")
        return entry

    def _detect_conflicts(self, rule_no: str, spec: dict):
        """业务标识字段：不同原始值转成同一结果即语义冲突，自动保持待核。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("关联规则") == rule_no]
        for field in spec.get("fields", []):
            if not field.get("identifier"):
                continue
            group = [r for r in rows
                     if r.get("字段名") == field["name"]
                     and r.get("校验结论") not in (MISSING_FIELD, EXPLICIT_NULL)]
            by_converted: dict[str, set[str]] = {}
            for item in group:
                by_converted.setdefault(str(item.get("转换值")), set()).add(str(item.get("原始值")))
            for converted, raws in by_converted.items():
                if len(raws) <= 1:
                    continue
                for item in group:
                    if str(item.get("转换值")) == converted and item.get("status") != "已确认":
                        item["校验结论"] = WARN_CONFLICT
                        item["待核原因"] = (
                            f"原始值{'、'.join(sorted(raws))}转换为同一值{converted}，"
                            "业务标识不可区分，保持待核"
                        )
                        item["status"] = "待核"
                        item["pending"] = True
                        item["abnormal"] = True

    def _refresh_ranges(self, rule_no: str, spec: dict):
        """把每列原始值集合→转换值集合的对照结果写回该列所有清单条目。"""
        ranges = self.verify_column_ranges(rule_no)
        rows = [r for r in store.rows(self.MODULE) if r.get("关联规则") == rule_no]
        for item in rows:
            info = ranges.get(item.get("字段名", ""))
            if info:
                item["取值范围"] = info["说明"]

    def verify_column_ranges(self, rule_no: str) -> dict:
        """每列实际启效的取值范围必须和清单标出的原始值对得上（不允许塌缩）。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("关联规则") == rule_no]
        result: dict[str, dict] = {}
        for item in rows:
            name = item.get("字段名", "")
            if item.get("校验结论") in (MISSING_FIELD, EXPLICIT_NULL):
                continue
            slot = result.setdefault(name, {"原始值": set(), "转换值": set()})
            slot["原始值"].add(str(item.get("原始值")))
            slot["转换值"].add(str(item.get("转换值")))
        return {
            name: {
                "原始值": sorted(slot["原始值"]),
                "转换值": sorted(slot["转换值"]),
                "对得上": len(slot["转换值"]) >= len(slot["原始值"]),
                "说明": (
                    f"原始{sorted(slot['原始值'])}→转换{sorted(slot['转换值'])}"
                    + ("（一一对应）" if len(slot["转换值"]) >= len(slot["原始值"]) else "（塌缩，对不上）")
                ),
            }
            for name, slot in result.items()
        }

    def restore_original(self, entry: dict) -> str:
        """从清单还原原样：前导零等影响业务标识的值按清单原始值返回。"""
        return str(entry.get("原始值", ""))

    def publishable_fields(self, endpoint: str) -> dict:
        """按清单结论划分可发布范围：已验字段可发布，告警/待核字段保持待核。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("目标端点") == endpoint]
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row.get("输出列") or row.get("字段名", ""), []).append(row)
        verified, disputed = [], []
        for column, entries in grouped.items():
            if any(str(e.get("校验结论", "")).startswith("告警") or e.get("status") == "待核"
                   for e in entries):
                disputed.append(column)
            elif all(e.get("校验结论") in VERIFIED_CONCLUSIONS for e in entries):
                verified.append(column)
        return {"已验字段": sorted(verified), "待核字段": sorted(disputed)}

    def quality_trace(self, rule_no: str) -> dict:
        """回查：规则校验用过的样本、已验字段、取值范围对照和待核项。"""
        checklist = [r for r in store.rows(self.MODULE) if r.get("关联规则") == rule_no]
        return {
            "关联规则": rule_no,
            "样本": [s for s in store.rows("parse_sample") if s.get("关联规则") == rule_no],
            "已验字段": checklist,
            "取值范围对照": self.verify_column_ranges(rule_no),
            "待核项": [r for r in checklist if r.get("status") == "待核"],
            "判定条件": CRITERIA,
        }

    # ---------- 动作 ----------

    def run_action(self, entry_id: int, action: str):
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "执行校验":
            self.validate_rule(entry.get("关联规则", ""))
            entry = store.find(self.MODULE, entry_id)
            return entry, f"{self.ENTITY}已按原始样本重新校验，结论：{entry.get('校验结论')}"
        if action == "标记待核":
            entry["status"] = "待核"
            entry["pending"] = True
            entry["abnormal"] = True
            entry["待核原因"] = entry.get("待核原因") or f"人工标记待核：{entry.get('校验结论')}"
            return entry, f"{self.ENTITY}已标记待核"
        if action == "确认映射":
            if str(entry.get("校验结论", "")).startswith("告警"):
                return None, f"清单{entry.get('清单编号')}仍存在{entry.get('校验结论')}，不能确认"
            entry["status"] = "已确认"
            entry["pending"] = False
            entry["abnormal"] = False
            return entry, f"{self.ENTITY}映射已确认，进入可发布范围"
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
