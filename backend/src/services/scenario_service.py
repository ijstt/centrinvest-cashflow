import copy
import uuid
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.entities import (
    Company, ForecastSnapshotRecord, ScenarioRecord, PlannedEventRecord,
    RecurringPatternRecord, DailyActualRecord
)
from src.schemas.contracts import (
    ScenarioRequest, ScenarioResponse, ScenarioDelta, ForecastSnapshot,
    ForecastDay, CashGap, ForecastWarning, ForecastSummary
)
from src.services.forecaster_service import make_cash_gap
from src.services.math.fpt_solver import simulate_moving_block_bootstrap

class ScenarioTransformResult(tuple):
    def __new__(cls, income, expense, deferred_receivables=None):
        return super().__new__(cls, (income, expense))

    def __init__(self, income, expense, deferred_receivables=None):
        self.stressed_income = income
        self.stressed_expense = expense
        self.deferred_receivables = deferred_receivables or []

class ScenarioService:
    @staticmethod
    def apply_scenario_transforms(
        future_dates: list[date],
        base_income: np.ndarray,
        base_expense: np.ndarray,
        adjustments: list,
        planned_events: list = None,
        base_event_ids: list[list[str]] = None,
        deferred_receivables: list | None = None,
        expense_components: dict | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Pure component-based scenario transformation with exact category decomposition.
        - delay_income: shifts specific event income by delay_days (14..28)
        - revenue_drop: discounts ONLY variable revenue, strictly protecting confirmed fixed client invoices and contracts
        - fixed_cost_increase: if category_ids specified, scales expenses belonging to those categories (including baseline)
        - extra_expense: adds one-off unexpected expense on designated date
        """
        horizon_days = len(future_dates)
        date_to_idx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(future_dates)}
        stressed_income = np.array(base_income, dtype=float).copy()
        stressed_expense = np.array(base_expense, dtype=float).copy()
        deferred_pool: list[dict] = []

        events_by_date: dict[date, list] = {}
        if planned_events:
            for ev in planned_events:
                ev_d = ev.due_date if isinstance(ev.due_date, date) else datetime.strptime(str(ev.due_date), "%Y-%m-%d").date()
                events_by_date.setdefault(ev_d, []).append(ev)

        # 1. Apply Delay Income
        delay_adjustments = [a for a in adjustments if getattr(a, "kind", None) == "delay_income" or (isinstance(a, dict) and a.get("kind") == "delay_income")]
        delayed_fixed_income_by_target: dict[str, float] = {}
        for da in delay_adjustments:
            delay_days = getattr(da, "delay_days", None) if not isinstance(da, dict) else da.get("delay_days")
            event_id = getattr(da, "event_id", None) if not isinstance(da, dict) else da.get("event_id")
            if not (14 <= delay_days <= 28):
                raise ValueError("validation_error: delay_days must be between 14 and 28")

            event_amount = None
            is_fixed_receivable = False
            if planned_events and event_id:
                for ev in planned_events:
                    if str(getattr(ev, "id", "")) == str(event_id):
                        amt = float(getattr(ev, "amount", 0.0)) - float(getattr(ev, "paid_amount", 0.0))
                        event_amount = amt
                        if getattr(ev, "kind", "") in ["invoice", "contract", "fixed_invoice"]:
                            is_fixed_receivable = True
                        break

            for i, d in enumerate(future_dates):
                day_ev_ids = base_event_ids[i] if base_event_ids and i < len(base_event_ids) else []
                if event_id in day_ev_ids or (planned_events and any(str(getattr(ev, "id", "")) == str(event_id) for ev in events_by_date.get(d, []))):
                    target_d = d + timedelta(days=delay_days)
                    target_d_str = target_d.strftime("%Y-%m-%d")
                    shift_amount = event_amount if event_amount is not None else stressed_income[i]
                    shift_amount = min(shift_amount, stressed_income[i])
                    stressed_income[i] = round(max(0.0, stressed_income[i] - shift_amount), 2)
                    if target_d_str in date_to_idx:
                        t_idx = date_to_idx[target_d_str]
                        stressed_income[t_idx] = round(stressed_income[t_idx] + shift_amount, 2)
                        if is_fixed_receivable:
                            delayed_fixed_income_by_target[target_d_str] = (
                                delayed_fixed_income_by_target.get(target_d_str, 0.0) + shift_amount
                            )
                    else:
                        deferred_item = {
                            "event_id": str(event_id),
                            "original_date": d.strftime("%Y-%m-%d"),
                            "target_date": target_d_str,
                            "amount": shift_amount,
                            "delay_days": delay_days,
                            "is_fixed_receivable": is_fixed_receivable,
                            "status": "deferred_beyond_horizon"
                        }
                        deferred_pool.append(deferred_item)
                        if deferred_receivables is not None:
                            deferred_receivables.append(deferred_item)
                    break

        delayed_event_ids = {
            str(getattr(da, "event_id", "")) if not isinstance(da, dict) else str(da.get("event_id", ""))
            for da in delay_adjustments
        }

        # 2. Apply Revenue Drop (10..30%, capped cumulatively at 30% per day)
        # Protect fixed confirmed invoices and contracts from market drop!
        rev_adjustments = [a for a in adjustments if getattr(a, "kind", None) == "revenue_drop" or (isinstance(a, dict) and a.get("kind") == "revenue_drop")]
        cumulative_drop = np.zeros(horizon_days)
        for ra in rev_adjustments:
            pct = getattr(ra, "percent", None) if not isinstance(ra, dict) else ra.get("percent")
            if pct is not None and 0.0 < pct <= 1.0:
                pct = pct * 100.0
            if pct is None or not (10.0 <= pct <= 30.0):
                raise ValueError("validation_error: revenue_drop percent must be between 10% and 30%")
            from_str = getattr(ra, "from_date", None) if not isinstance(ra, dict) else ra.get("from_date")
            to_str = getattr(ra, "to_date", None) if not isinstance(ra, dict) else ra.get("to_date")
            from_d = from_str if isinstance(from_str, date) else datetime.strptime(str(from_str)[:10], "%Y-%m-%d").date()
            to_d = to_str if isinstance(to_str, date) else datetime.strptime(str(to_str)[:10], "%Y-%m-%d").date()
            for i, d in enumerate(future_dates):
                if from_d <= d <= to_d:
                    cumulative_drop[i] = min(30.0, cumulative_drop[i] + pct)

        for i, d in enumerate(future_dates):
            if cumulative_drop[i] > 0:
                d_str = d.strftime("%Y-%m-%d")
                day_evs = events_by_date.get(d, [])
                fixed_income = sum(
                    max(0.0, float(getattr(ev, "amount", 0.0)) - float(getattr(ev, "paid_amount", 0.0)))
                    for ev in day_evs
                    if getattr(ev, "direction", "income") == "income"
                    and getattr(ev, "kind", "") in ["invoice", "contract", "fixed_invoice"]
                    and getattr(ev, "status", "planned") not in ["cancelled", "deleted"]
                    and str(getattr(ev, "id", "")) not in delayed_event_ids
                )
                fixed_income += delayed_fixed_income_by_target.get(d_str, 0.0)

                variable_revenue = max(0.0, stressed_income[i] - fixed_income)
                drop_amount = variable_revenue * (cumulative_drop[i] / 100.0)
                stressed_income[i] = round(stressed_income[i] - drop_amount, 2)

        # 3. Apply Fixed Cost Increase (0..100%)
        cost_adjustments = [a for a in adjustments if getattr(a, "kind", None) == "fixed_cost_increase" or (isinstance(a, dict) and a.get("kind") == "fixed_cost_increase")]
        for ca in cost_adjustments:
            pct = getattr(ca, "percent", None) if not isinstance(ca, dict) else ca.get("percent")
            if pct is not None and 0.0 < pct <= 1.0:
                pct = pct * 100.0
            cat_ids = getattr(ca, "category_ids", []) if not isinstance(ca, dict) else ca.get("category_ids", [])
            if pct is None or not (0.0 <= pct <= 100.0):
                raise ValueError("validation_error: fixed_cost_increase percent must be between 0% and 100%")
            from_str = getattr(ca, "from_date", None) if not isinstance(ca, dict) else ca.get("from_date")
            to_str = getattr(ca, "to_date", None) if not isinstance(ca, dict) else ca.get("to_date")
            from_d = from_str if isinstance(from_str, date) else datetime.strptime(str(from_str)[:10], "%Y-%m-%d").date()
            to_d = to_str if isinstance(to_str, date) else datetime.strptime(str(to_str)[:10], "%Y-%m-%d").date()

            for i, d in enumerate(future_dates):
                if from_d <= d <= to_d:
                    if cat_ids:
                        day_evs = events_by_date.get(d, [])
                        day_exp_evs = [
                            ev for ev in day_evs
                            if getattr(ev, "direction", "expense") == "expense"
                            and getattr(ev, "status", "planned") not in ["cancelled", "deleted"]
                        ]
                        matching_cat_expense = sum(
                            max(0.0, float(getattr(ev, "amount", 0.0)) - float(getattr(ev, "paid_amount", 0.0)))
                            for ev in day_exp_evs
                            if getattr(ev, "category_id", "") in cat_ids
                        )
                        affected = (sum(expense_components.get(category, [0.0]*horizon_days)[i]
                                        for category in cat_ids) if expense_components is not None
                                    else matching_cat_expense)
                        stressed_expense[i] = round(stressed_expense[i] + affected * pct / 100.0, 2)
                    else:
                        stressed_expense[i] = round(stressed_expense[i] * (1.0 + (pct / 100.0)), 2)

        # 4. Apply Extra Expense (>0)
        extra_adjustments = [a for a in adjustments if getattr(a, "kind", None) == "extra_expense" or (isinstance(a, dict) and a.get("kind") == "extra_expense")]
        for ea in extra_adjustments:
            amt = getattr(ea, "amount", None) if not isinstance(ea, dict) else ea.get("amount")
            ea_date = getattr(ea, "date", None) if not isinstance(ea, dict) else ea.get("date")
            if amt <= 0:
                raise ValueError("validation_error: extra_expense amount must be positive")
            date_key = ea_date if isinstance(ea_date, str) else ea_date.strftime("%Y-%m-%d")
            if date_key in date_to_idx:
                idx = date_to_idx[date_key]
                stressed_expense[idx] = round(stressed_expense[idx] + amt, 2)

        return ScenarioTransformResult(stressed_income, stressed_expense, deferred_pool)

    @staticmethod
    async def simulate_scenario(
        db: AsyncSession,
        company_id: str,
        base_forecast_id: str,
        req: ScenarioRequest
    ) -> ScenarioResponse:
        # Load base forecast snapshot
        res = await db.execute(
            select(ForecastSnapshotRecord).where(
                ForecastSnapshotRecord.company_id == company_id,
                ForecastSnapshotRecord.id == base_forecast_id
            )
        )
        base_rec = res.scalar_one_or_none()
        if not base_rec:
            raise KeyError(f"Base forecast {base_forecast_id} not found")

        if len(req.adjustments) > 8:
            raise ValueError("validation_error: Maximum 8 adjustments allowed per scenario")

        # Deep copy base forecast days to modify
        base_days = [ForecastDay(**d) for d in base_rec.days]
        horizon_days = len(base_days)
        as_of_d = base_rec.as_of_date
        future_dates = [as_of_d + timedelta(days=i + 1) for i in range(horizon_days)]
        base_event_ids = [d.planned_event_ids for d in base_days]

        # Load planned events for the horizon
        ev_query = await db.execute(
            select(PlannedEventRecord).where(
                PlannedEventRecord.company_id == company_id,
                PlannedEventRecord.due_date >= future_dates[0],
                PlannedEventRecord.due_date <= future_dates[-1]
            )
        )
        all_future_events = list(ev_query.scalars().all())

        base_income = np.array([d.predicted_income for d in base_days], dtype=float)
        base_expense = np.array([d.predicted_expense for d in base_days], dtype=float)

        # Apply pure component-decomposed transforms
        stressed_income, stressed_expense = ScenarioService.apply_scenario_transforms(
            future_dates=future_dates,
            base_income=base_income,
            base_expense=base_expense,
            adjustments=req.adjustments,
            planned_events=all_future_events,
            base_event_ids=base_event_ids,
            expense_components=(base_rec.calculation_state or {}).get('expense_components')
        )

        # Recompute balance trajectory
        init_bal = float(base_rec.summary["closing_balance"])
        stressed_balance = np.zeros(horizon_days)
        curr_b = round(init_bal, 2)
        for i in range(horizon_days):
            curr_b = round(curr_b + round(float(stressed_income[i]), 2) - round(float(stressed_expense[i]), 2), 2)
            stressed_balance[i] = curr_b

        # Load historical residuals for moving block bootstrap
        act_res = await db.execute(
            select(DailyActualRecord)
            .where(
                DailyActualRecord.company_id == company_id,
                DailyActualRecord.date <= as_of_d
            )
            .order_by(DailyActualRecord.date)
        )
        actuals = act_res.scalars().all()
        n_samples = 1000
        random_seed = 42

        if actuals:
            hist_records = [{
                "date": a.date,
                "income": float(a.income or 0.0),
                "expense": float(a.expense or 0.0),
            } for a in actuals]
            df_raw = pd.DataFrame(hist_records)
            df_raw["date"] = pd.to_datetime(df_raw["date"])
            min_date = df_raw["date"].min()
            max_date = df_raw["date"].max()
            full_idx = pd.date_range(min_date, max_date, freq="D")
            df_hist = df_raw.set_index("date").reindex(full_idx).fillna(0.0)
            df_hist["weekday"] = df_hist.index.day_of_week
            w_inc_mean = df_hist.groupby("weekday")["income"].transform("mean").values
            w_exp_mean = df_hist.groupby("weekday")["expense"].transform("mean").values
            res_inc = df_hist["income"].values - w_inc_mean
            res_exp = df_hist["expense"].values - w_exp_mean
        else:
            res_inc = np.zeros(14)
            res_exp = np.zeros(14)

        w0 = int(future_dates[0].weekday()) if future_dates else 0
        wh0 = int(actuals[0].date.weekday()) if actuals else 0
        sim_res = simulate_moving_block_bootstrap(
            initial_balance=init_bal,
            pred_income=stressed_income,
            pred_expense=stressed_expense,
            res_income=res_inc,
            res_expense=res_exp,
            n_samples=n_samples,
            block_len=7,
            seed=random_seed,
            forecast_start_weekday=w0,
            history_start_weekday=wh0,
        )

        scenario_trajs = sim_res["trajectories"]
        lower_bounds = sim_res["lower_bounds"]
        upper_bounds = sim_res["upper_bounds"]
        daily_gap_probs = sim_res["daily_gap_probabilities"]
        any_gap_prob = sim_res["cumulative_gap_probability"]

        # Stressed days
        stressed_days: list[ForecastDay] = []
        for i, d in enumerate(base_days):
            stressed_days.append(ForecastDay(
                date=d.date,
                predicted_income=float(stressed_income[i]),
                predicted_expense=float(stressed_expense[i]),
                predicted_balance=float(stressed_balance[i]),
                balance_lower=round(float(lower_bounds[i]), 2),
                balance_upper=round(float(upper_bounds[i]), 2),
                gap_probability=round(float(daily_gap_probs[i]), 4),
                planned_event_ids=d.planned_event_ids
            ))

        # Stressed CashGaps
        cash_gaps: list[CashGap] = []
        in_gap = False
        gap_start_idx = 0

        scenario_events = list(all_future_events)
        class SyntheticScenarioEvent:
            def __init__(self, ev_id: str, due_date: date, amount: float, title: str):
                self.id = ev_id
                self.due_date = due_date
                self.amount = amount
                self.title = title
                self.direction = "expense"

        extra_adjustments = [a for a in req.adjustments if a.kind == "extra_expense"]
        for idx, ea in enumerate(extra_adjustments):
            try:
                ea_d = datetime.strptime(ea.date, "%Y-%m-%d").date()
                scenario_events.append(SyntheticScenarioEvent(
                    ev_id=f"shock-extra-{idx+1}",
                    due_date=ea_d,
                    amount=ea.amount,
                    title=f"Стресс-расход {ea.amount:,.0f} руб."
                ))
            except Exception:
                pass

        for i, b in enumerate(stressed_balance):
            if b < 0:
                if not in_gap:
                    in_gap = True
                    gap_start_idx = i
            else:
                if in_gap:
                    in_gap = False
                    cash_gaps.append(make_cash_gap(
                        start_idx=gap_start_idx,
                        end_idx=i - 1,
                        future_dates=future_dates,
                        pred_balance=stressed_balance,
                        trajectories=scenario_trajs,
                        n_samples=n_samples,
                        planned_events=scenario_events
                    ))
        if in_gap:
            cash_gaps.append(make_cash_gap(
                start_idx=gap_start_idx,
                end_idx=horizon_days - 1,
                future_dates=future_dates,
                pred_balance=stressed_balance,
                trajectories=scenario_trajs,
                n_samples=n_samples,
                planned_events=scenario_events
            ))

        min_bal = float(np.min(stressed_balance))
        min_bal_idx = int(np.argmin(stressed_balance))
        min_bal_date = future_dates[min_bal_idx].strftime("%Y-%m-%d")

        first_gap_date = cash_gaps[0].start_date if cash_gaps else None
        max_shortage = round(float(max(g.shortage_amount for g in cash_gaps)), 2) if cash_gaps else 0.0

        risk_level = "critical" if min_bal < 0 else "medium" if min_bal < (0.2 * init_bal) else "low"

        stressed_summary = ForecastSummary(
            closing_balance=init_bal,
            end_balance=float(stressed_balance[-1]),
            total_income=round(float(np.sum(stressed_income)), 2),
            total_expense=round(float(np.sum(stressed_expense)), 2),
            minimum_balance=min_bal,
            minimum_balance_date=min_bal_date,
            first_gap_date=first_gap_date,
            maximum_shortage=max_shortage,
            any_gap_probability=round(float(any_gap_prob), 4),
            risk_level=risk_level
        )

        stressed_snapshot = ForecastSnapshot(
            id=str(uuid.uuid4()),
            company_id=company_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            as_of_date=base_rec.as_of_date.strftime("%Y-%m-%d"),
            horizon_days=base_rec.horizon_days,
            currency="RUB",
            data_revision=base_rec.data_revision,
            model=base_rec.model,
            model_version=base_rec.model_version,
            evaluation_id=base_rec.evaluation_id,
            probability_method="residual_block_bootstrap",
            interval_coverage=0.80,
            simulation_count=n_samples,
            random_seed=random_seed,
            summary=stressed_summary,
            days=stressed_days,
            gaps=cash_gaps,
            warnings=[ForecastWarning(**w) for w in base_rec.warnings],
            data_quality=base_rec.data_quality
        )

        # Delta calculation
        base_end_bal = float(base_rec.summary["end_balance"])
        base_max_shortage = float(base_rec.summary.get("maximum_shortage", 0.0))
        base_first_gap = base_rec.summary.get("first_gap_date")

        first_gap_shift = None
        if first_gap_date and base_first_gap:
            d_stress = datetime.strptime(first_gap_date, "%Y-%m-%d").date()
            d_base = datetime.strptime(base_first_gap, "%Y-%m-%d").date()
            first_gap_shift = (d_stress - d_base).days
        elif first_gap_date and not base_first_gap:
            d_stress = datetime.strptime(first_gap_date, "%Y-%m-%d").date()
            first_gap_shift = (d_stress - as_of_d).days

        delta = ScenarioDelta(
            end_balance=round(float(stressed_balance[-1] - base_end_bal), 2),
            maximum_shortage=round(float(max_shortage - base_max_shortage), 2),
            first_gap_shift_days=first_gap_shift
        )

        scenario_rec = ScenarioRecord(
            company_id=company_id,
            base_forecast_id=base_forecast_id,
            name=req.name,
            adjustments=[a.model_dump() for a in req.adjustments],
            forecast_snapshot=stressed_snapshot.model_dump(),
            delta=delta.model_dump()
        )
        db.add(scenario_rec)
        await db.commit()
        await db.refresh(scenario_rec)

        return ScenarioResponse(
            id=scenario_rec.id,
            base_forecast_id=base_forecast_id,
            name=req.name,
            adjustments=req.adjustments,
            forecast=stressed_snapshot,
            delta=delta
        )
