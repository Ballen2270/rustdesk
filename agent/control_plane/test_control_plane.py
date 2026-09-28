import unittest

from agent.control_plane.control_plane import ControlPlane


class ControlPlaneTests(unittest.TestCase):
    def request(self, **overrides):
        return {
            "skill_id": "rustdesk-jianhui",
            "device_id": "pc1",
            "surface_ids": ["pc1:display-1", "pc1:display-2"],
            "target": 10,
            "models": {"design": "openai-codex", "exception": "anthropic-claude"},
            **overrides,
        }

    def test_input_lease_is_per_physical_host(self):
        plane = ControlPlane()
        first = plane.create_run(self.request())
        second = plane.create_run(self.request())
        self.assertEqual(plane.control(first.id, "start").status, "running")
        with self.assertRaisesRegex(ValueError, "leased"):
            plane.control(second.id, "start")
        self.assertEqual(plane.control(first.id, "stop").status, "stopped")
        self.assertEqual(plane.control(second.id, "start").status, "running")

    def test_invalid_surface_and_models_are_rejected(self):
        plane = ControlPlane()
        with self.assertRaisesRegex(ValueError, "invalid surfaces"):
            plane.create_run(self.request(surface_ids=["pc1:unknown"]))
        with self.assertRaisesRegex(ValueError, "cannot fill execute"):
            plane.create_run(self.request(models={"execute": "openai-codex"}))

    def test_skill_draft_never_publishes_or_executes(self):
        plane = ControlPlane()
        draft = plane.create_draft({"name": "测试", "logic": "状态 A 到 B", "operation": "点击按钮", "images": ["a.png"]})
        self.assertEqual(draft["status"], "needs-review")


if __name__ == "__main__":
    unittest.main()
