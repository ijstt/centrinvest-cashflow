from datetime import datetime, date
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.entities import Company, ForecastSnapshotRecord, DailyActualRecord, PlannedEventRecord
from src.schemas.contracts import (
    PlanActualResponse, PlanActualDay, PlannedEvent, SourceReference
)


async def refresh_latest_forecast(db, company_id, changed_from_date):
    previous = (await db.execute(select(ForecastSnapshotRecord).where(
        ForecastSnapshotRecord.company_id == company_id
    ).order_by(ForecastSnapshotRecord.created_at.desc(), ForecastSnapshotRecord.id.desc()).limit(1))).scalar_one_or_none()
    company = await db.get(Company, company_id)
    if previous is None or previous.data_revision == company.data_revision:
        return None
    from src.services.incremental_service import IncrementalService
    from src.schemas.contracts import ForecastUpdateRequest
    result = await IncrementalService.update_forecast(db, company_id, previous.id,
        ForecastUpdateRequest(changed_from_date=changed_from_date,
                              expected_data_revision=company.data_revision))
    return result.forecast.id

class ReconciliationService:
    @staticmethod
    async def get_plan_actual(
        db: AsyncSession,
        company_id: str,
        forecast_id: str,
        from_date: str | None = None,
        to_date: str | None = None,
        granularity: str = "day"
    ) -> PlanActualResponse:
        # Load snapshot
        snap_res = await db.execute(
            select(ForecastSnapshotRecord).where(
                ForecastSnapshotRecord.company_id == company_id,
                ForecastSnapshotRecord.id == forecast_id
            )
        )
        snap = snap_res.scalar_one_or_none()
        if not snap:
            raise KeyError(f"Forecast {forecast_id} not found")

        comp_res = await db.execute(select(Company).where(Company.id == company_id))
        comp = comp_res.scalar_one()

        # Load actuals
        act_res = await db.execute(
            select(DailyActualRecord).where(
                DailyActualRecord.company_id == company_id
            ).order_by(DailyActualRecord.date)
        )
        actuals_by_date = {a.date.strftime("%Y-%m-%d"): a for a in act_res.scalars().all()}

        # Load overdue events relative to latest known actuals or forecast horizon
        latest_act_date = max((a.date for a in actuals_by_date.values()), default=snap.as_of_date)
        ev_res = await db.execute(
            select(PlannedEventRecord).where(
                PlannedEventRecord.company_id == company_id,
                PlannedEventRecord.due_date <= latest_act_date,
                PlannedEventRecord.status.in_(["planned", "partial", "overdue"])
            )
        )
        overdue_records = ev_res.scalars().all()
        overdue_events = []
        for r in overdue_records:
            overdue_events.append(PlannedEvent(
                id=r.id,
                company_id=r.company_id,
                title=r.title,
                kind=r.kind,
                direction=r.direction,
                amount=float(r.amount),
                due_date=r.due_date.strftime("%Y-%m-%d"),
                category_id=r.category_id,
                counterparty_name=r.counterparty_name,
                on_time_probability=r.on_time_probability,
                source=SourceReference(
                    kind=r.source_kind,
                    source_id=r.source_id,
                    external_id=r.external_id,
                    label=r.source_label or r.title
                ),
                status="overdue",
                paid_amount=float(r.paid_amount),
                matched_transaction_ids=r.matched_transaction_ids or [],
                recurrence_id=r.recurrence_id,
                updated_at=r.updated_at.isoformat()
            ))

        # Build PlanActualDay list
        days: list[PlanActualDay] = []
        norm_from = from_date[:10] if from_date else None
        norm_to = to_date[:10] if to_date else None
        for d in snap.days:
            d_str = d["date"]
            if norm_from and d_str < norm_from:
                continue
            if norm_to and d_str > norm_to:
                continue

            p_inc = float(d["predicted_income"])
            p_exp = float(d["predicted_expense"])
            p_bal = float(d["predicted_balance"])

            act = actuals_by_date.get(d_str)
            if act and act.observation == "observed":
                a_inc = float(act.income or 0.0)
                a_exp = float(act.expense or 0.0)
                a_bal = float(act.closing_balance or 0.0)
                variance = round((a_inc - a_exp) - (p_inc - p_exp), 2)
            else:
                a_inc = None
                a_exp = None
                a_bal = None
                variance = None

            days.append(PlanActualDay(
                date=d_str,
                planned_income=p_inc,
                actual_income=a_inc,
                planned_expense=p_exp,
                actual_expense=a_exp,
                planned_balance=p_bal,
                actual_balance=a_bal,
                net_flow_variance=variance,
                overdue_event_ids=[oe.id for oe in overdue_events if oe.due_date == d_str]
            ))

        # Aggregate to weekly granularity if requested
        if granularity == "week" and len(days) > 1:
            weekly_days: list[PlanActualDay] = []
            for chunk_idx in range(0, len(days), 7):
                chunk = days[chunk_idx:chunk_idx + 7]
                w_date = chunk[0].date
                w_p_inc = round(sum(c.planned_income for c in chunk), 2)
                w_p_exp = round(sum(c.planned_expense for c in chunk), 2)
                w_p_bal = chunk[-1].planned_balance

                act_incomes = [c.actual_income for c in chunk if c.actual_income is not None]
                act_expenses = [c.actual_expense for c in chunk if c.actual_expense is not None]
                act_balances = [c.actual_balance for c in chunk if c.actual_balance is not None]

                if act_incomes:
                    w_a_inc = round(sum(act_incomes), 2)
                    w_a_exp = round(sum(act_expenses), 2)
                    w_a_bal = act_balances[-1]
                    obs_p_inc = sum(c.planned_income for c in chunk if c.actual_income is not None)
                    obs_p_exp = sum(c.planned_expense for c in chunk if c.actual_income is not None)
                    w_var = round((w_a_inc - w_a_exp) - (obs_p_inc - obs_p_exp), 2)
                else:
                    w_a_inc = None
                    w_a_exp = None
                    w_a_bal = None
                    w_var = None

                w_overdue = list(dict.fromkeys(ev_id for c in chunk for ev_id in c.overdue_event_ids))
                weekly_days.append(PlanActualDay(
                    date=w_date,
                    planned_income=w_p_inc,
                    actual_income=w_a_inc,
                    planned_expense=w_p_exp,
                    actual_expense=w_a_exp,
                    planned_balance=w_p_bal,
                    actual_balance=w_a_bal,
                    net_flow_variance=w_var,
                    overdue_event_ids=w_overdue
                ))
            days = weekly_days

        latest = (await db.execute(select(ForecastSnapshotRecord).where(
            ForecastSnapshotRecord.company_id == company_id,
            ForecastSnapshotRecord.data_revision == comp.data_revision
        ).order_by(ForecastSnapshotRecord.created_at.desc()).limit(1))).scalar_one_or_none()
        return PlanActualResponse(
            forecast_id=forecast_id,
            data_revision=comp.data_revision,
            days=days,
            overdue_events=overdue_events,
            updated_forecast_id=latest.id if latest and latest.id != snap.id else None
        )
