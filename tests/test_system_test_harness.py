"""Unit tests for harness diff/reset logic (no real JarvisApp needed)."""
from __future__ import annotations

from system_tests.harness import diff_devices, diff_memory


class TestDiffDevices:
    def test_no_change(self):
        before = {"bedroom_light": {"is_on": False, "brightness": 100}}
        after = {"bedroom_light": {"is_on": False, "brightness": 100}}
        changes = diff_devices(before, after)
        assert changes == []

    def test_single_change(self):
        before = {"bedroom_light": {"is_on": False, "brightness": 100}}
        after = {"bedroom_light": {"is_on": True, "brightness": 100}}
        changes = diff_devices(before, after)
        assert len(changes) == 1
        assert changes[0].device_id == "bedroom_light"
        assert changes[0].field == "is_on"
        assert changes[0].before is False
        assert changes[0].after is True

    def test_multiple_changes(self):
        before = {"bedroom_light": {"is_on": False, "brightness": 100}}
        after = {"bedroom_light": {"is_on": True, "brightness": 50}}
        changes = diff_devices(before, after)
        assert len(changes) == 2

    def test_multiple_devices(self):
        before = {
            "bedroom_light": {"is_on": False},
            "living_room_light": {"is_on": False},
        }
        after = {
            "bedroom_light": {"is_on": True},
            "living_room_light": {"is_on": True},
        }
        changes = diff_devices(before, after)
        assert len(changes) == 2

    def test_ignores_metadata_fields(self):
        """device_id, name, device_type, required_role, is_available are metadata — skip."""
        before = {"bedroom_light": {"device_id": "bedroom_light", "name": "卧室灯", "is_on": False}}
        after = {"bedroom_light": {"device_id": "bedroom_light", "name": "卧室灯", "is_on": True}}
        changes = diff_devices(before, after)
        assert len(changes) == 1
        assert changes[0].field == "is_on"


class TestDiffMemory:
    def test_no_change(self):
        before = [{"id": "a", "content": "likes coffee", "category": "preference", "key": "drink"}]
        after = [{"id": "a", "content": "likes coffee", "category": "preference", "key": "drink"}]
        diff = diff_memory(before, after)
        assert diff.is_empty

    def test_added(self):
        before = []
        after = [{"id": "a", "content": "likes coffee", "category": "preference", "key": "drink"}]
        diff = diff_memory(before, after)
        assert len(diff.added) == 1
        assert diff.added[0].content == "likes coffee"

    def test_removed(self):
        before = [{"id": "a", "content": "likes coffee", "category": "preference", "key": "drink"}]
        after = []
        diff = diff_memory(before, after)
        assert len(diff.removed) == 1
