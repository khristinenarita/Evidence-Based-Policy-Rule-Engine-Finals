# Davao City Dietary Policy Rule Engine

**Lab 1: Evidence-Based Policy Rule Engine** (Public Policy & Program Outcomes)

Codifies Philippine national dietary guidelines and institutional (hospital) meal policies
for Davao City into a rule-based validation engine. Policies are **data** in
`app/policies.json`; a generic engine checks any meal plan against them and returns an
auditable compliance report. Backend: Python FastAPI (plus a no-dependency fallback server and CLI).

```
policies.json + foods.json --> engine.py --> main.py (FastAPI POST /validate)
                                         --> cli.py / simple_server.py (no dependencies)
```

## Policy layers

| Layer | Policy ids | Nature |
|---|---|---|
| 1. Philippine national guideline | `ph_ngf_pinggang_pinoy` | Advisory. Plate composition per main meal (grain, protein, vegetable, fruit), about 1 cup rice per meal, and flag-for-review rules for salty, sugary, fried foods. All **warnings**. |
| 2. International numeric references | `low_sodium_2g`, `low_sodium_1500`, `diabetic_ccho`, `diabetic_low_sodium` | Clinical numeric limits (WHO, AHA, Dietary Guidelines for Americans, ADA). |
| 3. Davao hospital templates | `davao_hospital_low_sodium`, `davao_hospital_diabetic`, `davao_hospital_diabetic_low_sodium` | Layer 1 + Layer 2 combined. **Templates**: replace values with your hospital's diet manual. |

Policies compose with `"extends": [...]` (later entries override earlier ones with the same rule
id, so clinical `error` rules replace national `warning` rules) and `"disable_rules": [...]`
(drop an inherited rule that another rule supersedes).

## What is sourced and what is not

- **National guideline (verified):** DOST-FNRI Nutritional Guidelines for Filipinos (2012) are
  qualitative (limit salty, sugar-rich, fatty and fried foods; eat more vegetables and fruits;
  eat fish, lean meat, poultry, eggs, beans or nuts daily). Pinggang Pinoy divides the plate into
  grain, meat/fish, vegetable and fruit portions, with a cup of rice as the main feature of the
  grain portion. Because there are no numbers, they are codified as plate checks and flags.
- **Sodium 2,000 mg/day:** WHO benchmark (< 5 g salt), also cited in Davao media coverage of salt intake.
- **Not obtained:** the numeric tables of the Philippine Dietary Reference Intakes (PDRI 2015) and
  Pinggang Pinoy per-group proportions were not retrievable. Add them as rules in `policies.json`
  when you have the FNRI tables.
- **Hospital policies are templates.** No actual Davao hospital's diet manual is encoded. The 45-60 g
  carbohydrate meal range and per-meal sodium caps are typical example institutional values.
- **Food values** carry a `basis` field: `usda_approx` (approximated from USDA data) or `estimate`
  (typical-serving estimates for Filipino dishes, which vary by recipe). Replace with the Philippine
  Food Composition Table (FNRI-DOST) before real use.

Decision-support tool only; a registered nutritionist-dietitian should review real patient plans.

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # interactive docs at http://127.0.0.1:8000/docs
```

Without FastAPI (standard library only):

```bash
python -m app.simple_server 8000
python -m app.cli examples/davao_noncompliant_plan.json
python -m unittest discover -s tests -t . -v
```

```bash
curl -s localhost:8000/validate -H 'Content-Type: application/json' \
     -d @examples/davao_noncompliant_plan.json | python -m json.tool | head -40
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/policies` | list policies (with jurisdiction and status) |
| GET | `/policies/{id}` | full resolved policy: rules and sources |
| GET | `/foods` | food database (per-serving nutrients, tags, basis) |
| POST | `/validate` | validate a plan; 404 unknown policy, 422 bad plan |

Request: `{"policy_id": "davao_hospital_diabetic", "meals": [{"name": "Almusal", "type": "main", "items": [{"food_id": "pandesal", "servings": 2}]}]}`
(meal `type` is `main` or `snack`; use `snack` for merienda). Items may also be inline:
`{"name": "My ulam", "servings": 1, "nutrients": {"kcal": 300, "sodium_mg": 500}, "tags": ["protein"]}`.

Response: `compliant` (no error-level violations), `fully_compliant` (no violations at all), `summary`,
`totals` (day and per meal), `violations`, and `checks` (every rule evaluation, the audit trail).
Each check carries rule id, severity, actual value, limit, rationale, source and a fix hint.

## Rule types

| type | fields | meaning |
|---|---|---|
| `max` / `min` | `scope` (meal/day), `nutrient`, `value` | bound on a meal or day total |
| `pct_energy_max` | `nutrient` (sat_fat_g / added_sugar_g), `value` | % of daily kcal |
| `per_1000kcal_min` | `nutrient`, `value` | density minimum |
| `consistency` | `nutrient`, `max_difference` | spread across meals |
| `forbidden_tags` | `tags` | items carrying a tag are flagged |
| `required_tags_per_meal` | `any_of` | each meal needs one of these tags |
| `min_meals` | `value` | minimum number of meals |
| `tag_servings_max` | `scope`, `tags`, `value` | cap total servings of tagged foods (e.g. cups of rice) |

Common optional fields: `severity` (error/warning), `applies_to` (all/main/snack), `meal_names`,
`description`, `source`. Nutrients: `kcal, carbs_g, fiber_g, added_sugar_g, sat_fat_g, sodium_mg, protein_g`.

## Add a Davao hospital policy (no code)

```json
{
  "id": "my_hospital_renal",
  "name": "My Hospital - Renal Diet",
  "jurisdiction": "Davao City, Philippines",
  "institution": "My Hospital Dietary Department",
  "status": "active",
  "extends": ["ph_ngf_pinggang_pinoy"],
  "rules": [
    {"id": "protein_max", "type": "max", "scope": "day", "nutrient": "protein_g",
     "value": 60, "severity": "error", "description": "Protein at most 60 g/day.",
     "source": "Hospital diet manual, section X"}
  ]
}
```

## Design notes for your write-up

- **Policy as data:** nutritionists edit JSON; engineers maintain the handlers.
- **Explainability:** every check cites its rule, source and numbers.
- **Severity:** warnings model advisory national guidance, errors model hard clinical limits.
- **Local relevance:** Filipino meal pattern (almusal, tanghalian, hapunan, merienda), rice-portion rule, Davao foods (pomelo, durian, tuna, bangus), and high-sodium Filipino staples (bagoong, patis, tuyo, longganisa).
- **Limitations:** small food database; no per-patient adjustment (energy needs, renal function, medications); portions taken as given.
