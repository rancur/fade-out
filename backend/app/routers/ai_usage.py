"""AI usage tracking and budget endpoints."""

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AIUsage

router = APIRouter(prefix="/api/ai", tags=["ai_usage"])


# --- Schemas ---

class AIUsageOut(BaseModel):
    id: int
    mix_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    operation: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AIUsageListResponse(BaseModel):
    items: List[AIUsageOut]
    total: int
    page: int
    page_size: int


class UsageSummaryItem(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    operation: Optional[str] = None
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    request_count: int = 0


class UsageSummaryResponse(BaseModel):
    month: str
    items: List[UsageSummaryItem]
    total_cost_usd: float = 0.0


class BudgetResponse(BaseModel):
    monthly_budget: float
    spent_this_month: float
    remaining: float
    percentage_used: float


# --- Endpoints ---

@router.get("/usage", response_model=AIUsageListResponse)
async def list_ai_usage(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    start_date: Optional[date] = Query(None, description="Filter from this date (inclusive)"),
    end_date: Optional[date] = Query(None, description="Filter to this date (inclusive)"),
    db: AsyncSession = Depends(get_db),
):
    """List AI usage records with optional date range filtering and pagination."""
    query = select(AIUsage)
    count_query = select(sa_func.count()).select_from(AIUsage)

    if start_date:
        query = query.where(AIUsage.created_at >= datetime.combine(start_date, datetime.min.time()))
        count_query = count_query.where(AIUsage.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.where(AIUsage.created_at <= datetime.combine(end_date, datetime.max.time()))
        count_query = count_query.where(AIUsage.created_at <= datetime.combine(end_date, datetime.max.time()))

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(AIUsage.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    rows = result.scalars().all()

    return AIUsageListResponse(
        items=[AIUsageOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/usage/summary", response_model=UsageSummaryResponse)
async def ai_usage_summary(db: AsyncSession = Depends(get_db)):
    """Aggregate AI costs by provider, model, and operation for the current month."""
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    month_label = now.strftime("%Y-%m")

    query = (
        select(
            AIUsage.provider,
            AIUsage.model,
            AIUsage.operation,
            sa_func.sum(AIUsage.cost_usd).label("total_cost_usd"),
            sa_func.sum(AIUsage.input_tokens).label("total_input_tokens"),
            sa_func.sum(AIUsage.output_tokens).label("total_output_tokens"),
            sa_func.count().label("request_count"),
        )
        .where(AIUsage.created_at >= month_start)
        .group_by(AIUsage.provider, AIUsage.model, AIUsage.operation)
        .order_by(sa_func.sum(AIUsage.cost_usd).desc())
    )

    result = await db.execute(query)
    rows = result.all()

    items = [
        UsageSummaryItem(
            provider=row.provider,
            model=row.model,
            operation=row.operation,
            total_cost_usd=round(row.total_cost_usd or 0, 6),
            total_input_tokens=row.total_input_tokens or 0,
            total_output_tokens=row.total_output_tokens or 0,
            request_count=row.request_count,
        )
        for row in rows
    ]

    total_cost = sum(item.total_cost_usd for item in items)

    return UsageSummaryResponse(
        month=month_label,
        items=items,
        total_cost_usd=round(total_cost, 6),
    )


@router.get("/budget", response_model=BudgetResponse)
async def ai_budget(db: AsyncSession = Depends(get_db)):
    """Return current month AI budget status."""
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)

    result = await db.execute(
        select(sa_func.sum(AIUsage.cost_usd))
        .where(AIUsage.created_at >= month_start)
    )
    spent = result.scalar() or 0.0
    spent = round(spent, 4)

    from app.services import app_config

    budget = float(await app_config.resolve("ai_monthly_budget_usd"))
    remaining = round(max(budget - spent, 0.0), 4)
    percentage = round((spent / budget * 100) if budget > 0 else 0.0, 2)

    return BudgetResponse(
        monthly_budget=budget,
        spent_this_month=spent,
        remaining=remaining,
        percentage_used=percentage,
    )
