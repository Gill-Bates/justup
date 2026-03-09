#!/usr/bin/env python3
#
# app/services/__init__.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Services layer for business logic.

Ownership model:
- Scheduler: writes status and metrics
- API/Frontend: reads status and metrics
- Services: shared business logic
"""
