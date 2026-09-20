"""Shared building blocks for the OceanBase dual-mode driver POC.

This package holds what both compatibility-mode suites depend on: environment-backed
connection configuration, credential redaction, the result contract, the shared error
vocabulary, and the check harness.

Nothing here talks to a database. The driver-specific code lives in
:mod:`mysql_mode` and :mod:`oracle_mode`.
"""
