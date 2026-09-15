"""Recorded and scripted fakes for every test (CLAUDE.md §11: no test needs real hardware).

`fixtures/` holds recorded Redfish answers, ugly ones included; `bmc.py` holds the stateful
fake target and `FakeHal`, which plays back boots, applies planted failures at a chosen
cycle, and records every power action.
"""
