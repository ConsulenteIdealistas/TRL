"""手順1の追加テスト。実行: python -m unittest test_entities -v"""
import unittest
import trl

ENT = {"entities": [
    {
        "id": "ex_corp",
        "name": "Example Corp",
        "aliases": ["Example Corporation", "エグザンプル"],
        "products": [
            {
                "id": "example_product",
                "name": "Example Device",
                "aliases": ["EX-100"]
            }
        ]
    },
    {"id": "foo_inc", "name": "Foo Inc", "aliases": []}
]}

class EntityTests(unittest.TestCase):
    def test_validate_ok(self):
        trl.validate_entities(ENT)

    def test_alias_collision(self):
        bad = {"entities": [{"id": "a", "name": "Alpha Co", "aliases": ["Shared Name"]},
                            {"id": "b", "name": "Beta Co", "aliases": ["shared  name"]}]}
        with self.assertRaises(trl.ConfigError):
            trl.validate_entities(bad)

    def test_short_alias_rejected(self):
        with self.assertRaises(trl.ConfigError):
            trl.validate_entities({"entities": [{"id": "a", "name": "AI"}]})

    def test_affiliation_is_strong(self):
        r = trl.link_entities({"title": "t", "abstract": "", "affiliations": ["R&D, Example Corp., Tokyo"]}, ENT)
        self.assertEqual([(x["entity_id"], x["strength"]) for x in r], [("ex_corp", "strong")])

    def test_abstract_only_is_weak(self):
        r = trl.link_entities({"title": "t", "abstract": "vs Foo Inc devices", "affiliations": []}, ENT)
        self.assertEqual([(x["entity_id"], x["strength"]) for x in r], [("foo_inc", "weak")])

    def test_word_boundary(self):
        self.assertEqual(trl.link_entities({"title": "Foo Incubator", "abstract": "", "affiliations": []}, ENT), [])

    def test_screen_modes(self):
        weak, strong = {"entities": [{"strength": "weak"}]}, {"entities": [{"strength": "strong"}]}
        self.assertIsNone(trl.entity_screen({}, "off"))
        self.assertEqual(trl.entity_screen(weak, "strong"), "no_linked_entity")
        self.assertIsNone(trl.entity_screen(strong, "strong"))
        self.assertIsNone(trl.entity_screen(weak, "any"))
        
    def test_product_link_is_not_company_link(self):
        links = trl.link_entities(
            {
                "title": "Example Device",
                "abstract": "",
                "affiliations": [],
            },
            ENT,
        )

        self.assertTrue(
            any(
                link["link_type"] == "product"
                and link["product_id"] == "example_product"
                for link in links
            )
        )
        self.assertEqual(
            trl.entity_screen({"entities": links}, "any"),
            "no_linked_entity",
        )

    def test_product_alias_matches(self):
        links = trl.link_entities(
            {
                "title": "EX-100 evaluation",
                "abstract": "",
                "affiliations": [],
            },
            ENT,
        )

        self.assertTrue(
            any(
                link["link_type"] == "product"
                and link["product_id"] == "example_product"
                for link in links
            )
        )

    def test_store_roundtrip_and_upgrade_only(self):
        st = trl.Store(":memory:")
        mk = lambda s, f: [{"entity_id": "e", "alias": "x", "field": f, "strength": s}]
        st.set_entities("c1", mk("weak", "abstract"))
        st.set_entities("c1", mk("strong", "affiliation"))
        st.set_entities("c1", mk("weak", "abstract"))
        self.assertEqual(st.get_entities("c1")[0]["strength"], "strong")


if __name__ == "__main__":
    unittest.main()
