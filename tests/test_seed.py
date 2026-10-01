import unittest

from project_data import load_seed


class SeedDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data = load_seed()

    def test_seed_has_named_records(self) -> None:
        self.assertTrue(self.data["project"])
        self.assertTrue(self.data["records"])

    def test_seed_contains_core_domain_records(self) -> None:
        kinds = {r["kind"] for r in self.data["records"]}
        for required in (
            "medicine", "enterprise", "user", "region", "production_line",
            "license", "changeover", "downtime", "material", "transit",
            "material_requirement", "material_supply", "batch",
        ):
            self.assertIn(required, kinds)

    def test_batches_reference_real_lines_and_enterprises(self) -> None:
        records = self.data["records"]
        lines = {r["id"] for r in records if r["kind"] == "production_line"}
        for rec in records:
            if rec["kind"] == "batch":
                self.assertIn(rec["line_id"], lines)
                self.assertIn(rec["qc_status"], ("pending", "released", "rejected"))


if __name__ == "__main__":
    unittest.main()
