# -*- coding: utf-8 -*-
"""角色化多智能体包：RootAgent（编排 + 授权） + ManagerAgent（校验 + 执行）。"""
from .root_agent import RootAgent, SubTask, TaskPlan
from .manager_agent import ManagerAgent, ExecutionReport

__all__ = ["RootAgent", "SubTask", "TaskPlan", "ManagerAgent", "ExecutionReport"]
