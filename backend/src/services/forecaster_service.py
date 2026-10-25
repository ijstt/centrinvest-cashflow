import uuid
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta, timezone
from typing import Literal
from src.services.forecast_model import predict_baseline, calibration_window
from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.entities import (
    Company, DailyActualRecord, PlannedEventRecord, RecurringPatternRecord, ForecastSnapshotRecord, TransactionRecord
)
from src.schemas.contracts import (
    CreateForecastRequest, ForecastSnapshot, ForecastDay, CashGap, ForecastWarning,
    ForecastSummary, DataQuality, ModelId, RiskLevel
)
from src.services.recurring_service import add_months, RecurringService
from src.services.forecast_components import compose_flows
import hashlib
import json
from types import SimpleNamespace
from src.services.math.fpt_solver import simulate_moving_block_bootstrap, compute_analytical_fpt
from src.services.math.conformal import calibrate_conformal_corridor


# Official Russian Government weekend transfers (Постановления Правительства РФ 2024-2026)
GOVERNMENT_TRANSFERS_HOLIDAYS = {
    # 2024 (Постановление Правительства РФ № 1314)
    date(2024, 4, 29), date(2024, 4, 30), date(2024, 5, 10), date(2024, 12, 30), date(2024, 12, 31),
    # 2025 (Постановление Правительства РФ № 1335)
    date(2025, 5, 2), date(2025, 5, 8), date(2025, 6, 13), date(2025, 11, 3), date(2025, 12, 31),
    # 2026
    date(2026, 1, 9), date(2026, 3, 9), date(2026, 5, 8), date(2026, 5, 11), date(2026, 12, 31),
}
GOVERNMENT_TRANSFERS_WORKING_WEEKENDS = {
    # 2024
    date(2024, 4, 27), date(2024, 11, 2), date(2024, 12, 28),
    # 2025
    date(2025, 11, 1),
}

def is_russian_holiday(d: date) -> bool:
    if d in GOVERNMENT_TRANSFERS_HOLIDAYS:
        return True
    if d.month == 1 and 1 <= d.day <= 8:
        return True
    if d.month == 2 and d.day == 23:
        return True
    if d.month == 3 and d.day == 8:
        return True
    if d.month == 5 and (d.day == 1 or d.day == 9):
        return True
    if d.month == 6 and d.day == 12:
        return True
    if d.month == 11 and d.day == 4:
        return True
    return False

def is_russian_working_day(d: date) -> bool:
    if d in GOVERNMENT_TRANSFERS_WORKING_WEEKENDS:
        return True
    if is_russian_holiday(d):
        return False
    return d.weekday() < 5

class ForecasterService:
    @staticmethod
    def combine_baseline_and_events(
        pred_income_base: np.ndarray,
        pred_expense_base: np.ndarray,
        recurring_inflow: np.ndarray,
        recurring_outflow: np.ndarray,
        additional_inflow: np.ndarray,
        additional_outflow: np.ndarray,
        base_recurring_inflow: np.ndarray | None = None,
        base_recurring_outflow: np.ndarray | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Component-based identity replacement (Count-Once invariant):
        - When baseline contains a known recurring component (e.g. 40 of 100 total baseline expense),
          adjusting or replacing that recurring component with a new planned amount (e.g. 50)
          subtracts the matched baseline component and adds the new planned recurring component:
            adj_base = max(0, pred_expense_base - base_recurring_outflow)
            total = adj_base + recurring_outflow + additional_outflow
            e.g. (100 - 40) + 50 + 0 = 110.0
        - When base_recurring is not specified, it defaults to the recurring event itself (capped at baseline),
          preserving the exact identity replacement (100 base + 100 rec => 100).
        """
        sub_out = base_recurring_outflow if base_recurring_outflow is not None else recurring_outflow
        sub_in = base_recurring_inflow if base_recurring_inflow is not None else recurring_inflow

        adj_pred_expense_base = np.maximum(0.0, pred_expense_base - sub_out)
        total_pred_expense = np.round(adj_pred_expense_base + recurring_outflow + additional_outflow, 2)

        adj_pred_income_base = np.maximum(0.0, pred_income_base - sub_in)
        total_pred_income = np.round(adj_pred_income_base + recurring_inflow + additional_inflow, 2)

        return total_pred_income, total_pred_expense


    @staticmethod
    async def create_forecast(
        db: AsyncSession,
        company_id: str,
        req: CreateForecastRequest,
        *, persist: bool = True, reusable_state: dict | None = None
    ) -> ForecastSnapshot:
        # Check company and data revision
        comp_res = await db.execute(select(Company).where(Company.id == company_id).with_for_update())
        comp = comp_res.scalar_one_or_none()
        if not comp:
            raise KeyError(f"Company {company_id} not found")

        # Invariant: stale expected_data_revision returns 409 conflict
        if req.expected_data_revision is not None and req.expected_data_revision != comp.data_revision:
            raise ValueError(f"conflict: Data revision mismatch. Expected {req.expected_data_revision}, found {comp.data_revision}")

        # Load daily actuals up to as_of_date
        as_of_d = datetime.strptime(req.as_of_date, "%Y-%m-%d").date()
        act_res = await db.execute(
            select(DailyActualRecord)
            .where(
                DailyActualRecord.company_id == company_id,
                DailyActualRecord.date <= as_of_d
            )
            .order_by(DailyActualRecord.date)
        )
        actuals = act_res.scalars().all()
        if not actuals:
            raise ValueError("insufficient_data: No historical actuals found on or before as_of_date")

        # Initial balance determination
        if req.initial_balance is not None:
            initial_balance = req.initial_balance
        else:
            # Look for closing balance on as_of_date or closest preceding date
            # Strict cutoff: only use actuals on or before as_of_date
            valid_actuals = [a for a in actuals if a.closing_balance is not None and a.date <= as_of_d]
            if valid_actuals:
                initial_balance = float(valid_actuals[-1].closing_balance)
            else:
                # Invariant: Never look ahead or invent 0 without explicit override
                raise ValueError(
                    "insufficient_data: No trustworthy closing balance found on or before as_of_date. "
                    "Please provide an explicit initial_balance override."
                )

        # Build daily history dataframe
        hist_records = []
        for a in actuals:
            hist_records.append({
                "date": a.date,
                "income": float(a.income or 0.0),
                "expense": float(a.expense or 0.0),
                "closing_balance": float(a.closing_balance or 0.0)
            })
        df_raw = pd.DataFrame(hist_records)
        df_raw["date"] = pd.to_datetime(df_raw["date"])
        df_raw = df_raw.sort_values("date")

        observed_days = len(df_raw)
        min_date = df_raw["date"].min()
        max_date = df_raw["date"].max()

        # Reindex to continuous daily calendar series so weekends have 0 flow and balance is carried forward
        full_idx = pd.date_range(min_date, max_date, freq="D")
        df_hist = df_raw.set_index("date").reindex(full_idx)
        df_hist["income"] = df_hist["income"].fillna(0.0)
        df_hist["expense"] = df_hist["expense"].fillna(0.0)
        df_hist["closing_balance"] = df_hist["closing_balance"].ffill().bfill()
        df_hist = df_hist.reset_index().rename(columns={"index": "date"})
        df_hist["weekday"] = df_hist["date"].dt.day_of_week

        # Check missing days on business days (Mon-Fri) excluding Russian holidays
        biz_dates = [bd for bd in full_idx if bd.day_of_week < 5 and not is_russian_holiday(bd.date())]
        observed_dates_set = set(df_raw["date"].dt.date)
        missing_biz_days = sum(1 for bd in biz_dates if bd.date() not in observed_dates_set)

        warnings_list: list[ForecastWarning] = []
        data_quality_warnings: list[str] = []

        if observed_days < 28:
            data_quality_warnings.append("Short history (<28 observed days). Prediction uncertainty is elevated.")
            warnings_list.append(ForecastWarning(
                id=str(uuid.uuid4()),
                code="short_history",
                severity="warning",
                title="Короткая история операций",
                message=f"В базе доступно только {observed_days} дней наблюдений. Рекомендуется загрузить выписку минимум за 3-6 месяцев.",
                date=req.as_of_date,
                amount=None,
                event_id=None
            ))

        if missing_biz_days > 5:
            data_quality_warnings.append(f"Detected {missing_biz_days} missing business days in historical statement.")
            warnings_list.append(ForecastWarning(
                id=str(uuid.uuid4()),
                code="missing_data",
                severity="info",
                title="Пропуски в истории операций",
                message=f"Обнаружено {missing_biz_days} рабочих дней без операций в выписке.",
                date=None,
                amount=None,
                event_id=None
            ))

        # Model selection
        chosen_model: ModelId = req.model if req.model != "auto" else "ridge_calendar"
        if observed_days < 14 and chosen_model == "ridge_calendar":
            chosen_model = "weekday_mean"

        # Fit baseline model
        horizon_days = req.horizon_days
        future_dates = [as_of_d + timedelta(days=i) for i in range(1, horizon_days + 1)]
        pred_income_base = np.zeros(horizon_days)
        pred_expense_base = np.zeros(horizon_days)

        history_key = hashlib.sha256(df_hist.to_json(date_format='iso').encode()).hexdigest()
        cache = reusable_state if (reusable_state and reusable_state.get('history_key') == history_key
            and len(reusable_state.get('baseline_income', [])) == horizon_days) else None
        if cache:
            chosen_model = cache['model']
            pred_income_base = np.array(cache['baseline_income'])
            pred_expense_base = np.array(cache['baseline_expense'])
        else:
            pred_income_base, pred_expense_base = predict_baseline(df_hist, future_dates, chosen_model)

        # Derive patterns only from facts known at this cutoff; GET never drives forecasts.
        tx_res = await db.execute(select(TransactionRecord).where(
            TransactionRecord.company_id == company_id,
            TransactionRecord.date <= as_of_d,
            TransactionRecord.source_kind != 'sandbox'))
        historical_transactions = list(tx_res.scalars())
        all_future_events = []
        recurring_pats = []
        if req.include_planned_events:
            ev_res = await db.execute(select(PlannedEventRecord).where(
                PlannedEventRecord.company_id == company_id,
                PlannedEventRecord.due_date >= future_dates[0],
                PlannedEventRecord.due_date <= future_dates[-1]))
            all_future_events = []
            known_receipts = {tx.id:float(tx.amount) for tx in historical_transactions}
            for event in ev_res.scalars():
                values = {column.name:getattr(event,column.name) for column in event.__table__.columns}
                if event.matched_transaction_ids and event.status != 'cancelled':
                    values['paid_amount'] = sum(known_receipts.get(key,0) for key in event.matched_transaction_ids)
                    values['status'] = ('paid' if values['paid_amount'] >= float(event.amount) else
                                        'partial' if values['paid_amount'] else 'planned')
                all_future_events.append(SimpleNamespace(**values))
            recurring_pats = await RecurringService.detect_recurring(db, company_id, as_of_d, persist=False)
            # Preserve explicitly linked, past-only patterns (including manually curated ones).
            linked_ids = {event.recurrence_id for event in all_future_events if event.recurrence_id}
            if linked_ids:
                stored = (await db.execute(select(RecurringPatternRecord).where(
                    RecurringPatternRecord.company_id == company_id,
                    RecurringPatternRecord.id.in_(linked_ids)))).scalars().all()
                known_ids = {tx.id for tx in historical_transactions}
                for pattern in stored:
                    if not pattern.transaction_ids or set(pattern.transaction_ids) <= known_ids:
                        recurring_pats = [p for p in recurring_pats if
                            (p.direction,p.category_id,p.counterparty_name) !=
                            (pattern.direction,pattern.category_id,pattern.counterparty_name)]
                        recurring_pats.append(pattern)
        total_pred_income, total_pred_expense, planned_event_ids_per_day, expense_components = compose_flows(
            future_dates, pred_income_base, pred_expense_base, all_future_events,
            recurring_pats, historical_transactions)

        # Compute point balance trajectory
        pred_balance = np.zeros(horizon_days)
        curr_b = round(initial_balance, 2)
        for i in range(horizon_days):
            curr_b = round(curr_b + round(float(total_pred_income[i]), 2) - round(float(total_pred_expense[i]), 2), 2)
            pred_balance[i] = curr_b

        # Bootstrap simulation (7-day block bootstrap) via mathematical FPT solver
        calibration_data = {}
        n_samples = 1000
        random_seed = 42
        w_inc_mean = df_hist.groupby("weekday")["income"].transform("mean").values
        w_exp_mean = df_hist.groupby("weekday")["expense"].transform("mean").values
        res_inc = df_hist["income"].values - w_inc_mean
        res_exp = df_hist["expense"].values - w_exp_mean

        if len(res_inc) >= 1:
            w0 = int(future_dates[0].weekday()) if future_dates else 0
            wh0 = int(df_hist["weekday"].iloc[0]) if "weekday" in df_hist.columns and len(df_hist) > 0 else 0
            sim_res = simulate_moving_block_bootstrap(
                initial_balance=initial_balance,
                pred_income=total_pred_income,
                pred_expense=total_pred_expense,
                res_income=res_inc,
                res_expense=res_exp,
                n_samples=n_samples,
                block_len=7,
                seed=random_seed,
                forecast_start_weekday=w0,
                history_start_weekday=wh0,
            )

            trajectories = sim_res["trajectories"]
            lower_bounds = sim_res["lower_bounds"]
            upper_bounds = sim_res["upper_bounds"]
            gap_probs = sim_res["daily_gap_probabilities"]
            any_gap_prob = sim_res["cumulative_gap_probability"]
            prob_method = "residual_block_bootstrap"
            interval_coverage = 0.80

            # Conformal calibration of prediction corridors using honest held-out validation slice
            if len(df_hist) >= 14:
                if cache and cache.get('calibration'):
                    h_act = np.array(cache['calibration']['actual'])
                    h_low = np.array(cache['calibration']['lower'])
                    h_upp = np.array(cache['calibration']['upper'])
                else:
                    window = calibration_window(df_hist, chosen_model)
                    h_act, h_low, h_upp = (np.array(window[key]) for key in ('actual','lower','upper'))

                calib = calibrate_conformal_corridor(
                    predicted_lower=np.array(lower_bounds),
                    predicted_upper=np.array(upper_bounds),
                    historical_actual=h_act,
                    historical_lower=h_low,
                    historical_upper=h_upp,
                    alpha=0.20,
                    point_predictions=pred_balance,
                )
                calibration_data = {'actual':h_act.tolist(), 'lower':h_low.tolist(), 'upper':h_upp.tolist()}
                lower_bounds = [float(x) for x in calib["calibrated_lower"]]
                upper_bounds = [float(x) for x in calib["calibrated_upper"]]
                interval_coverage = calib["nominal_coverage"]
                prob_method = "residual_block_bootstrap"
        else:
            lower_bounds = [float(b) for b in pred_balance]
            upper_bounds = [float(b) for b in pred_balance]
            gap_probs = [1.0 if b < 0 else 0.0 for b in pred_balance]
            any_gap_prob = 1.0 if np.any(pred_balance < 0) else 0.0
            prob_method = "unavailable"
            interval_coverage = 1.0
            trajectories = np.tile(pred_balance, (1000, 1))


        # Build ForecastDay list
        forecast_days: list[ForecastDay] = []
        for i, d in enumerate(future_dates):
            d_str = d.strftime("%Y-%m-%d")
            forecast_days.append(ForecastDay(
                date=d_str,
                predicted_income=float(total_pred_income[i]),
                predicted_expense=float(total_pred_expense[i]),
                predicted_balance=float(pred_balance[i]),
                balance_lower=float(lower_bounds[i]) if lower_bounds[i] is not None else None,
                balance_upper=float(upper_bounds[i]) if upper_bounds[i] is not None else None,
                gap_probability=float(gap_probs[i]) if gap_probs[i] is not None else None,
                planned_event_ids=planned_event_ids_per_day[i]
            ))

        # Compute cumulative ruin probabilities P(tau <= t)
        if trajectories is not None and len(trajectories) > 0 and trajectories.shape[1] == horizon_days:
            is_neg = trajectories < 0.0
            has_ruin_by_t = np.maximum.accumulate(is_neg, axis=1)
            cum_ruin_probs = np.mean(has_ruin_by_t, axis=0)
        else:
            cum_ruin_probs = np.zeros(horizon_days)

        # Detect contiguous CashGap intervals in point forecast and probabilistic ruin risk
        cash_gaps: list[CashGap] = []
        in_gap = False
        gap_start_idx = 0

        for i, b in enumerate(pred_balance):
            is_gap_day = (b < 0) or (cum_ruin_probs[i] > 0.20)
            if is_gap_day:
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
                        pred_balance=pred_balance,
                        trajectories=trajectories,
                        n_samples=n_samples,
                        planned_events=all_future_events,
                        lower_bounds=lower_bounds
                    ))
        if in_gap:
            cash_gaps.append(make_cash_gap(
                start_idx=gap_start_idx,
                end_idx=horizon_days - 1,
                future_dates=future_dates,
                pred_balance=pred_balance,
                trajectories=trajectories,
                n_samples=n_samples,
                planned_events=all_future_events,
                lower_bounds=lower_bounds
            ))

        # Check gap warnings and risk level
        min_balance = float(np.min(pred_balance))
        min_bal_idx = int(np.argmin(pred_balance))
        min_bal_date = future_dates[min_bal_idx].strftime("%Y-%m-%d")

        first_gap_date = None
        max_shortage = 0.0
        if cash_gaps:
            first_gap_date = cash_gaps[0].start_date
            max_shortage = round(float(max(g.shortage_amount for g in cash_gaps)), 2)
            warnings_list.append(ForecastWarning(
                id=str(uuid.uuid4()),
                code="cash_gap",
                severity="critical",
                title="Обнаружен кассовый разрыв",
                message=f"С {first_gap_date} прогнозируется отрицательный остаток на счете с максимальным дефицитом {max_shortage:,.2f} руб.",
                date=first_gap_date,
                amount=max_shortage,
                event_id=cash_gaps[0].contributing_event_ids[0] if cash_gaps[0].contributing_event_ids else None
            ))

        # Risk level determination
        if initial_balance < 0 or min_balance < 0 or (any_gap_prob is not None and any_gap_prob >= 0.5):
            risk_level: RiskLevel = "critical"
            if initial_balance < 0 and not any(w.title == "Текущий кассовый разрыв (овердрафт)" for w in warnings_list):
                warnings_list.append(ForecastWarning(
                    id=str(uuid.uuid4()),
                    code="cash_gap",
                    severity="critical",
                    title="Текущий кассовый разрыв (овердрафт)",
                    message=f"Начальный остаток на дату прогноза отрицательный ({initial_balance:,.2f} руб.). Компания находится в кассовом разрыве.",
                    date=as_of_d.strftime("%Y-%m-%d"),
                    amount=abs(initial_balance),
                    event_id=None
                ))
        elif (any_gap_prob is not None and any_gap_prob >= 0.2) or min_balance <= 0 or (initial_balance > 0 and min_balance < 0.2 * initial_balance):
            risk_level: RiskLevel = "medium"
            warnings_list.append(ForecastWarning(
                id=str(uuid.uuid4()),
                code="low_balance",
                severity="warning",
                title="Низкий уровень ликвидности",
                message=f"Минимальный остаток падает до {min_balance:,.2f} руб., что ниже резервного порога 20%.",
                date=min_bal_date,
                amount=min_balance,
                event_id=None
            ))
        else:
            risk_level: RiskLevel = "low"

        # Check for overdue events in history
        now_d = as_of_d
        overdue_res = await db.execute(
            select(PlannedEventRecord).where(
                PlannedEventRecord.company_id == company_id,
                PlannedEventRecord.due_date < now_d,
                PlannedEventRecord.direction == "income",
                PlannedEventRecord.status.in_(["planned", "partial", "overdue"])
            )
        )
        overdue_events = overdue_res.scalars().all()
        for oe in overdue_events:
            warnings_list.append(ForecastWarning(
                id=str(uuid.uuid4()),
                code="overdue_income",
                severity="warning",
                title="Просроченная дебиторская задолженность",
                message=f"Поступление по событию «{oe.title}» на сумму {float(oe.amount):,.2f} руб. просрочено с {oe.due_date.strftime('%Y-%m-%d')}.",
                date=oe.due_date.strftime("%Y-%m-%d"),
                amount=float(oe.amount),
                event_id=oe.id
            ))

        summary = ForecastSummary(
            closing_balance=initial_balance,
            end_balance=float(pred_balance[-1]),
            total_income=round(float(np.sum(total_pred_income)), 2),
            total_expense=round(float(np.sum(total_pred_expense)), 2),
            minimum_balance=min_balance,
            minimum_balance_date=min_bal_date,
            first_gap_date=first_gap_date,
            maximum_shortage=max_shortage,
            any_gap_probability=any_gap_prob,
            risk_level=risk_level
        )

        data_quality = DataQuality(
            observed_days=observed_days,
            missing_days=missing_biz_days,
            duplicate_rows=0,
            uncategorized_share=(sum(t.category_id == 'uncategorized' for t in historical_transactions)
                                 / len(historical_transactions)) if historical_transactions else 0.0,
            warnings=data_quality_warnings
        )

        # Save snapshot
        snapshot_rec = ForecastSnapshotRecord(
            id=str(uuid.uuid4()), created_at=datetime.now(timezone.utc),
            calculation_state={'request':req.model_dump(), 'history_key':history_key,
                'balance_date':comp.balance_date.isoformat() if comp.balance_date else None,
                'model':chosen_model, 'baseline_income':pred_income_base.tolist(),
                'baseline_expense':pred_expense_base.tolist(), 'calibration':calibration_data,
                'expense_components':expense_components},
            company_id=company_id,
            as_of_date=as_of_d,
            horizon_days=horizon_days,
            currency="RUB",
            data_revision=comp.data_revision,
            model=chosen_model,
            model_version="2.0.0",
            probability_method=prob_method,
            interval_coverage=interval_coverage,
            simulation_count=n_samples,
            random_seed=random_seed,
            summary=summary.model_dump(),
            days=[d.model_dump() for d in forecast_days],
            gaps=[g.model_dump() for g in cash_gaps],
            warnings=[w.model_dump() for w in warnings_list],
            data_quality=data_quality.model_dump()
        )
        if persist:
            db.add(snapshot_rec)
            await db.commit()
            await db.refresh(snapshot_rec)

        return ForecastSnapshot(
            id=snapshot_rec.id,
            company_id=snapshot_rec.company_id,
            created_at=snapshot_rec.created_at.isoformat(),
            as_of_date=req.as_of_date,
            horizon_days=horizon_days,
            currency="RUB",
            data_revision=comp.data_revision,
            model=chosen_model,
            model_version="2.0.0",
            evaluation_id=None,
            probability_method=prob_method,
            interval_coverage=interval_coverage,
            simulation_count=n_samples,
            random_seed=random_seed,
            summary=summary,
            days=forecast_days,
            gaps=cash_gaps,
            warnings=warnings_list,
            data_quality=data_quality
        )

def make_cash_gap(
    start_idx: int,
    end_idx: int,
    future_dates: list[date],
    pred_balance: np.ndarray,
    trajectories: np.ndarray,
    n_samples: int,
    planned_events: list,
    lower_bounds: list | np.ndarray | None = None
) -> CashGap:
    st_date = future_dates[start_idx]
    end_date = future_dates[end_idx]
    segment_bal = pred_balance[start_idx:end_idx + 1]
    min_seg_idx = start_idx + int(np.argmin(segment_bal))
    min_date = future_dates[min_seg_idx]

    if pred_balance[min_seg_idx] < 0:
        shortage = round(float(abs(pred_balance[min_seg_idx])), 2)
    else:
        seg_trajs = trajectories[:, start_idx:end_idx + 1] if (trajectories is not None and trajectories.size > 0) else np.array([])
        neg_vals = seg_trajs[seg_trajs < 0]
        if len(neg_vals) > 0:
            shortage = round(float(abs(np.percentile(neg_vals, 10))), 2)
        elif lower_bounds is not None and min_seg_idx < len(lower_bounds) and lower_bounds[min_seg_idx] is not None and lower_bounds[min_seg_idx] < 0:
            shortage = round(float(abs(lower_bounds[min_seg_idx])), 2)
        else:
            shortage = round(float(abs(np.min(seg_trajs))), 2) if (seg_trajs.size > 0 and np.min(seg_trajs) < 0) else 0.0

    # Probability: fraction of trajectories with any negative value in [start_idx, end_idx]
    if trajectories.shape[0] > 0 and trajectories.shape[1] > end_idx:
        seg_trajs = trajectories[:, start_idx:end_idx + 1]
        gap_prob = round(float(np.mean(np.any(seg_trajs < 0, axis=1))), 4)
    else:
        gap_prob = 1.0 if shortage > 0 else 0.0

    # Contributing events around gap start
    event_ids = []
    event_reasons = []
    for ev in planned_events:
        direction = getattr(ev, "direction", ev.get("direction") if isinstance(ev, dict) else None)
        # If event falls within 7 days before gap start or during gap
        if direction == "expense" and (st_date - timedelta(days=7)) <= ev.due_date <= end_date:
            event_ids.append(ev.id)
            event_reasons.append(f"{ev.title} ({float(ev.amount):,.0f} руб.)")

    if event_reasons:
        explanation = f"Разрыв сформирован крупными обязательствами: {', '.join(event_reasons[:3])} при превышении плановых расходов над поступлениями."
    else:
        explanation = f"Разрыв вызван структурным превышением регулярных операционных расходов над прогнозируемой выручкой бизнеса."

    return CashGap(
        start_date=st_date.strftime("%Y-%m-%d"),
        end_date=end_date.strftime("%Y-%m-%d"),
        minimum_balance_date=min_date.strftime("%Y-%m-%d"),
        shortage_amount=shortage,
        probability=gap_prob,
        contributing_event_ids=event_ids,
        explanation=explanation
    )
