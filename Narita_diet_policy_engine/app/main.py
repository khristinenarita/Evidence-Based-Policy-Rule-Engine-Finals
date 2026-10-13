"""FastAPI backend for the dietary policy compliance engine.

Run:  uvicorn app.main:app --reload
Docs: http://127.0.0.1:8000/docs
"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .engine import (
    DISCLAIMER,
    InvalidPlanError,
    UnknownPolicyError,
    evaluate,
    load_foods,
    load_policies,
)

app = FastAPI(
    title="Davao City Dietary Policy Compliance Engine",
    version="1.0.0",
    description="Validates meal plans against codified hospital / national dietary policies. " + DISCLAIMER,
)

POLICIES = load_policies()
FOODS = load_foods()


class ItemIn(BaseModel):
    """One food entry: either a food_id from /foods, or inline per-serving nutrients."""

    model_config = ConfigDict(extra="forbid")
    food_id: str | None = None
    name: str | None = Field(default=None, max_length=80)
    servings: float = Field(default=1, gt=0, le=20)
    nutrients: dict[str, float] | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)


class MealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    type: Literal["main", "snack"] = "main"
    items: list[ItemIn] = Field(min_length=1, max_length=50)


class ValidateRequest(BaseModel):
    policy_id: str
    meals: list[MealIn] = Field(min_length=1, max_length=12)

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "policy_id": "diabetic_ccho",
                "meals": [
                    {"name": "Breakfast", "type": "main", "items": [
                        {"food_id": "oatmeal_cooked", "servings": 1}, {"food_id": "banana", "servings": 1}]},
                    {"name": "Lunch", "type": "main", "items": [
                        {"food_id": "brown_rice_cooked", "servings": 1},
                        {"food_id": "chicken_breast_grilled", "servings": 1},
                        {"food_id": "mixed_salad", "servings": 1}]},
                ],
            }
        },
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/policies")
def list_policies() -> list[dict]:
    return [
        {"id": p["id"], "name": p["name"], "description": p["description"], "jurisdiction": p["jurisdiction"],
         "status": p["status"], "rule_count": len(p["rules"])}
        for p in POLICIES.values()
    ]


@app.get("/policies/{policy_id}")
def get_policy(policy_id: str) -> dict:
    policy = POLICIES.get(policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail=f"unknown policy_id {policy_id!r}")
    return policy


@app.get("/foods")
def list_foods() -> list[dict]:
    return [
        {"id": f["id"], "name": f["name"], "serving": f["serving"], "tags": f.get("tags", []),
         "nutrients": f["nutrients"]}
        for f in FOODS.values()
    ]


@app.post("/validate")
def validate(req: ValidateRequest) -> dict:
    plan = {"meals": [m.model_dump(exclude_none=True) for m in req.meals]}
    try:
        return evaluate(plan, req.policy_id, POLICIES, FOODS)
    except UnknownPolicyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except InvalidPlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
