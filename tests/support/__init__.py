"""Shared test support: fakes, builders and rows that more than one test file uses.

A test file never imports from another test file; what two of them share lives here, under a
public name, and ``tests/canary/test_canary_no_test_file_imports_another.py`` holds the line.
"""
