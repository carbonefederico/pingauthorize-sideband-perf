"""Offline checks for the generator's --services mode (Ping-recommended
dispatch shape): 100 ServiceDefinitions, service-targeted set clones, no
leaf conditions on clones. Runs against the real baseline snapshot."""
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICIES = ROOT / "server-profiles/pingauthorizepap/policies"
BASELINE = POLICIES / "AgentGatewayPortfolioMCP.SNAPSHOT"
COND_X100 = POLICIES / "AgentGatewayPortfolioMCP-x100.SNAPSHOT"
SERVICES_X100 = POLICIES / "AgentGatewayPortfolioMCP-x100-services.SNAPSHOT"

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


class X100ServicesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (BASELINE, COND_X100) if p.exists()
        }
        gen.main(argv=["--services"])
        cls.x = json.loads(SERVICES_X100.read_text())
        cls.ids = by_id(cls.x)

    def test_baseline_untouched(self):
        for name, digest in self.before.items():
            self.assertEqual(digest, hashlib.sha256((POLICIES / name).read_bytes()).hexdigest())

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "regen.SNAPSHOT"
            gen.generate(out, services=True)
            first = out.read_bytes()
            gen.generate(out, services=True)
            self.assertEqual(first, out.read_bytes())
            self.assertEqual(first, SERVICES_X100.read_bytes())

    def test_structure(self):
        self.assertIsInstance(self.x, list)
        # 161 originals + 99 branches x 41 (38 subtree clones + audience attr
        # + ServiceDefinition + Target).
        self.assertEqual(len(self.x), 161 + 99 * 41)
        self.assertEqual(self.ids[GDP_ID]["name"], "Global Decision Point")
        footers = [e for e in self.x if isinstance(e, dict) and e.get("@class") == "DataStreamFooter"]
        self.assertEqual(len(footers), 1)
        self.assertIs(self.x[-1], footers[0])

    def test_wrapper_keeps_all_branches(self):
        # Both modes keep all 100 sets under the one AgentGateway wrapper;
        # service targeting selects among them, not tree position.
        wrapper = self.ids[AGENTGATEWAY_ID]
        self.assertEqual(len(wrapper["children"]), 100)

    def test_tree(self):
        services = {e["name"]: e for e in self.x
                    if isinstance(e, dict) and e.get("objectType") == "ServiceDefinition"}
        for i in range(2, 101):
            name = f"AgentGateway portfolio-{i}"
            self.assertIn(name, services, f"missing ServiceDefinition {name}")
            svc = services[name]
            # The branch's Portfolio set clone: find via its audience rule.
            branch_set = self._branch_set(i)
            self.assertEqual(branch_set["condition"], {"empty": {}})
            # Service-targeted: targets -> Target element -> the service id.
            targets = branch_set.get("targets") or []
            self.assertEqual(len(targets), 1)
            target = self.ids[targets[0]]
            self.assertEqual(target.get("type"), "Target")
            self.assertEqual(target["targeted"]["services"], [svc["id"]])
            # No leaf condition anywhere in the clone subtree.
            self.assertNotIn(LEAF_ATTR, json.dumps(branch_set["condition"]))
            # Audience constant per branch.
            aud = self._audience_value(branch_set)
            self.assertEqual(aud, f"{AUD_BASE}-{i}")

    def _branch_set(self, i):
        return self.ids[str(gen.services_set_id(i))]

    def _audience_value(self, policy_set):
        read_policy = next(
            self.ids[c["id"]] for c in policy_set["children"]
            if self.ids[c["id"]]["name"] == "Allow Read Tools")
        aud_rule = next(
            self.ids[r["id"]] for r in read_policy["children"]
            if self.ids[r["id"]]["name"].startswith("R1 audience"))
        cmp_ = aud_rule["effectSettings"]["condition"]["and"]["conditions"][0]["comparison"]
        return self.ids[cmp_["right"]["attribute"]["id"]]["resolvers"][0]["value"]

    def test_wrapper_unchanged(self):
        wrapper = self.ids[AGENTGATEWAY_ID]
        # The wrapper's FIRST child stays the original condition-gated set.
        self.assertEqual(wrapper["children"][0]["id"], PORTFOLIO_ID)
        gdp = self.ids[GDP_ID]
        self.assertEqual(gdp["children"][0]["id"], AGENTGATEWAY_ID)


if __name__ == "__main__":
    unittest.main()
