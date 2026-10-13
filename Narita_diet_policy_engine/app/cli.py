"""Command-line validator.

Usage:
    python -m app.cli examples/noncompliant_plan.json
    python -m app.cli plan.json --policy low_sodium_1500 --json
Exit code: 0 compliant, 1 has errors, 2 bad input.
"""
from __future__ import annotations

import argparse
import json
import sys

from .engine import InvalidPlanError, PolicyError, UnknownPolicyError, evaluate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate a meal plan against a dietary policy.")
    ap.add_argument("plan", help="path to a plan JSON file")
    ap.add_argument("--policy", help="policy id (overrides 'policy_id' in the file)")
    ap.add_argument("--json", action="store_true", help="print the full JSON report")
    args = ap.parse_args(argv)

    try:
        with open(args.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
        policy_id = args.policy or plan.pop("policy_id", None)
        if not policy_id:
            raise InvalidPlanError("no policy given: use --policy or add 'policy_id' to the file")
        plan.pop("policy_id", None)
        report = evaluate(plan, policy_id)
    except (OSError, json.JSONDecodeError, InvalidPlanError, UnknownPolicyError, PolicyError) as exc:
        print(f"error: {exc.args[0] if isinstance(exc, KeyError) else exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        s = report["summary"]
        status = "COMPLIANT" if report["compliant"] else "NOT COMPLIANT"
        print(f"{report['policy']['name']}: {status}  "
              f"({s['errors']} error(s), {s['warnings']} warning(s), {s['checks_passed']}/{s['checks_run']} checks passed)")
        for v in report["violations"]:
            print(f"  [{v['severity'].upper():7}] {v['rule_id']}: {v['message']}")
            if v["hint"]:
                print(f"            hint: {v['hint']}")
    return 0 if report["compliant"] else 1


if __name__ == "__main__":
    sys.exit(main())
