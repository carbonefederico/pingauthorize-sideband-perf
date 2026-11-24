#!/usr/bin/env python3
"""generate-x100-snapshot.py — build the 100-branch policy-tree snapshot.

Reads the baseline AgentGatewayPortfolioMCP.SNAPSHOT (the campaign of
record's single MCP branch) and emits AgentGatewayPortfolioMCP-x100.SNAPSHOT:
the original `Portfolio MCP` set plus 99 clones, one per synthetic MCP
(`portfolio-2` .. `portfolio-100`), each an identical 6-policy/16-rule
subtree gated on its own URL leaf and carrying its own audience constant.
The Global Decision Point id is reused, so the packaging root needs no
parameterization — the two snapshots never coexist in a loaded package.

Deterministic by construction: every cloned id is uuid5 over a fixed
namespace and the branch index, so regeneration is byte-identical. The
baseline snapshot is never modified.

Usage:
  python3 scripts/generate-x100-snapshot.py             # condition-gated x100
  python3 scripts/generate-x100-snapshot.py --services  # service-targeted x100

Services mode emits the Ping-recommended dispatch shape instead: per branch
a ServiceDefinition named AgentGateway.portfolio-<i> (matching what a
parameterized sideband endpoint's service:AgentGateway.{mcp} derives), a
Target element for it, and the Portfolio set clone targeted at that service
with NO leaf condition. Dispatch then happens at the endpoint layer; the
tree carries no URL conditions on the clones.
"""
import argparse
import copy
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP.SNAPSHOT"
OUTPUT = ROOT / "server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP-x100.SNAPSHOT"
OUTPUT_SERVICES = ROOT / "server-profiles/pingauthorizepap/policies/AgentGatewayPortfolioMCP-x100-services.SNAPSHOT"

BRANCH_COUNT = 100
BRANCH_1_LEAF = "portfolio"
# Fixed namespace: any constant works; uuid5(key) is then deterministic
# across runs and machines for the same (branch, role, original-id) key.
UUID_NAMESPACE = uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")

# Ids inside the baseline snapshot (verified before writing this generator):
PORTFOLIO_ID = "59473014-ee49-41c7-8f65-4bfb8388e8e6"
AGENTGATEWAY_ID = "35d4ec64-e0ce-498d-b4b8-155436bb19ba"
AUDIENCE_ATTR_ID = "183b9113-9fba-4127-8cb5-7c1312e47429"
LEAF_ATTR_ID = "354ae71a-f26d-4449-aa8b-f203c1e92d08"
CONSTANTS_FOLDER_ID = "f22540f5-62fa-4acc-b511-783d797cc7b8"


def clone_id(branch_index, original_id, namespace=UUID_NAMESPACE):
    return str(uuid.uuid5(namespace, f"{branch_index}:{original_id}"))


def services_namespace():
    """Salted namespace for services-mode ids so the two snapshot variants
    never share an id."""
    return uuid.uuid5(UUID_NAMESPACE, "services-mode")


def services_set_id(branch_index):
    """The services-mode clone's Portfolio-set id — exported for tests."""
    return clone_id(branch_index, PORTFOLIO_ID, services_namespace())


def clone_key(element):
    """The id-bearing sub-objects of one element that are cloned together
    with it: children ids are rebuilt by re-walking the subtree; statements
    and targets are themselves elements in the stream, so only the top-level
    id and version need rewriting here."""
    return element


def reachable_subtree(elements, root_id):
    """Ids reachable from root_id via children/statement id references."""
    by_id = {e["id"]: e for e in elements if isinstance(e, dict) and e.get("id")}
    reach, stack = set(), [root_id]
    while stack:
        i = stack.pop()
        if i in reach:
            continue
        reach.add(i)
        e = by_id.get(i, {})
        for child in e.get("children") or []:
            stack.append(child["id"])
        for stmt in e.get("statements") or []:
            stack.append(stmt)
    return reach


def audience_value(branch_index):
    base = f"https://agentgateway-stack-ag.ping-devops.com/mcp/{BRANCH_1_LEAF}"
    return base if branch_index == 1 else f"{base}-{branch_index}"


def audience_attribute(branch_index, original_attr, namespace=UUID_NAMESPACE):
    attr = copy.deepcopy(original_attr)
    attr["id"] = clone_id(branch_index, original_attr["id"], namespace)
    attr["version"] = clone_id(branch_index, original_attr["version"], namespace)
    attr["name"] = f"Branch {branch_index} Audience"
    attr["fullName"] = f"Constants.Branch {branch_index} Audience"
    attr["description"] = (
        f"RFC 8707 resource identifier of the {BRANCH_1_LEAF}-{branch_index} MCP "
        "— tokens must be minted for this audience.")
    attr["resolvers"][0]["value"] = audience_value(branch_index)
    return attr


def set_leaf_condition(element, leaf):
    """Rewrite the set condition's leaf comparison to the branch's leaf."""
    for cond in element["condition"]["and"]["conditions"]:
        cmp_ = cond.get("comparison")
        if cmp_ and cmp_["left"]["attribute"]["id"] == LEAF_ATTR_ID:
            cmp_["right"]["constant"]["value"] = leaf
            return
    raise ValueError(f"no leaf condition found on {element.get('name')}")


def clone_subtree(elements, subtree_ids, branch_index, services=False,
                  branch_service_id=None, branch_target_id=None,
                  namespace=UUID_NAMESPACE):
    """Deep-copy the branch subtree with fresh deterministic ids and all
    audience references repointed to the branch's own audience attribute.
    Condition mode (default): the set gets a per-branch leaf condition.
    Services mode: the set gets NO condition and is targeted at the branch's
    service via the branch's Target element."""
    id_map = {orig: clone_id(branch_index, orig, namespace) for orig in subtree_ids}
    cloned = []
    for element in elements:
        if element.get("id") not in subtree_ids:
            continue
        clone = copy.deepcopy(element)
        clone["id"] = id_map[clone["id"]]
        if clone.get("version"):
            clone["version"] = id_map.get(
                element["version"], clone_id(branch_index, element["version"], namespace))
        # Rewrite every id reference this element carries. Policies carry
        # statement ids; Statements must not gain an empty statements field
        # (PAP's importer rejects the shape).
        for child in clone.get("children") or []:
            child["id"] = id_map[child["id"]]
        if "statements" in clone:
            clone["statements"] = [id_map[s] for s in clone["statements"] or []]
        # The audience constant attribute is not part of the subtree; each
        # clone references the new per-branch attribute instead.
        if clone.get("type") == "Rule":
            _repoint_audience(clone, branch_index, namespace)
        elif clone.get("type") == "Policy":
            clone["description"] = clone["description"].replace(
                audience_value(1), audience_value(branch_index))
        elif clone["id"] == id_map[PORTFOLIO_ID]:
            clone["name"] = f"{BRANCH_1_LEAF}-{branch_index} MCP"
            clone["description"] = clone["description"].replace(
                audience_value(1), audience_value(branch_index))
            if services:
                # Ping-recommended dispatch: no URL condition; targeting is
                # by the branch's service via the per-branch Target element.
                clone["condition"] = {"empty": {}}
                clone["targets"] = [branch_target_id]
            else:
                set_leaf_condition(clone, f"{BRANCH_1_LEAF}-{branch_index}")
        cloned.append(clone)
    return cloned


def _repoint_audience(rule, branch_index, namespace=UUID_NAMESPACE):
    """Point the rule's R1 comparison at the branch's audience attribute.
    The comparison sits in effectSettings.condition on conditional rules and
    in the top-level condition on the others — walk both."""
    for holder in (rule.get("effectSettings") or {}, rule):
        condition = holder.get("condition")
        if not condition:
            continue
        for cond in _walk_conditions(condition):
            cmp_ = cond.get("comparison")
            if not cmp_:
                continue
            for side in ("left", "right"):
                ref = cmp_.get(side, {}).get("attribute")
                if ref and ref.get("id") == AUDIENCE_ATTR_ID:
                    ref["id"] = clone_id(branch_index, AUDIENCE_ATTR_ID, namespace)


def _walk_conditions(condition):
    """Yield every condition node in the snapshot's nested and/or shape."""
    if not isinstance(condition, dict):
        return
    for connector in ("and", "or"):
        if connector in condition:
            for inner in condition[connector].get("conditions") or []:
                yield inner
                yield from _walk_conditions(inner)
    if "comparison" in condition:
        yield condition


def stream_digest(data):
    """The DataStreamFooter digest PAP validates: base64 sha256 over the
    compact JSON of every element except the footer, concatenated. Verified
    against PAP's own export of the baseline (Task 2, 2026-11-24)."""
    import base64
    import hashlib
    body = "".join(
        json.dumps(e, separators=(",", ":"), ensure_ascii=False) for e in data[:-1])
    return base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--services", action="store_true",
                        help="emit the service-targeted variant "
                             "(AgentGatewayPortfolioMCP-x100-services.SNAPSHOT)")
    args = parser.parse_args(argv)
    output = OUTPUT_SERVICES if args.services else OUTPUT
    generate(output, services=args.services)
    print(f"Wrote {output} ({output.stat().st_size} bytes)")


def branch_service_definition(branch_index, template):
    """The branch's ServiceDefinition, named to match the parameterized
    endpoint's derived service value exactly ('AgentGateway portfolio-<i>';
    the endpoint template 'AgentGateway {mcp}' captures the full leaf)."""
    svc = copy.deepcopy(template)
    svc["id"] = clone_id(branch_index, template["id"], services_namespace())
    svc["version"] = clone_id(branch_index, template["version"], services_namespace())
    svc["name"] = f"AgentGateway {BRANCH_1_LEAF}-{branch_index}"
    svc["fullName"] = svc["name"]
    return svc


def branch_1_service_definition(template):
    """Branch 1's ServiceDefinition: the parameterized endpoint derives
    'AgentGateway portfolio' for /mcp/portfolio, so branch 1 needs its own
    service even though its policy set keeps the campaign-of-record shape."""
    svc = copy.deepcopy(template)
    svc["id"] = clone_id(1, template["id"], services_namespace())
    svc["version"] = clone_id(1, template["version"], services_namespace())
    svc["name"] = f"AgentGateway {BRANCH_1_LEAF}"
    svc["fullName"] = svc["name"]
    return svc


def branch_target(branch_index, service_id):
    """The per-branch Target element binding the branch's policy set to its
    service. Field shape mirrors the baseline's real Target elements (the
    importer rejects a Target missing version/shared/permissions)."""
    return {
        "id": clone_id(branch_index, "target", services_namespace()),
        "version": clone_id(branch_index, "target-version", services_namespace()),
        "type": "Target",
        "name": f"AgentGateway {BRANCH_1_LEAF}-{branch_index} target",
        "description": f"Targets the {BRANCH_1_LEAF}-{branch_index} MCP's policy set at its service.",
        "shared": False,
        "permissions": {"inherit": True, "rolePermissions": []},
        "targeted": {"services": [service_id]},
        "properties": [],
    }


def branch_1_target(service_id):
    """Branch 1's Target element, targeting the ORIGINAL condition-gated set
    (the set keeps its leaf condition; targeting is additive)."""
    return {
        "id": clone_id(1, "target", services_namespace()),
        "version": clone_id(1, "target-version", services_namespace()),
        "type": "Target",
        "name": f"AgentGateway {BRANCH_1_LEAF} target",
        "description": f"Targets the original {BRANCH_1_LEAF} MCP's policy set at its service.",
        "shared": False,
        "permissions": {"inherit": True, "rolePermissions": []},
        "targeted": {"services": [service_id]},
        "properties": [],
    }


def generate(output_path, services=False):
    data = json.loads(BASELINE.read_text())
    by_id = {e["id"]: e for e in data if isinstance(e, dict) and e.get("id")}
    subtree_ids = reachable_subtree(data, PORTFOLIO_ID)
    audience_attr = by_id[AUDIENCE_ATTR_ID]
    service_template = by_id["48a0ae45-a0eb-40b4-90b1-a1ee77c17395"]  # AgentGateway ServiceDefinition

    # Anchor for insertion: the head runs to the GDP element (the last
    # id-bearing element, index 157); the mid-stream PackageSeparator (104)
    # stays in the head; the wrapper tail (separator/EndOfPackage/footer)
    # stays last.
    last_index = max(i for i, e in enumerate(data)
                     if isinstance(e, dict) and e.get("id"))
    tail = data[last_index + 1:]

    mode_ns = services_namespace() if services else UUID_NAMESPACE
    out = list(data[: last_index + 1])
    for branch_index in range(2, BRANCH_COUNT + 1):
        out.append(audience_attribute(branch_index, audience_attr, mode_ns))
        if services:
            svc = branch_service_definition(branch_index, service_template)
            target = branch_target(branch_index, svc["id"])
            out.extend([svc, target])
            out.extend(clone_subtree(
                data, subtree_ids, branch_index, services=True,
                branch_service_id=svc["id"], branch_target_id=target["id"],
                namespace=mode_ns))
        else:
            out.extend(clone_subtree(data, subtree_ids, branch_index))

    # The AgentGateway set sits in the head section; rewrite its children
    # there (both modes keep all 100 sets under the one wrapper set) and
    # append the tail. The wrapper keeps its baseline Target element, whose
    # services list this function extends in services mode (below) to cover
    # every derived service value.
    for e in out:
        if isinstance(e, dict) and e.get("id") == "57d54005-b9b3-4371-ba64-22996439ddf8" and services:
            # Wrapper Target: plain AgentGateway + every branch service
            # (branch 1's 'AgentGateway portfolio' included).
            e["targeted"]["services"] = [e["targeted"]["services"][0]] + [
                clone_id(i, "48a0ae45-a0eb-40b4-90b1-a1ee77c17395", services_namespace())
                for i in range(1, BRANCH_COUNT + 1)]
        if isinstance(e, dict) and e.get("id") == PORTFOLIO_ID and services:
            # Branch 1's set gains its service target; its leaf condition
            # stays (harmless under the parameterized endpoint, decisive
            # under the plain one).
            e["targets"] = [clone_id(1, "target", services_namespace())]
    for e in out:
        if isinstance(e, dict) and e.get("id") == AGENTGATEWAY_ID:
            e["children"] = [{"id": PORTFOLIO_ID, "type": "PolicySet"}] + [
                {"id": clone_id(i, PORTFOLIO_ID, mode_ns), "type": "PolicySet"}
                for i in range(2, BRANCH_COUNT + 1)]
    if services:
        out.append(branch_1_service_definition(service_template))
        out.append(branch_1_target(clone_id(1, service_template["id"], services_namespace())))
    out.extend(tail)

    # The footer carries a digest over the whole element stream; the clone
    # changes the stream, so recompute it. PAP rejects a stale digest at
    # import (Task 2 finding).
    out[-1]["digest"] = stream_digest(out)

    output_path.write_text(
        json.dumps(out, separators=(",", ":"), ensure_ascii=False) + "\n"
        if BASELINE.read_text().endswith("\n") else
        json.dumps(out, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
