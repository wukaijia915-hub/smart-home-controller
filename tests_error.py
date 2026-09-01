# -*- coding: utf-8 -*-
"""异常输入与边界测试：验证系统对非法/越界/矛盾/模糊输入的鲁棒性。
参考 openvela-smarthome-agent 的 error_cases 设计，覆盖 7 类异常场景。
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.devices.registry import DeviceRegistry
from backend.agent import HomeAgent
from backend.tools import tool_registry


class ErrorTestSuite:
    def __init__(self):
        self.registry = DeviceRegistry()
        self.agent = HomeAgent(self.registry)
        tool_registry.build_from_devices(self.registry)
        self.results = []

    def _record(self, category: str, case: str, input_data, expected: str, actual: str, passed: bool):
        self.results.append({
            "category": category,
            "case": case,
            "input": str(input_data)[:80],
            "expected": expected,
            "actual": actual[:120],
            "passed": passed,
        })
        mark = "✓" if passed else "✗"
        print(f"  {mark} [{category}] {case}: {actual[:80]}")

    # ---------- 1. 空指令 / 无意义输入 ----------
    def test_empty_input(self):
        print("\n【1】空指令 / 无意义输入")
        cases = [
            ("空字符串", "", "应返回兜底回复，不崩溃"),
            ("纯空格", "   ", "应返回兜底回复"),
            ("特殊符号", "!!!@@@###", "应返回兜底回复"),
            ("单字", "啊", "应返回兜底回复或理解"),
        ]
        for name, text, expected in cases:
            result = self.agent.decide(text)
            reply = result.get("reply", "")
            actions = result.get("actions", [])
            # 空输入不应执行任何设备操作
            passed = len(actions) == 0 and len(reply) > 0
            self._record("empty", name, text, expected, f"回复={reply[:40]}, 操作={len(actions)}个", passed)

    # ---------- 2. 未知设备 / 不存在的设备 ----------
    def test_unknown_device(self):
        print("\n【2】未知设备 / 不存在的设备")
        cases = [
            ("打开厨房灯", "厨房灯不存在时应兜底或忽略"),
            ("打开浴室的空调", "浴室空调不存在应兜底"),
            ("打开花园 sprinkler", "完全未知设备应兜底"),
            ("把车库门打开", "车库门不存在应兜底"),
        ]
        for text, expected in cases:
            result = self.agent.decide(text)
            actions = result.get("actions", [])
            # 未知设备不应导致错误，应优雅兜底
            passed = isinstance(actions, list)
            self._record("unknown_device", text, text, expected, f"操作={len(actions)}个, 回复={result.get('reply','')[:40]}", passed)

    # ---------- 3. 参数越界 ----------
    def test_out_of_range(self):
        print("\n【3】参数越界")
        cases = [
            ("空调调到100度", "温度应被限制在16-30℃"),
            ("空调调到-5度", "温度应被限制在16-30℃"),
            ("灯光调到200%", "亮度应被限制在0-100%"),
            ("灯光调到-10%", "亮度应被限制在0-100%"),
            ("风扇调到10档", "档位应被限制在1-3"),
        ]
        for text, expected in cases:
            result = self.agent.decide(text)
            actions = result.get("actions", [])
            # 检查执行后设备状态是否在合法范围内
            all_valid = True
            for action in actions:
                did = action.get("device_id")
                dev = self.registry.get(did)
                if dev:
                    for attr in dev.attributes.values():
                        if attr.type == "number" and attr.min is not None and attr.max is not None:
                            if not (attr.min <= attr.value <= attr.max):
                                all_valid = False
            self._record("out_of_range", text, text, expected, f"操作={len(actions)}个, 范围合法={all_valid}", all_valid)

    # ---------- 4. 矛盾指令 ----------
    def test_conflicting_commands(self):
        print("\n【4】矛盾指令")
        cases = [
            ("打开客厅灯然后关掉", "应执行最后一个或不冲突"),
            ("空调同时制冷和制热", "应选择一个模式或兜底"),
            ("把窗帘全部打开然后全部关闭", "应执行最后状态"),
        ]
        for text, expected in cases:
            result = self.agent.decide(text)
            actions = result.get("actions", [])
            # 矛盾指令不应崩溃，应给出合理结果
            passed = isinstance(actions, list) and len(result.get("reply", "")) > 0
            self._record("conflict", text, text, expected, f"操作={len(actions)}个, 回复={result.get('reply','')[:40]}", passed)

    # ---------- 5. 模糊指令 ----------
    def test_vague_input(self):
        print("\n【5】模糊指令")
        cases = [
            ("帮我弄一下", "过于模糊应兜底"),
            ("那个东西调一下", "指代不明应兜底"),
            ("舒服一点", "主观描述应尝试理解或兜底"),
            ("太那个了", "完全模糊应兜底"),
        ]
        for text, expected in cases:
            result = self.agent.decide(text)
            # 模糊指令不应崩溃
            passed = isinstance(result, dict) and "reply" in result
            self._record("vague", text, text, expected, f"回复={result.get('reply','')[:50]}", passed)

    # ---------- 6. 工具注册表参数校验 ----------
    def test_tool_validation(self):
        print("\n【6】工具注册表参数校验")
        # 测试工具调用的参数校验
        test_cases = [
            ("未知工具", {"name": "nonexistent_tool", "params": {}}, "应返回未知工具错误"),
            ("缺少必填参数", {"name": "set_客厅_light", "params": {}}, "应返回缺少参数错误"),
            ("枚举值无效", {"name": "set_客厅_ac", "params": {"power": "invalid"}}, "应返回枚举错误"),
            ("数值越界", {"name": "set_客厅_ac", "params": {"power": "on", "target_temp": 100}}, "应返回越界错误"),
        ]
        for name, params, expected in test_cases:
            result = tool_registry.call(params["name"], params.get("params", {}))
            passed = not result.get("ok", True)  # 异常输入应返回 ok=False
            self._record("tool_validation", name, params, expected, f"ok={result.get('ok')}, error={result.get('error','')[:60]}", passed)

    # ---------- 7. 超长输入 ----------
    def test_very_long_input(self):
        print("\n【7】超长输入")
        long_text = "打开客厅灯" * 100  # 600字
        result = self.agent.decide(long_text)
        passed = isinstance(result, dict) and "reply" in result
        self._record("long_input", "600字重复指令", long_text[:30] + "...", "应不崩溃，正常处理", f"回复长度={len(result.get('reply',''))}", passed)

    # ---------- 运行全部 ----------
    def run_all(self):
        print("=" * 60)
        print("  灵犀 — 异常输入与边界测试")
        print("=" * 60)
        self.test_empty_input()
        self.test_unknown_device()
        self.test_out_of_range()
        self.test_conflicting_commands()
        self.test_vague_input()
        self.test_tool_validation()
        self.test_very_long_input()

        # 汇总
        print("\n" + "=" * 60)
        total = len(self.results)
        passed = sum(1 for r in self.results if r["passed"])
        print(f"  汇总：{passed}/{total} 项通过 ({passed/total*100:.1f}%)")
        print("=" * 60)

        # 保存结果
        output = {"total": total, "passed": passed, "pass_rate": f"{passed/total*100:.1f}%", "details": self.results}
        with open(os.path.join(os.path.dirname(__file__), "error_test_results.json"), "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存到 error_test_results.json")
        return output


if __name__ == "__main__":
    suite = ErrorTestSuite()
    suite.run_all()
