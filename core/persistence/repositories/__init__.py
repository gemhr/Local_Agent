#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""narrow repository 层。

每个 repository 只接收调用方提供的 ``AsyncSession`` 并执行单条语句级操作：
不 commit、不 rollback、不开启事务、不创建 Session。事务 Owner 是
Application Service（``async with database.transaction():``）。

本项目**不**提供 GenericBaseRepository / UniversalDAO / 万能 CRUD 框架。
"""
