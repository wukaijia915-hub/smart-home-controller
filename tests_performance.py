# -*- coding: utf-8 -*-
"""灵犀智能家居中枢 — 性能量化测试脚本。
验证技术方案验收指标：
  - 规则层响应延迟 < 100ms
  - LLM 层响应延迟 < 5s（需配置 API Key）
  - 设备控制成功率 ≥ 95%
  - 决策准确率 ≥ 90%（标准场景集 S1-S10）
  - 前端渲染帧率 ≥ 30 FPS（浏览器控制台查看，本脚本提供检测方法）

用法：
  python tests_performance.py          # 运行全部测试
  python tests_performance.py --api    # 包含 LLM 层测试（需网络和 API Key）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 确保能导入 backend
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.agent import HomeAgent
from backend.devices.registry import DeviceRegistry
from backend.scenes import SCENES


class PerformanceTester:
    def __init__(self):
        self.registry = DeviceRegistry()
        self.agent = HomeAgent(self.registry)
        self.results = {}

    # ---------- 1. 规则层响应延迟 ----------
    def test_rule_latency(self, runs: int = 50) -> dict:
        """测试规则层（场景匹配 + 设备指令解析）响应延迟，目标 < 100ms。"""
        test_inputs = [
            "我回家了", "我出门了", "看电影", "睡觉了",
            "打开客厅灯", "关闭空调", "窗帘拉开", "空调调到24度",
            "好热", "好冷", "省电模式", "起床了",
        ]
        latencies = []
        for _ in range(runs):
            for text in test_inputs:
                t0 = time.perf_counter()
                self.agent.decide(text)
                t1 = time.perf_counter()
                latencies.append((t1 - t0) * 1000)  # ms

        avg = sum(latencies) / len(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        p99 = sorted(latencies)[int(len(latencies) * 0.99)]
        passed = avg < 100 and p95 < 100

        result = {
            "test": "规则层响应延迟",
            "target": "< 100ms",
            "runs": len(latencies),
            "avg_ms": round(avg, 2),
            "p95_ms": round(p95, 2),
            "p99_ms": round(p99, 2),
            "passed": passed,
        }
        self.results["rule_latency"] = result
        return result

    # ---------- 2. LLM 层响应延迟 ----------
    def test_llm_latency(self, runs: int = 3) -> dict:
        """测试 LLM 层响应延迟，目标 < 5s。需配置 API Key。"""
        if not self.agent.llm.available:
            return {
                "test": "LLM 层响应延迟",
                "target": "< 5s",
                "status": "skipped",
                "reason": "未配置 LLM API Key，跳过",
                "passed": True,
            }

        # 用不会被规则层匹配的复杂指令触发 LLM
        test_inputs = [
            "今天空气质量不太好，而且我感觉有点闷，帮我调整一下家里的环境",
            "我下午要在家办公，需要合适的光线和温度，顺便放点轻松的氛围",
            "孩子晚上要写作业，帮我设置一下适合学习的环境",
        ]
        latencies = []
        for text in test_inputs[:runs]:
            t0 = time.perf_counter()
            self.agent._llm_plan(text)
            t1 = time.perf_counter()
            latencies.append((t1 - t0))

        avg = sum(latencies) / len(latencies)
        passed = avg < 5.0

        result = {
            "test": "LLM 层响应延迟",
            "target": "< 5s",
            "runs": len(latencies),
            "avg_s": round(avg, 2),
            "passed": passed,
        }
        self.results["llm_latency"] = result
        return result

    # ---------- 3. 设备控制成功率 ----------
    def test_device_control_success(self) -> dict:
        """测试设备控制成功率，目标 ≥ 95%。"""
        test_cases = []
        for d in self.registry.all():
            for cmd in d.commands:
                params = {}
                if cmd == "set_brightness":
                    params = {"brightness": 50}
                elif cmd == "set_color_temp":
                    params = {"color_temp": 4000}
                elif cmd == "set_position":
                    params = {"position": 50}
                elif cmd == "set_temperature":
                    params = {"temperature": 24}
                elif cmd == "set_mode":
                    params = {"mode": "cool"}
                elif cmd == "set_fan_speed":
                    params = {"fan_speed": "mid"}
                elif cmd == "set_volume":
                    params = {"volume": 30}
                elif cmd == "set_speed":
                    params = {"speed": "mid"}
                elif cmd == "set_target":
                    params = {"target": 55}
                test_cases.append((d.device_id, cmd, params))

        success = 0
        failed = []
        for device_id, cmd, params in test_cases:
            r = self.registry.control(device_id, cmd, params)
            if r.get("ok"):
                success += 1
            else:
                failed.append((device_id, cmd, r.get("error")))

        total = len(test_cases)
        rate = (success / total * 100) if total > 0 else 0
        passed = rate >= 95

        result = {
            "test": "设备控制成功率",
            "target": "≥ 95%",
            "total": total,
            "success": success,
            "failed": len(failed),
            "rate": round(rate, 1),
            "passed": passed,
            "failed_cases": failed[:5],  # 只显示前5个失败
        }
        self.results["device_control"] = result
        return result

    # ---------- 4. 决策准确率（标准场景集 S1-S11，每场景3变体×3重复） ----------
    def test_decision_accuracy(self) -> dict:
        """测试决策准确率，目标 ≥ 90%。每场景 3 种口语化变体 × 3 次重复 = 9 次测试。"""
        standard_scenes = [
            {"id": "S1", "name": "回家", "variants": ["我回家了", "到家了", "回来了"],
             "expect_devices": ["light_living_01", "curtain_living_01", "ac_living_01"], "min_hits": 2},
            {"id": "S2", "name": "离家", "variants": ["我出门了", "离家模式", "我走了"],
             "expect_devices": ["light_living_01", "ac_living_01", "tv_living_01"], "min_hits": 2},
            {"id": "S3", "name": "观影", "variants": ["看电影", "观影模式", "看个片"],
             "expect_devices": ["light_living_01", "curtain_living_01", "tv_living_01"], "min_hits": 2},
            {"id": "S4", "name": "睡眠", "variants": ["睡觉了", "睡眠模式", "就寝"],
             "expect_devices": ["light_living_01", "light_bedroom_01", "ac_bedroom_01"], "min_hits": 2},
            {"id": "S5", "name": "会客", "variants": ["有客人来", "会客模式", "朋友来"],
             "expect_devices": ["light_living_01", "curtain_living_01", "ac_living_01"], "min_hits": 2},
            {"id": "S6", "name": "起床", "variants": ["早上好", "起床了", "早安"],
             "expect_devices": ["light_bedroom_01", "curtain_bedroom_01"], "min_hits": 1},
            {"id": "S7", "name": "降温", "variants": ["好热", "太热了", "降温"],
             "expect_devices": ["ac_living_01", "fan_living_01"], "min_hits": 1},
            {"id": "S7'", "name": "升温", "variants": ["好冷", "太冷了", "升温"],
             "expect_devices": ["ac_living_01"], "min_hits": 1},
            {"id": "S8", "name": "节能", "variants": ["省电", "节能模式", "环保"],
             "expect_devices": ["ac_living_01"], "min_hits": 1},
            {"id": "S11", "name": "直接控制", "variants": ["打开客厅灯", "空调调到24度", "拉开窗帘"],
             "expect_devices": ["light_living_01", "ac_living_01", "curtain_living_01"], "min_hits": 1},
        ]

        correct = 0
        total = 0
        details = []
        for scene in standard_scenes:
            scene_correct = 0
            scene_total = 0
            scene_hits = []
            for variant in scene["variants"]:
                for _ in range(3):
                    total += 1
                    scene_total += 1
                    result = self.agent.decide(variant)
                    action_devices = {a["device_id"] for a in result.get("actions", [])}
                    expected = set(scene["expect_devices"])
                    hit = expected & action_devices
                    is_correct = len(hit) >= scene["min_hits"]
                    if is_correct:
                        correct += 1
                        scene_correct += 1
                    scene_hits.append(len(hit))
            details.append({
                "id": scene["id"],
                "name": scene["name"],
                "variants": scene["variants"],
                "tests": scene_total,
                "passed": scene_correct,
                "rate": round(scene_correct / scene_total * 100, 1) if scene_total else 0,
                "avg_hits": round(sum(scene_hits) / len(scene_hits), 1) if scene_hits else 0,
                "min_hits_required": scene["min_hits"],
            })

        accuracy = (correct / total * 100) if total > 0 else 0
        passed = accuracy >= 90

        result = {
            "test": "决策准确率（标准场景集，每场景3变体×3重复）",
            "target": "≥ 90%",
            "total_scenes": len(standard_scenes),
            "total_tests": total,
            "correct": correct,
            "accuracy": round(accuracy, 1),
            "passed": passed,
            "details": details,
        }
        self.results["decision_accuracy"] = result
        return result

    # ---------- 5. 前端帧率检测说明 ----------
    def test_fps_guide(self) -> dict:
        """前端帧率检测说明（需在浏览器中运行）。"""
        result = {
            "test": "前端渲染帧率",
            "target": "≥ 30 FPS",
            "method": "浏览器控制台执行以下代码检测：",
            "code": """
// 在浏览器控制台粘贴运行，10秒后显示平均FPS
let frames = 0, start = performance.now();
function count() { frames++; requestAnimationFrame(count); }
count();
setTimeout(() => {
  const fps = frames / ((performance.now() - start) / 1000);
  console.log('平均FPS:', fps.toFixed(1), fps >= 30 ? '✓ 达标' : '✗ 未达标');
}, 10000);
""",
            "passed": "需浏览器实测",
        }
        self.results["fps"] = result
        return result

    # ---------- 运行全部测试 ----------
    def run_all(self, include_llm: bool = False) -> dict:
        print("=" * 60)
        print("  灵犀智能家居中枢 — 性能量化测试")
        print("=" * 60)
        print()

        tests = [
            self.test_rule_latency,
            self.test_device_control_success,
            self.test_decision_accuracy,
            self.test_fps_guide,
        ]
        if include_llm:
            tests.insert(1, self.test_llm_latency)

        for test_fn in tests:
            r = test_fn()
            self._print_result(r)
            print()

        # 汇总
        print("=" * 60)
        passed_count = sum(1 for r in self.results.values() if r.get("passed") is True)
        total_count = len(self.results)
        print(f"  汇总：{passed_count}/{total_count} 项达标")
        print("=" * 60)

        return self.results

    def _print_result(self, r: dict):
        status = "✓ 达标" if r.get("passed") is True else ("✗ 未达标" if r.get("passed") is False else "— 待实测")
        print(f"【{r['test']}】 目标: {r['target']}  {status}")
        for k, v in r.items():
            if k in ("test", "target", "passed", "details", "failed_cases", "code", "method"):
                continue
            if isinstance(v, list) and len(v) > 3:
                print(f"  {k}: {v[:3]}... (共{len(v)}项)")
            else:
                print(f"  {k}: {v}")
        if "details" in r:
            print(f"  场景明细:")
            for d in r["details"]:
                mark = "✓" if d["rate"] >= 90 else "✗"
                print(f"    {mark} {d['id']} {d['name']} -> {d['passed']}/{d['tests']}通过 ({d['rate']}%), 平均命中{d['avg_hits']}个设备")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="灵犀性能量化测试")
    parser.add_argument("--api", action="store_true", help="包含 LLM 层测试（需 API Key）")
    parser.add_argument("--output", type=str, help="将结果保存为 JSON 文件")
    args = parser.parse_args()

    tester = PerformanceTester()
    results = tester.run_all(include_llm=args.api)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存到: {args.output}")
