import copy
import json
import unittest
from pathlib import Path

from app.engine import (
    InvalidPlanError,
    PolicyError,
    UnknownFoodError,
    UnknownPolicyError,
    _resolve,
    evaluate,
    load_foods,
    load_policies,
)

ROOT = Path(__file__).resolve().parent.parent


def load_example(name):
    plan = json.loads((ROOT / "examples" / f"{name}.json").read_text())
    return plan.pop("policy_id"), plan


def rule_ids(report, severity=None):
    return {v["rule_id"] for v in report["violations"] if severity in (None, v["severity"])}


def meal(name, items, mtype="main"):
    return {"name": name, "type": mtype, "items": items}


def custom(**nutrients):
    tags = nutrients.pop("tags", [])
    return {"name": "custom", "nutrients": {"kcal": 500, **nutrients}, "tags": tags}


class PolicyLoadingTests(unittest.TestCase):
    def test_all_policies_resolve(self):
        policies = load_policies()
        self.assertEqual(set(policies), {
            "low_sodium_2g", "low_sodium_1500", "diabetic_ccho", "diabetic_low_sodium",
            "ph_ngf_pinggang_pinoy", "davao_hospital_low_sodium", "davao_hospital_diabetic",
            "davao_hospital_diabetic_low_sodium"})

    def test_extends_overrides_inherited_rule(self):
        rules = {r["id"]: r for r in load_policies()["low_sodium_1500"]["rules"]}
        self.assertEqual(rules["sodium_day_max"]["value"], 1500)
        self.assertEqual(len(rules), 4)  # no duplicate ids

    def test_combined_policy_uses_stricter_sodium_rule(self):
        rules = {r["id"]: r for r in load_policies()["diabetic_low_sodium"]["rules"]}
        self.assertEqual(rules["sodium_day_max"]["value"], 2000)
        self.assertEqual(rules["sodium_day_max"]["severity"], "error")
        self.assertIn("carb_main_max", rules)

    def test_bad_rule_config_is_rejected(self):
        bad = {"p": {"id": "p", "rules": [{"id": "r", "type": "max", "scope": "day", "nutrient": "sodium_mg"}]}}
        with self.assertRaises(PolicyError):
            _resolve("p", bad)
        bad["p"]["rules"] = [{"id": "r", "type": "max", "scope": "day", "nutrient": "bogus", "value": 1}]
        with self.assertRaises(PolicyError):
            _resolve("p", bad)

    def test_circular_extends_is_rejected(self):
        loop = {"a": {"id": "a", "extends": ["b"]}, "b": {"id": "b", "extends": ["a"]}}
        with self.assertRaises(PolicyError):
            _resolve("a", loop)

    def test_every_food_has_all_nutrients(self):
        for f in load_foods().values():
            self.assertEqual(set(f["nutrients"]), {"kcal", "carbs_g", "fiber_g", "added_sugar_g",
                                                   "sat_fat_g", "sodium_mg", "protein_g"}, f["id"])


class EvaluationTests(unittest.TestCase):
    def test_compliant_example_passes_everything(self):
        pid, plan = load_example("compliant_plan")
        r = evaluate(plan, pid)
        self.assertTrue(r["compliant"])
        self.assertTrue(r["fully_compliant"])
        self.assertEqual(r["summary"]["checks_run"], r["summary"]["checks_passed"])

    def test_noncompliant_example_flags_expected_rules(self):
        pid, plan = load_example("noncompliant_plan")
        r = evaluate(plan, pid)
        self.assertFalse(r["compliant"])
        self.assertEqual(rule_ids(r, "error"), {"carb_main_max", "no_sugar_sweetened_beverages", "sodium_day_max"})
        self.assertIn("added_sugar_pct", rule_ids(r, "warning"))

    def test_totals_are_servings_scaled(self):
        plan = {"meals": [meal("Lunch", [{"food_id": "white_rice_cooked", "servings": 2}])]}
        r = evaluate(plan, "low_sodium_2g")
        self.assertEqual(r["totals"]["day"]["carbs_g"], 90)
        self.assertEqual(r["totals"]["day"]["kcal"], 410)

    def test_daily_sodium_boundary_is_inclusive(self):
        ok = {"meals": [meal("Lunch", [custom(sodium_mg=2000)])]}
        over = {"meals": [meal("Lunch", [custom(sodium_mg=2000.5)])]}
        self.assertNotIn("sodium_day_max", rule_ids(evaluate(ok, "low_sodium_2g")))
        self.assertIn("sodium_day_max", rule_ids(evaluate(over, "low_sodium_2g"), "error"))

    def test_strict_policy_is_stricter_than_2g(self):
        plan = {"meals": [meal("Lunch", [custom(sodium_mg=1800)])]}
        self.assertTrue(evaluate(plan, "low_sodium_2g")["compliant"])
        self.assertFalse(evaluate(plan, "low_sodium_1500")["compliant"])

    def test_pct_energy_math(self):
        # 20 g added sugar = 80 kcal of 500 kcal = 16%
        plan = {"meals": [meal("Lunch", [custom(added_sugar_g=20)])]}
        v = [c for c in evaluate(plan, "diabetic_ccho")["checks"] if c["rule_id"] == "added_sugar_pct"][0]
        self.assertAlmostEqual(v["actual"], 16.0)
        self.assertFalse(v["passed"])

    def test_snack_rules_apply_only_to_snacks(self):
        plan = {"meals": [meal("Snack", [custom(carbs_g=25)], "snack")]}
        ids = {c["rule_id"] for c in evaluate(plan, "diabetic_ccho")["checks"]}
        self.assertIn("carb_snack_max", ids)
        self.assertNotIn("carb_main_max", ids)

    def test_carb_consistency_violation(self):
        plan = {"meals": [meal("Breakfast", [custom(carbs_g=45)]), meal("Lunch", [custom(carbs_g=60)]),
                          meal("Dinner", [custom(carbs_g=46)])]}
        self.assertNotIn("carb_consistency", rule_ids(evaluate(plan, "diabetic_ccho")))
        plan["meals"][1]["items"][0]["nutrients"]["carbs_g"] = 60.5
        self.assertIn("carb_consistency", rule_ids(evaluate(plan, "diabetic_ccho")))

    def test_required_vegetable_only_checked_for_named_meals(self):
        plan = {"meals": [meal("Breakfast", [custom(carbs_g=50)]), meal("Lunch", [custom(carbs_g=50)])]}
        r = evaluate(plan, "diabetic_ccho")
        targets = {c["target"] for c in r["checks"] if c["rule_id"] == "veg_at_lunch_dinner"}
        self.assertEqual(targets, {"Lunch"})
        self.assertIn("veg_at_lunch_dinner", rule_ids(r, "warning"))

    def test_min_main_meals(self):
        plan = {"meals": [meal("Lunch", [custom(carbs_g=50)])]}
        self.assertIn("min_main_meals", rule_ids(evaluate(plan, "diabetic_ccho")))

    def test_violation_includes_hint_with_top_contributor(self):
        plan = {"meals": [meal("Lunch", [{"food_id": "soy_sauce", "servings": 3},
                                         {"food_id": "white_rice_cooked", "servings": 1}])]}
        v = [x for x in evaluate(plan, "low_sodium_2g")["violations"] if x["rule_id"] == "sodium_day_max"][0]
        self.assertIn("Soy sauce", v["hint"])

    def test_policy_rule_can_be_added_via_data_only(self):
        policies = copy.deepcopy(load_policies())
        policies["low_sodium_2g"]["rules"].append({
            "id": "protein_min", "type": "min", "scope": "day", "nutrient": "protein_g", "value": 60,
            "severity": "warning", "applies_to": "all"})
        plan = {"meals": [meal("Lunch", [custom(protein_g=10)])]}
        self.assertIn("protein_min", rule_ids(evaluate(plan, "low_sodium_2g", policies=policies)))


class InputValidationTests(unittest.TestCase):
    def test_unknown_policy(self):
        with self.assertRaises(UnknownPolicyError):
            evaluate({"meals": [meal("L", [custom()])]}, "nope")

    def test_unknown_food(self):
        with self.assertRaises(UnknownFoodError):
            evaluate({"meals": [meal("L", [{"food_id": "unicorn_steak"}])]}, "low_sodium_2g")

    def test_bad_plans(self):
        bad = [
            None,
            {},
            {"meals": []},
            {"meals": [meal("L", [])]},
            {"meals": [meal("L", [{"food_id": "banana", "servings": 0}])]},
            {"meals": [meal("L", [{"food_id": "banana", "servings": True}])]},
            {"meals": [meal("L", [{"food_id": "banana", "servings": 999}])]},
            {"meals": [meal("L", [{"nutrients": {"sodium_mg": -5, "kcal": 10}}])]},
            {"meals": [meal("L", [{"nutrients": {"sodum_mg": 5, "kcal": 10}}])]},
            {"meals": [meal("L", [{"name": "x"}])]},
            {"meals": [meal("L", [custom()], "brunch")]},
            {"meals": [meal("L", [{"nutrients": {"kcal": 0}}])]},
        ]
        for plan in bad:
            with self.subTest(plan=plan), self.assertRaises(InvalidPlanError):
                evaluate(plan, "low_sodium_2g")


class DavaoPhilippineTests(unittest.TestCase):
    def test_davao_compliant_plan_is_fully_compliant(self):
        pid, plan = load_example("davao_compliant_plan")
        r = evaluate(plan, pid)
        self.assertTrue(r["fully_compliant"], r["violations"])
        self.assertEqual(r["policy"]["status"], "template")
        self.assertEqual(r["policy"]["jurisdiction"], "Davao City, Philippines")

    def test_davao_noncompliant_plan_flags_expected_rules(self):
        pid, plan = load_example("davao_noncompliant_plan")
        r = evaluate(plan, pid)
        self.assertFalse(r["compliant"])
        self.assertEqual(rule_ids(r, "error"),
                         {"carb_main_max", "no_sugar_sweetened_beverages", "sodium_day_max"})
        self.assertTrue({"pp_vegetable", "pp_fruit", "pp_rice_portion", "no_fried_foods",
                         "no_high_sodium_items", "no_concentrated_sweets"} <= rule_ids(r, "warning"))

    def test_clinical_rule_overrides_national_flag_rule(self):
        # national policy only warns about sugary drinks; the diabetic hospital policy makes it an error
        sev = {r["id"]: r["severity"] for r in load_policies()["davao_hospital_diabetic"]["rules"]}
        self.assertEqual(sev["no_sugar_sweetened_beverages"], "error")
        sev_national = {r["id"]: r["severity"] for r in load_policies()["ph_ngf_pinggang_pinoy"]["rules"]}
        self.assertEqual(sev_national["no_sugar_sweetened_beverages"], "warning")

    def test_disable_rules_swaps_vegetable_rule_without_duplicates(self):
        ids = [r["id"] for r in load_policies()["davao_hospital_diabetic"]["rules"]]
        self.assertNotIn("veg_at_lunch_dinner", ids)
        self.assertIn("pp_vegetable", ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_disable_unknown_rule_is_rejected(self):
        bad = {"p": {"id": "p", "disable_rules": ["ghost"], "rules": []}}
        with self.assertRaises(PolicyError):
            _resolve("p", bad)

    def test_davao_hospital_sodium_is_hard_limit_with_low_sodium_order(self):
        rules = {r["id"]: r for r in load_policies()["davao_hospital_low_sodium"]["rules"]}
        self.assertEqual(rules["sodium_day_max"]["severity"], "error")
        self.assertEqual(rules["sodium_day_max"]["value"], 2000)

    def test_rice_portion_rule(self):
        def plan(cups):
            return {"meals": [meal("Tanghalian", [{"food_id": "white_rice_cooked", "servings": cups},
                                                  {"food_id": "tuna_grilled", "servings": 1}])]}
        self.assertNotIn("pp_rice_portion", rule_ids(evaluate(plan(1), "ph_ngf_pinggang_pinoy")))
        r = evaluate(plan(1.5), "ph_ngf_pinggang_pinoy")
        self.assertIn("pp_rice_portion", rule_ids(r, "warning"))

    def test_national_policy_is_advisory_only(self):
        plan = {"meals": [meal("Almusal", [{"food_id": "white_rice_cooked", "servings": 1}])]}
        r = evaluate(plan, "ph_ngf_pinggang_pinoy")
        self.assertTrue(r["compliant"])          # warnings only, no errors
        self.assertFalse(r["fully_compliant"])
        self.assertTrue({"pp_protein", "pp_vegetable", "pp_fruit"} <= rule_ids(r, "warning"))

    def test_filipino_meal_names_and_foods_resolve(self):
        foods = load_foods()
        for fid in ("pomelo", "tuna_grilled", "pinakbet", "bagoong", "halo_halo", "pandesal"):
            self.assertIn(fid, foods)


if __name__ == "__main__":
    unittest.main()
