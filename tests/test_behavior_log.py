"""Tests for memory.behavior_log — append-only behavior event logging."""
from __future__ import annotations
import json
import pytest
from memory.cold.behavior_log import BehaviorLog

@pytest.fixture()
def blog(tmp_path):
    return BehaviorLog(str(tmp_path / "test.db"))

class TestBehaviorLog:
    def test_log_and_query(self, blog: BehaviorLog):
        blog.log("user1", "skill_call", {"skill": "weather", "params": {}})
        blog.log("user1", "conversation", {"turns": 3, "duration_s": 45})
        events = blog.get_events("user1", limit=10)
        assert len(events) == 2
        assert events[0]["event_type"] == "conversation"  # newest first
        assert events[1]["event_type"] == "skill_call"

    def test_empty_user(self, blog: BehaviorLog):
        events = blog.get_events("nobody", limit=10)
        assert events == []

    def test_filter_by_event_type(self, blog: BehaviorLog):
        blog.log("user1", "skill_call", {"skill": "weather"})
        blog.log("user1", "skill_call", {"skill": "news"})
        blog.log("user1", "conversation", {"turns": 2})
        events = blog.get_events("user1", event_type="skill_call")
        assert len(events) == 2
        assert all(e["event_type"] == "skill_call" for e in events)

    def test_get_events_since(self, blog: BehaviorLog):
        blog.log("user1", "skill_call", {"skill": "weather"})
        events = blog.get_events("user1", since_days=7)
        assert len(events) == 1

    def test_detail_is_json(self, blog: BehaviorLog):
        blog.log("user1", "skill_call", {"skill": "weather", "params": {"city": "Vancouver"}})
        events = blog.get_events("user1")
        detail = events[0]["detail"]
        assert isinstance(detail, dict)
        assert detail["skill"] == "weather"
