"""Offline checks for the x100 snapshot generator: determinism, structure,
tree wiring, and id hygiene. Runs against the real baseline snapshot — no
cluster, no parameters."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP.SNAPSHOT"
GENERATED = ROOT / "server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP-x100.SNAPSHOT"

spec = importlib.util.spec_from_file_location(
    "genx100", ROOT / "scripts/generate-x100-snapshot.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

AUD_BASE = "https://agentgateway-stack-ag.ping-devops.com/mcp/portfolio"
GDP_ID = "a7c4e2d0-8f11-4b5a-9c3d-6e8f2a1b4c7d"
PORTFOLIO_ID = "59473014-ee49-41c7-8f65-4bfb8388e8e6"
AGENTGATEWAY_ID = "35d4ec64-e0ce-498d-b4b8-155436bb19ba"
LEAF_ATTR = "354ae71a-f26d-4449-aa8b-f203c1e92d08"


def by_id(elements):
    return {e["id"]: e for e in elements if isinstance(e, dict) and e.get("id")}


class X100SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before_hash = hashlib.sha256(BASELINE.read_bytes()).hexdigest()
        if not GENERATED.exists():
            gen.main()
        cls.x100 = json.loads(GENERATED.read_text())
        cls.by_id = by_id(cls.x100)
        cls.baseline_ids = {
            e["id"] for e in json.loads(BASELINE.read_text())
            if isinstance(e, dict) and e.get("id")
        }

    def test_baseline_untouched(self):
        self.assertEqual(
            self.before_hash, hashlib.sha256(BASELINE.read_bytes()).hexdigest())

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "regen.SNAPSHOT"
            gen.generate(out)
            first = out.read_bytes()
            out.write_bytes(b"stale")
            gen.generate(out)
            self.assertEqual(first, out.read_bytes())
            self.assertEqual(first, GENERATED.read_bytes())

    def test_structure(self):
        self.assertIsInstance(self.x100, list)
        # 161 originals + 99 branches x 39 elements (38 subtree clones + 1
        # audience attribute each).
        self.assertEqual(len(self.x100), 161 + 99 * 39)
        self.assertEqual(self.by_id[GDP_ID]["name"], "Global Decision Point")
        headers = [e for e in self.x100
                   if isinstance(e, dict) and e.get("type") == "SnapshotPackageFile$PackageHeader"]
        self.assertEqual(len(headers), 1)
        footers = [e for e in self.x100
                   if isinstance(e, dict) and e.get("@class") == "DataStreamFooter"]
        self.assertEqual(len(footers), 1)
        self.assertIs(self.x100[-1], footers[0])

    def test_tree(self):
        ag = self.by_id[AGENTGATEWAY_ID]
        self.assertEqual(len(ag["children"]), 100)
        self.assertEqual(ag["children"][0]["id"], PORTFOLIO_ID)
        for i, child in enumerate(ag["children"], start=1):
            self.assertEqual(child["type"], "PolicySet")
        # Branch 1 keeps the original leaf; branches 2..100 carry portfolio-<i>.
        self.assertEqual(
            self._leaf_value(self.by_id[PORTFOLIO_ID]), "portfolio")
        for i in range(2, 101):
            clone = self.by_id[ag["children"][i - 1]["id"]]
            self.assertEqual(self._leaf_value(clone), f"portfolio-{i}")
            aud = self._audience_value(clone)
            self.assertEqual(
                aud, AUD_BASE if i == 1 else f"{AUD_BASE}-{i}")

    def _leaf_value(self, policy_set):
        leaf_id = LEAF_ATTR
        for cond in policy_set["condition"]["and"]["conditions"]:
            cmp_ = cond.get("comparison")
            if cmp_ and cmp_["left"]["attribute"]["id"] == leaf_id:
                return cmp_["right"]["constant"]["value"]
        self.fail(f"no leaf condition on {policy_set.get('name')}")

    def _audience_value(self, policy_set):
        # The branch's audience constant: find the R1 rule under its Allow
        # Read Tools policy, read the attribute id it compares, look it up.
        read_policy = next(
            self.by_id[c["id"]] for c in policy_set["children"]
            if self.by_id[c["id"]]["name"] == "Allow Read Tools")
        aud_rule = next(
            self.by_id[r["id"]] for r in read_policy["children"]
            if self.by_id[r["id"]]["name"].startswith("R1 audience"))
        cmp_ = aud_rule["effectSettings"]["condition"]["and"]["conditions"][0]["comparison"]
        aud_attr_id = cmp_["right"]["attribute"]["id"]
        aud_attr = self.by_id[aud_attr_id]
        self.assertEqual(aud_attr["objectType"], "AttributeDefinition")
        return aud_attr["resolvers"][0]["value"]

    def test_no_id_collisions(self):
        x100_ids = set(self.by_id.keys())
        originals_in_x100 = self.baseline_ids & x100_ids
        cloned = x100_ids - self.baseline_ids
        self.assertEqual(len(cloned) + len(originals_in_x100), len(x100_ids))
        # Clone ids are unique (guaranteed by dict keys) and disjoint from
        # every baseline id.
        self.assertEqual(cloned & self.baseline_ids, set())
        # 39 per branch x 99 = 3855 cloned elements carrying ids.
        self.assertEqual(len(cloned), 99 * 39)
        # Every reference resolves inside the x100 element set.
        for e in self.x100:
            if not isinstance(e, dict) or "id" not in e:
                continue
            for ref in (e.get("parentId"), ):
                if ref:
                    self.assertIn(ref, self.by_id)
            for child in e.get("children") or []:
                self.assertIn(child["id"], self.by_id)
            for stmt in e.get("statements") or []:
                self.assertIn(stmt, self.by_id)


if __name__ == "__main__":
    unittest.main()
