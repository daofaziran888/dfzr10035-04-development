"""字段转换质量清单端到端演练。

场景：启效响应解析规则 → Schema差异登记（失败重试）→ 用四条原始响应样本
（账号 0012 / 12 / 缺失 / 显式空值，其中一条带重量 1000 克）校验字段映射 →
争议账号映射保持待核 → Schema差异确认可发布范围 → 同步投递待办 → 执行投递写
数据版本（重量 1 千克、两个账号不合并）→ 修正账号映射后再次确认并核对。

运行：cd backend && .venv/bin/python scripts/demo_field_quality.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import service_for  # noqa: E402
from app.services.field_quality import CRITERIA, MISSING  # noqa: E402
from app.store import store  # noqa: E402

RULE_NO = "RESP-9001"
ENDPOINT = "ENDP-CSV-0001"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    print(f"  [{'通过' if condition else '失败'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def find(rows: list[dict], **conds) -> dict | None:
    return next((r for r in rows if all(r.get(k) == v for k, v in conds.items())), None)


def main():
    parse_svc = service_for("response_parse")
    quality_svc = service_for("field_quality")
    diff_svc = service_for("schema_diff")
    delivery_svc = service_for("data_delivery")

    print("=" * 72)
    print("步骤1  登记解析规则（account→整数 争议映射；weight_g→weight_kg 单位换算）")
    print("=" * 72)
    mapping = {"fields": [
        {"name": "account", "path": "$.account", "type": "int",
         "identifier": True, "default": 0},
        {"name": "weight_g", "path": "$.weight_g", "type": "int", "alias": "weight_kg",
         "unit": {"from": "克", "to": "千克", "divisor": 1000}},
    ]}
    rule, missing = parse_svc.create_entry({
        "规则编号": RULE_NO, "目标端点": ENDPOINT, "JSON路径": "$.account",
        "字段别名": "account", "数据类型": "int", "默认值": "0", "解析顺序": "1",
        "映射描述": json.dumps(mapping, ensure_ascii=False),
    })
    check("规则登记", rule is not None and not missing, f"规则编号={RULE_NO}")

    print("\n步骤2  登记四条原始响应样本（0012+重量1000克 / 12 / 缺失 / 显式空值）")
    samples = [
        ("SMPL-9001", {"order_no": "SO-1001", "account": "0012", "weight_g": 1000}),
        ("SMPL-9002", {"order_no": "SO-1002", "account": 12}),
        ("SMPL-9003", {"order_no": "SO-1003"}),
        ("SMPL-9004", {"order_no": "SO-1004", "account": None}),
    ]
    for sample_no, content in samples:
        quality_svc.register_sample(RULE_NO, ENDPOINT, sample_no, content)
    check("样本登记", len(store.rows("parse_sample")) == 4, "4条样本已入库")

    print("\n步骤3  启效规则：Schema差异登记首次失败 → 重试成功，重放队列留痕")
    calls = {"n": 0}

    def flaky_gate(diff):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("模拟差异登记存储瞬时不可用")

    parse_svc._diff_commit_gate = flaky_gate  # 注入一次性故障
    rule_entry, message = parse_svc.run_action(rule["id"], "启效规则")
    del parse_svc._diff_commit_gate  # 还原闸口
    print(f"  启效结果：{message}")
    diff = find(store.rows("schema_diff"), 关联端点=ENDPOINT)
    retry = find(store.rows("retry_queue"), 任务编号=f"RETR-DIFF-{str(rule['id']).zfill(4)}")
    check("差异登记重试后成功", diff is not None and calls["n"] == 2,
          f"第{calls['n']}次尝试写入{diff and diff['记录编号']}")
    check("重放队列留痕且闭环", retry is not None and retry["status"] == "已成功",
          retry and f"重试次数={retry['重试次数']}，{retry['失败原因']}")

    print("\n步骤4  字段转换质量清单：同一规则的两类转换结果")
    entries = [e for e in store.rows("field_quality") if e.get("关联规则") == RULE_NO]
    acc = {e["样本编号"]: e for e in entries if e["字段名"] == "account"}
    weight = {e["样本编号"]: e for e in entries if e["字段名"] == "weight_g"}
    check("清单条目覆盖 2字段×4样本", len(entries) == 8, f"共{len(entries)}条")
    check("账号0012与12语义冲突→待核",
          acc["SMPL-9001"]["校验结论"] == "告警:语义冲突"
          and acc["SMPL-9002"]["校验结论"] == "告警:语义冲突"
          and acc["SMPL-9001"]["status"] == acc["SMPL-9002"]["status"] == "待核",
          acc["SMPL-9001"].get("待核原因", ""))
    check("前导零原值可从清单还原",
          quality_svc.restore_original(acc["SMPL-9001"]) == "0012"
          and acc["SMPL-9001"]["可还原"] == "是",
          f"原始值={acc['SMPL-9001']['原始值']}，转换值={acc['SMPL-9001']['转换值']}")
    check("重量单位安全换算",
          weight["SMPL-9001"]["校验结论"] == "通过"
          and weight["SMPL-9001"]["转换值"] == "1",
          f"1000克→{weight['SMPL-9001']['转换值']}（{weight['SMPL-9001']['转换方式']}）")
    check("字段缺失判定条件",
          acc["SMPL-9003"]["校验结论"] == "字段缺失"
          and "不存在" in acc["SMPL-9003"]["缺失判定"],
          acc["SMPL-9003"]["缺失判定"])
    check("实际空值判定条件",
          acc["SMPL-9004"]["校验结论"] == "实际空值"
          and "null" in acc["SMPL-9004"]["空值判定"],
          acc["SMPL-9004"]["空值判定"])
    check("缺失与空值判定条件不同",
          CRITERIA["字段缺失"] != CRITERIA["实际空值"]
          and acc["SMPL-9003"]["校验结论"] != acc["SMPL-9004"]["校验结论"])

    print("\n步骤5  每列实际启效的取值范围与清单原始值对照")
    ranges = quality_svc.verify_column_ranges(RULE_NO)
    check("账号列取值塌缩对不上",
          ranges["account"]["对得上"] is False, ranges["account"]["说明"])
    check("重量列取值一一对应",
          ranges["weight_g"]["对得上"] is True, ranges["weight_g"]["说明"])

    print("\n步骤6  回查：校验用过的样本和已验字段")
    trace = quality_svc.quality_trace(RULE_NO)
    check("样本可回查", len(trace["样本"]) == 4,
          "、".join(s["样本编号"] for s in trace["样本"]))
    check("已验字段可回查", len(trace["已验字段"]) == 8 and len(trace["待核项"]) == 2,
          f"待核{len(trace['待核项'])}项")

    print("\n步骤7  Schema差异确认：以清单告警字段确认新类型和来源，同步投递待办")
    diff_entry, message = diff_svc.run_action(diff["id"], "确认变更")
    print(f"  确认结果：{message}")
    new_def = json.loads(diff_entry["新定义"])
    todo = find(store.rows("data_delivery"), 数据来源=ENDPOINT)
    check("新类型和来源来自清单",
          "weight_kg" in new_def["已验字段类型"] and "字段转换质量清单" in new_def["来源"],
          diff_entry["新定义"])
    check("告警字段记录在差异上", diff_entry.get("告警字段") == "account")
    check("投递待办已同步",
          todo is not None and todo["status"] == "待配置" and todo["pending"],
          f"{todo and todo['管道编号']} 已验字段={todo and todo['已验字段']} 待核字段={todo and todo['待核字段']}")
    check("争议账号不进可发布范围",
          todo["已验字段"] == ["weight_kg"] and todo["待核字段"] == ["account"])

    print("\n步骤8  执行一笔投递，按已验字段写数据版本")
    delivery_svc.run_action(todo["id"], "启动投递")
    done_entry, message = delivery_svc.run_action(todo["id"], "暂停投递")
    version = max((v for v in store.rows("data_version")
                   if str(v.get("版本编号", "")).startswith("DATA-DELIV-")
                   and v.get("数据来源") == ENDPOINT),
                  key=lambda v: v["id"], default=None)
    payload = version.get("投递内容", []) if version else []
    row_1001 = find(payload, order_no="SO-1001") or {}
    quarantine_keys = {r.get("_quarantine", {}).get("account", {}).get("原值") for r in payload}
    check("数据版本已写入", version is not None and version["行数统计"] == 4,
          f"{version and version['版本编号']} 行数={version and version['行数统计']} Schema摘要={version and version['Schema摘要']}")
    check("重量作为1千克进入数据版本", row_1001.get("weight_kg") == 1,
          f"SO-1001.weight_kg={row_1001.get('weight_kg')}")
    check("两个账号没被当成同一订单",
          len(payload) == 4 and quarantine_keys == {"0012", "12", "<缺失>", "<空值>"},
          f"隔离原值={sorted(quarantine_keys)}")
    int_keys = {int(s["account"]) if str(s.get("account", "")).isdigit()
                else (0 if "account" not in s or s["account"] is None else s["account"])
                for _, s in samples}
    check("若按争议映射则会合并（对照）", len(int_keys) < 4,
          f"整数键={sorted(int_keys)} 只剩{len(int_keys)}个，订单会被合并")

    print("\n步骤9  修正账号映射（保留原始字符串）→ 重新校验 → 确认映射")
    mapping["fields"][0] = {"name": "account", "path": "$.account", "type": "string",
                            "identifier": True, "default": ""}
    rule["映射描述"] = json.dumps(mapping, ensure_ascii=False)
    quality_svc.run_action(acc["SMPL-9001"]["id"], "执行校验")
    for sample_no in ("SMPL-9001", "SMPL-9002"):
        entry = find(store.rows("field_quality"), 关联规则=RULE_NO, 字段名="account", 样本编号=sample_no)
        _, confirm_msg = quality_svc.run_action(entry["id"], "确认映射")
        print(f"  {sample_no}: {confirm_msg}")
    fixed_acc = {e["样本编号"]: e for e in store.rows("field_quality")
                 if e.get("关联规则") == RULE_NO and e["字段名"] == "account"}
    check("修正后账号映射通过",
          fixed_acc["SMPL-9001"]["校验结论"] == "通过"
          and fixed_acc["SMPL-9002"]["校验结论"] == "通过"
          and fixed_acc["SMPL-9001"]["转换方式"] == "保留原始字符串")
    check("原值保持原样",
          fixed_acc["SMPL-9001"]["原始值"] == "0012"
          and fixed_acc["SMPL-9001"]["转换值"] == "0012"
          and fixed_acc["SMPL-9002"]["转换值"] == "12")
    check("账号列取值范围对得上",
          quality_svc.verify_column_ranges(RULE_NO)["account"]["对得上"] is True,
          quality_svc.verify_column_ranges(RULE_NO)["account"]["说明"])

    print("\n步骤10  再次确认Schema差异，核对实际列、原值与待核结果")
    diff_entry, message = diff_svc.run_action(diff["id"], "确认变更")
    print(f"  再次确认：{message}")
    todo = find(store.rows("data_delivery"), 数据来源=ENDPOINT)
    trace = quality_svc.quality_trace(RULE_NO)
    check("实际列含账号与重量",
          todo["已验字段"] == ["account", "weight_kg"] and todo["待核字段"] == [],
          f"已验字段={todo['已验字段']}")
    check("待核结果清零", len(trace["待核项"]) == 0
          and all(e["status"] == "已确认" or e["校验结论"] != "告警:语义冲突"
                  for e in trace["已验字段"]))
    check("原值仍可还原", quality_svc.restore_original(fixed_acc["SMPL-9001"]) == "0012")

    delivery_svc.run_action(todo["id"], "启动投递")
    delivery_svc.run_action(todo["id"], "暂停投递")
    version2 = max((v for v in store.rows("data_version")
                    if str(v.get("版本编号", "")).startswith("DATA-DELIV-")
                    and v.get("数据来源") == ENDPOINT),
                   key=lambda v: v["id"])
    accounts = [r.get("account") for r in version2.get("投递内容", [])]
    check("再次投递的实际列含账号原值",
          accounts[:2] == ["0012", "12"] and "account" in version2["Schema摘要"],
          f"account列={accounts}，Schema摘要={version2['Schema摘要']}")

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"演练结束：{len(FAILURES)} 项未通过 → {FAILURES}")
        return 1
    print("演练结束：全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
