"""Rule engine: validates meal plans against codified dietary policies.

Pure standard library. Policies live in policies.json, foods in foods.json.
Typical use:

    from app.engine import evaluate
    result = evaluate({"meals": [...]}, "diabetic_ccho")
    result["compliant"], result["violations"]
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

DATA_DIR = Path(__file__).parent
NUTRIENTS = ("kcal", "carbs_g", "fiber_g", "added_sugar_g", "sat_fat_g", "sodium_mg", "protein_g")
LABELS = {
    "kcal": ("Energy", "kcal"),
    "carbs_g": ("Carbohydrate", "g"),
    "fiber_g": ("Fiber", "g"),
    "added_sugar_g": ("Added sugar", "g"),
    "sat_fat_g": ("Saturated fat", "g"),
    "sodium_mg": ("Sodium", "mg"),
    "protein_g": ("Protein", "g"),
}
KCAL_PER_GRAM = {"sat_fat_g": 9.0, "added_sugar_g": 4.0}
MEAL_TYPES = ("main", "snack")
SEVERITIES = ("error", "warning")
MAX_MEALS = 12
MAX_ITEMS_PER_MEAL = 50
MAX_SERVINGS = 20
EPS = 1e-9
DISCLAIMER = (
    "Decision-support tool only. Thresholds are configurable examples; confirm them against "
    "your institution's current policy and have a registered dietitian review real patient plans."
)


class PolicyError(Exception):
    """A policy file is malformed."""


class UnknownPolicyError(KeyError):
    """Requested policy id does not exist."""


class InvalidPlanError(ValueError):
    """The submitted meal plan is malformed."""


class UnknownFoodError(InvalidPlanError):
    """A food_id is not in the food database."""


# --------------------------------------------------------------------------- helpers

def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _fmt(v: float) -> str:
    return f"{v:,.1f}".rstrip("0").rstrip(".")


def _qty(nutrient: str, v: float) -> str:
    return f"{_fmt(v)} {LABELS[nutrient][1]}"


# --------------------------------------------------------------------------- loading

def load_foods(path: str | None = None) -> dict[str, dict]:
    raw = json.loads(Path(path or DATA_DIR / "foods.json").read_text(encoding="utf-8"))
    return {f["id"]: f for f in raw["foods"]}


_RULE_REQUIRED = {
    "max": ("scope", "nutrient", "value"),
    "min": ("scope", "nutrient", "value"),
    "pct_energy_max": ("nutrient", "value"),
    "per_1000kcal_min": ("nutrient", "value"),
    "consistency": ("nutrient", "max_difference"),
    "forbidden_tags": ("tags",),
    "required_tags_per_meal": ("any_of",),
    "min_meals": ("value",),
    "tag_servings_max": ("scope", "tags", "value"),
}


def _normalise_rule(rule: dict, policy_id: str) -> dict:
    where = f"policy '{policy_id}', rule '{rule.get('id')}'"
    if not isinstance(rule.get("id"), str) or not rule["id"]:
        raise PolicyError(f"policy '{policy_id}': every rule needs a string 'id'")
    if rule.get("type") not in _RULE_REQUIRED:
        raise PolicyError(f"{where}: unknown type {rule.get('type')!r}")
    out = dict(rule)
    out.setdefault("severity", "error")
    if out["severity"] not in SEVERITIES:
        raise PolicyError(f"{where}: severity must be one of {SEVERITIES}")
    for key in _RULE_REQUIRED[out["type"]]:
        if key not in out:
            raise PolicyError(f"{where}: missing '{key}'")
    if "nutrient" in out and out["nutrient"] not in NUTRIENTS:
        raise PolicyError(f"{where}: unknown nutrient {out['nutrient']!r}")
    if out["type"] == "pct_energy_max" and out["nutrient"] not in KCAL_PER_GRAM:
        raise PolicyError(f"{where}: pct_energy_max supports {sorted(KCAL_PER_GRAM)}")
    if out["type"] in ("max", "min", "tag_servings_max") and out["scope"] not in ("meal", "day"):
        raise PolicyError(f"{where}: scope must be 'meal' or 'day'")
    for key in ("value", "max_difference"):
        if key in out and not _is_num(out[key]):
            raise PolicyError(f"{where}: '{key}' must be a number")
    out.setdefault("applies_to", "all")
    if out["applies_to"] not in ("all",) + MEAL_TYPES:
        raise PolicyError(f"{where}: applies_to must be all/main/snack")
    for key in ("tags", "any_of", "meal_names"):
        if key in out and not (isinstance(out[key], list) and all(isinstance(t, str) for t in out[key])):
            raise PolicyError(f"{where}: '{key}' must be a list of strings")
    return out


def _resolve(pid: str, raw_by_id: dict[str, dict], stack: tuple[str, ...] = ()) -> dict:
    if pid in stack:
        raise PolicyError("circular 'extends': " + " -> ".join(stack + (pid,)))
    if pid not in raw_by_id:
        raise PolicyError(f"unknown policy in 'extends': {pid!r}")
    raw = raw_by_id[pid]
    rules: dict[str, dict] = {}
    sources: list[dict] = []

    def add_sources(items: list[dict]) -> None:
        for s in items:
            if s not in sources:
                sources.append(s)

    for parent in raw.get("extends", []):
        resolved = _resolve(parent, raw_by_id, stack + (pid,))
        for r in resolved["rules"]:
            rules[r["id"]] = r  # later parents override earlier ones
        add_sources(resolved["sources"])
    for rid in raw.get("disable_rules", []):
        if rid not in rules:
            raise PolicyError(f"policy '{pid}': cannot disable unknown inherited rule {rid!r}")
        del rules[rid]
    own_ids: set[str] = set()
    for r in raw.get("rules", []):
        norm = _normalise_rule(r, pid)
        if norm["id"] in own_ids:
            raise PolicyError(f"policy '{pid}': duplicate rule id {norm['id']!r}")
        own_ids.add(norm["id"])
        rules[norm["id"]] = norm  # own rules override inherited ones
    add_sources(raw.get("sources", []))
    return {
        "id": pid,
        "name": raw.get("name", pid),
        "description": raw.get("description", ""),
        "jurisdiction": raw.get("jurisdiction", ""),
        "institution": raw.get("institution", ""),
        "status": raw.get("status", "reference"),
        "extends": raw.get("extends", []),
        "sources": sources,
        "rules": list(rules.values()),
    }


@lru_cache(maxsize=4)
def load_policies(path: str | None = None) -> dict[str, dict]:
    """Return {policy_id: resolved_policy}. Treat the result as read-only."""
    raw = json.loads(Path(path or DATA_DIR / "policies.json").read_text(encoding="utf-8"))
    raw_by_id = {p["id"]: p for p in raw["policies"]}
    return {pid: _resolve(pid, raw_by_id) for pid in raw_by_id}


# --------------------------------------------------------------------------- plan building

def _build_item(raw: Any, foods: dict[str, dict], where: str, meal_name: str) -> dict:
    if not isinstance(raw, dict):
        raise InvalidPlanError(f"{where}: item must be an object")
    servings = raw.get("servings", 1)
    if not _is_num(servings) or servings <= 0 or servings > MAX_SERVINGS:
        raise InvalidPlanError(f"{where}: 'servings' must be a number > 0 and <= {MAX_SERVINGS}")
    if raw.get("food_id") is not None:
        fid = raw["food_id"]
        food = foods.get(fid) if isinstance(fid, str) else None
        if food is None:
            raise UnknownFoodError(f"{where}: unknown food_id {fid!r}")
        per, name, tags, fid_out = food["nutrients"], food["name"], list(food.get("tags", [])), fid
    elif isinstance(raw.get("nutrients"), dict):
        per = raw["nutrients"]
        for k, v in per.items():
            if k not in NUTRIENTS:
                raise InvalidPlanError(f"{where}: unknown nutrient {k!r}")
            if not _is_num(v) or v < 0:
                raise InvalidPlanError(f"{where}: nutrient {k!r} must be a number >= 0")
        name = raw.get("name") or "custom item"
        tags = raw.get("tags") or []
        if not isinstance(name, str) or not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
            raise InvalidPlanError(f"{where}: 'name' must be text and 'tags' a list of text")
        fid_out = None
    else:
        raise InvalidPlanError(f"{where}: give either 'food_id' or a 'nutrients' object")
    return {
        "food_id": fid_out,
        "name": name,
        "servings": servings,
        "tags": tags,
        "meal": meal_name,
        "nutrients": {n: round(float(per.get(n, 0)) * servings, 4) for n in NUTRIENTS},
    }


def _totals(items: list[dict]) -> dict[str, float]:
    return {n: round(sum(i["nutrients"][n] for i in items), 2) for n in NUTRIENTS}


def build_plan(raw: Any, foods: dict[str, dict]) -> dict:
    meals_raw = raw.get("meals") if isinstance(raw, dict) else None
    if not isinstance(meals_raw, list) or not meals_raw:
        raise InvalidPlanError("plan must be an object with a non-empty 'meals' list")
    if len(meals_raw) > MAX_MEALS:
        raise InvalidPlanError(f"at most {MAX_MEALS} meals per plan")
    meals = []
    for mi, rm in enumerate(meals_raw, 1):
        if not isinstance(rm, dict):
            raise InvalidPlanError(f"meal {mi}: must be an object")
        name = rm.get("name", f"Meal {mi}")
        mtype = rm.get("type", "main")
        if not isinstance(name, str) or not name.strip():
            raise InvalidPlanError(f"meal {mi}: 'name' must be non-empty text")
        if mtype not in MEAL_TYPES:
            raise InvalidPlanError(f"meal '{name}': type must be one of {MEAL_TYPES}")
        items_raw = rm.get("items")
        if not isinstance(items_raw, list) or not items_raw:
            raise InvalidPlanError(f"meal '{name}': needs a non-empty 'items' list")
        if len(items_raw) > MAX_ITEMS_PER_MEAL:
            raise InvalidPlanError(f"meal '{name}': at most {MAX_ITEMS_PER_MEAL} items")
        items = [_build_item(it, foods, f"meal '{name}' item {ii}", name) for ii, it in enumerate(items_raw, 1)]
        meals.append({"name": name, "type": mtype, "items": items, "totals": _totals(items)})
    all_items = [i for m in meals for i in m["items"]]
    day = _totals(all_items)
    if day["kcal"] <= 0:
        raise InvalidPlanError("plan has zero energy (kcal); check the items' nutrient values")
    return {"meals": meals, "day": day, "items": all_items}


# --------------------------------------------------------------------------- rule handlers

Check = dict[str, Any]


def _check(rule: dict, *, scope: str, target: str, passed: bool, message: str, nutrient: str | None = None,
           actual: float | None = None, limit: float | None = None, comparator: str | None = None,
           hint: str | None = None) -> Check:
    return {
        "rule_id": rule["id"],
        "severity": rule["severity"],
        "scope": scope,
        "target": target,
        "nutrient": nutrient,
        "actual": None if actual is None else round(actual, 2),
        "limit": limit,
        "comparator": comparator,
        "passed": passed,
        "message": message,
        "rationale": rule.get("description"),
        "source": rule.get("source"),
        "hint": hint,
    }


def _applies(meal: dict, rule: dict) -> bool:
    if rule["applies_to"] != "all" and meal["type"] != rule["applies_to"]:
        return False
    names = rule.get("meal_names")
    return not names or meal["name"].lower() in {n.lower() for n in names}


def _top_contributor(items: list[dict], nutrient: str, in_day: bool) -> str | None:
    if not items:
        return None
    top = max(items, key=lambda i: i["nutrients"][nutrient])
    amount = top["nutrients"][nutrient]
    if amount <= 0:
        return None
    where = f" in {top['meal']}" if in_day else ""
    return f"Largest contributor: {top['name']}{where} ({_qty(nutrient, amount)})."


def _rule_bound(rule: dict, plan: dict) -> list[Check]:
    is_max = rule["type"] == "max"
    n, limit = rule["nutrient"], rule["value"]
    label = LABELS[n][0]
    if rule["scope"] == "day":
        targets = [("Daily total", plan["day"], plan["items"], True)]
    else:
        targets = [(m["name"], m["totals"], m["items"], False) for m in plan["meals"] if _applies(m, rule)]
    out = []
    for name, totals, items, in_day in targets:
        actual = totals[n]
        passed = actual <= limit + EPS if is_max else actual >= limit - EPS
        word = "at most" if is_max else "at least"
        if passed:
            msg = f"{name}: {label.lower()} {_qty(n, actual)} meets the requirement ({word} {_qty(n, limit)})."
            hint = None
        else:
            direction = "above" if is_max else "below"
            msg = f"{name}: {label.lower()} is {_qty(n, actual)}, {direction} the limit of {_qty(n, limit)}."
            hint = _top_contributor(items, n, in_day) if is_max else None
        out.append(_check(rule, scope=rule["scope"], target=name, passed=passed, message=msg, nutrient=n,
                          actual=actual, limit=limit, comparator="<=" if is_max else ">=", hint=hint))
    return out


def _rule_pct_energy_max(rule: dict, plan: dict) -> list[Check]:
    n, limit = rule["nutrient"], rule["value"]
    pct = plan["day"][n] * KCAL_PER_GRAM[n] / plan["day"]["kcal"] * 100
    passed = pct <= limit + EPS
    label = LABELS[n][0]
    msg = (f"{label} supplies {_fmt(pct)}% of daily energy "
           f"({'within' if passed else 'above'} the {_fmt(limit)}% limit).")
    hint = None if passed else _top_contributor(plan["items"], n, True)
    return [_check(rule, scope="day", target="Daily total", passed=passed, message=msg, nutrient=n,
                   actual=pct, limit=limit, comparator="<=", hint=hint)]


def _rule_per_1000kcal_min(rule: dict, plan: dict) -> list[Check]:
    n, limit = rule["nutrient"], rule["value"]
    density = plan["day"][n] / plan["day"]["kcal"] * 1000
    passed = density >= limit - EPS
    label = LABELS[n][0]
    msg = (f"{label} density is {_fmt(density)} {LABELS[n][1]} per 1,000 kcal "
           f"({'meets' if passed else 'below'} the {_fmt(limit)} minimum).")
    return [_check(rule, scope="day", target="Daily total", passed=passed, message=msg, nutrient=n,
                   actual=density, limit=limit, comparator=">=")]


def _rule_consistency(rule: dict, plan: dict) -> list[Check]:
    n, limit = rule["nutrient"], rule["max_difference"]
    meals = [m for m in plan["meals"] if _applies(m, rule)]
    if len(meals) < 2:
        return []
    lo = min(meals, key=lambda m: m["totals"][n])
    hi = max(meals, key=lambda m: m["totals"][n])
    diff = hi["totals"][n] - lo["totals"][n]
    passed = diff <= limit + EPS
    label = LABELS[n][0].lower()
    if passed:
        msg = f"{label.capitalize()} varies by {_qty(n, diff)} across meals (within {_qty(n, limit)})."
    else:
        msg = (f"{label.capitalize()} varies by {_qty(n, diff)} across meals ({hi['name']} "
               f"{_qty(n, hi['totals'][n])} vs {lo['name']} {_qty(n, lo['totals'][n])}); limit is {_qty(n, limit)}.")
    return [_check(rule, scope="day", target="Across meals", passed=passed, message=msg, nutrient=n,
                   actual=diff, limit=limit, comparator="<=")]


def _rule_forbidden_tags(rule: dict, plan: dict) -> list[Check]:
    banned = set(rule["tags"])
    out = []
    for m in plan["meals"]:
        if not _applies(m, rule):
            continue
        hits = [i["name"] for i in m["items"] if banned & set(i["tags"])]
        if hits:
            msg = f"{m['name']}: contains restricted item(s): {', '.join(hits)}."
            hint = "Replace or remove: " + ", ".join(hits) + "."
        else:
            msg, hint = f"{m['name']}: no restricted items.", None
        out.append(_check(rule, scope="meal", target=m["name"], passed=not hits, message=msg, hint=hint))
    return out


def _rule_required_tags(rule: dict, plan: dict) -> list[Check]:
    wanted = set(rule["any_of"])
    out = []
    for m in plan["meals"]:
        if not _applies(m, rule):
            continue
        ok = any(wanted & set(i["tags"]) for i in m["items"])
        msg = (f"{m['name']}: includes a required item type." if ok else
               f"{m['name']}: missing a required item type ({' or '.join(sorted(wanted))}).")
        out.append(_check(rule, scope="meal", target=m["name"], passed=ok, message=msg,
                          hint=None if ok else "Add one: " + ", ".join(sorted(wanted)) + "."))
    return out


def _rule_min_meals(rule: dict, plan: dict) -> list[Check]:
    count = sum(1 for m in plan["meals"] if _applies(m, rule))
    limit = rule["value"]
    passed = count >= limit
    kind = "" if rule["applies_to"] == "all" else f"{rule['applies_to']} "
    msg = f"Plan has {count} {kind}meal(s); at least {_fmt(limit)} required." if not passed else \
          f"Plan has {count} {kind}meal(s) (minimum {_fmt(limit)})."
    return [_check(rule, scope="day", target="Meal count", passed=passed, message=msg,
                   actual=count, limit=limit, comparator=">=")]


def _rule_tag_servings_max(rule: dict, plan: dict) -> list[Check]:
    """Cap the total servings of foods carrying any of the rule's tags (e.g. cups of rice)."""
    wanted, limit = set(rule["tags"]), rule["value"]
    if rule["scope"] == "day":
        targets = [("Daily total", plan["items"])]
    else:
        targets = [(m["name"], m["items"]) for m in plan["meals"] if _applies(m, rule)]
    tag_text = "/".join(sorted(wanted))
    out = []
    for name, items in targets:
        hits = [i for i in items if wanted & set(i["tags"])]
        total = sum(i["servings"] for i in hits)
        passed = total <= limit + EPS
        if passed:
            msg, hint = f"{name}: {_fmt(total)} serving(s) of {tag_text} (limit {_fmt(limit)}).", None
        else:
            msg = f"{name}: {_fmt(total)} serving(s) of {tag_text}, above the limit of {_fmt(limit)}."
            hint = "Reduce: " + ", ".join(i["name"] for i in hits) + "."
        out.append(_check(rule, scope=rule["scope"], target=name, passed=passed, message=msg,
                          actual=total, limit=limit, comparator="<=", hint=hint))
    return out


HANDLERS: dict[str, Callable[[dict, dict], list[Check]]] = {
    "max": _rule_bound,
    "min": _rule_bound,
    "pct_energy_max": _rule_pct_energy_max,
    "per_1000kcal_min": _rule_per_1000kcal_min,
    "consistency": _rule_consistency,
    "forbidden_tags": _rule_forbidden_tags,
    "required_tags_per_meal": _rule_required_tags,
    "min_meals": _rule_min_meals,
    "tag_servings_max": _rule_tag_servings_max,
}


# --------------------------------------------------------------------------- public API

def evaluate(plan: Any, policy_id: str, policies: dict | None = None, foods: dict | None = None) -> dict:
    """Validate ``plan`` against ``policy_id`` and return a full compliance report."""
    policies = load_policies() if policies is None else policies
    foods = load_foods() if foods is None else foods
    policy = policies.get(policy_id)
    if policy is None:
        raise UnknownPolicyError(f"unknown policy_id {policy_id!r}; available: {sorted(policies)}")
    built = build_plan(plan, foods)

    checks: list[Check] = []
    for rule in policy["rules"]:
        checks.extend(HANDLERS[rule["type"]](rule, built))
    violations = [c for c in checks if not c["passed"]]
    errors = sum(1 for v in violations if v["severity"] == "error")
    warnings = len(violations) - errors

    return {
        "policy": {
            "id": policy["id"],
            "name": policy["name"],
            "jurisdiction": policy.get("jurisdiction", ""),
            "institution": policy.get("institution", ""),
            "status": policy.get("status", "reference"),
            "sources": policy["sources"],
        },
        "compliant": errors == 0,
        "fully_compliant": not violations,
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "checks_run": len(checks),
            "checks_passed": len(checks) - len(violations),
        },
        "totals": {
            "day": built["day"],
            "meals": [
                {"name": m["name"], "type": m["type"], **m["totals"],
                 "items": [{"name": i["name"], "servings": i["servings"], **i["nutrients"]} for i in m["items"]]}
                for m in built["meals"]
            ],
        },
        "violations": violations,
        "checks": checks,
        "disclaimer": DISCLAIMER,
    }
