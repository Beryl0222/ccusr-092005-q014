import unittest

from project_data import load_seed


class SeedDataTest(unittest.TestCase):
    def test_seed_has_named_records(self) -> None:
        data = load_seed()
        self.assertTrue(data["project"])
        self.assertTrue(data["records"])
        for record in data["records"]:
            self.assertTrue(record["id"])


if __name__ == "__main__":
    unittest.main()
