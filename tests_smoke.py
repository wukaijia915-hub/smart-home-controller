# -*- coding: utf-8 -*-
"""临时冒烟测试：设备层 + 规则 Agent 决策。"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from backend.devices.registry import DeviceRegistry
from backend.agent import HomeAgent

reg = DeviceRegistry()
agent = HomeAgent(reg)
print('设备数:', len(reg.all()))
for t in ['我回家了', '看电影', '打开卧室灯', '空调调到24度', '打开窗帘', '太热了', '电视音量30', '关灯', '你好呀']:
    r = agent.decide(t)
    acts = [(a['device_id'], a['command']) for a in r['actions']]
    print(f'[{t}] -> {r["reply"][:30]} | actions={acts}')
