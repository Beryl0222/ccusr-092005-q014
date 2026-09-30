import unittest

from project_data import load_seed


class SeedDataTest(unittest.TestCase):
    def test_seed_has_domain_collections(self) -> None:
        data = load_seed()
        self.assertTrue(data["project"])
        for key in ("enterprises", "regions", "medicines", "production_lines",
                    "line_licenses", "lanes", "initial_reports"):
            self.assertIn(key, data)
            self.assertIsInstance(data[key], list)
            self.assertTrue(data[key], f"{key} 不应为空")

    def test_initial_reports_carry_source_time_and_reporter(self) -> None:
        data = load_seed()
        for rep in data["initial_reports"]:
            self.assertTrue(rep["source_ts"])
            self.assertTrue(rep["reporter"])
            for line in rep["lines"]:
                # 理论产能、待检成品、在制品分项上报，不允许混为一个字段
                for field in ("theoretical_capacity", "awaiting_qc_qty", "wip_qty"):
                    self.assertIn(field, line)


if __name__ == "__main__":
    unittest.main()
