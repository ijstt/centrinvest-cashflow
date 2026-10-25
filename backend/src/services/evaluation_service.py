import math
import uuid
import numpy as np
import pandas as pd
from datetime import datetime, date, timedelta
from typing import Literal
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.entities import Company, DailyActualRecord, EvaluationRecord
from src.schemas.contracts import (
    EvaluationRequest, EvaluationResponse, EvaluationFold, ModelMetrics, ModelId
)
from src.services.math import norm_cdf, compute_wape, compute_brier_score


class EvaluationService:
    @staticmethod
    async def evaluate_models(
        db: AsyncSession,
        company_id: str,
        req: EvaluationRequest
    ) -> EvaluationResponse:
        comp_res = await db.execute(select(Company).where(Company.id == company_id).with_for_update())
        comp = comp_res.scalar_one_or_none()
        if not comp:
            raise KeyError(f"Company {company_id} not found")

        # Load all daily actuals up to training_end_date
        end_d = datetime.strptime(req.training_end_date, "%Y-%m-%d").date()
        act_res = await db.execute(
            select(DailyActualRecord)
            .where(
                DailyActualRecord.company_id == company_id,
                DailyActualRecord.date <= end_d
            )
            .order_by(DailyActualRecord.date)
        )
        actuals = act_res.scalars().all()
        if len(actuals) < 45:
            raise ValueError(f"insufficient_data: Need at least 45 historical days for evaluation, found {len(actuals)}")

        records = []
        for a in actuals:
            records.append({
                "date": a.date,
                "income": float(a.income or 0.0),
                "expense": float(a.expense or 0.0),
                "closing_balance": float(a.closing_balance) if a.closing_balance is not None else np.nan,
                "observation": getattr(a, "observation", "observed")
            })
        df_raw = pd.DataFrame(records)
        df_raw["date"] = pd.to_datetime(df_raw["date"])
        df_raw = df_raw.sort_values("date")

        min_date = df_raw["date"].min()
        max_date = df_raw["date"].max()
        full_idx = pd.date_range(min_date, max_date, freq="D")
        df = df_raw.set_index("date").reindex(full_idx)
        df["observation"] = df["observation"].fillna("missing")
        df["income"] = df["income"].fillna(0.0)
        df["expense"] = df["expense"].fillna(0.0)
        df["closing_balance"] = df["closing_balance"].ffill()
        df = df.reset_index().rename(columns={"index": "date"})
        df["weekday"] = df["date"].dt.day_of_week

        test_days = req.test_days
        num_folds = min(req.folds, max(1, (len(df) - 30) // test_days))
        if num_folds < 1:
            num_folds = 1

        # Construct non-overlapping rolling origin folds from the end
        folds: list[EvaluationFold] = []
        n_total = len(df)
        for k in range(num_folds):
            test_end_idx = n_total - 1 - (k * test_days)
            test_start_idx = test_end_idx - test_days + 1
            train_end_idx = test_start_idx - 1

            if train_end_idx < 14:
                break

            folds.append(EvaluationFold(
                train_end=df["date"].iloc[train_end_idx].strftime("%Y-%m-%d"),
                test_start=df["date"].iloc[test_start_idx].strftime("%Y-%m-%d"),
                test_end=df["date"].iloc[test_end_idx].strftime("%Y-%m-%d")
            ))

        if not folds:
            raise ValueError("insufficient_data: History too short to construct temporal folds")

        models_to_test = req.models or ["seasonal_naive", "weekday_mean", "ridge_calendar"]
        model_results: dict[str, dict] = {m: {
            "inc_errs": [], "exp_errs": [], "bal_errs": [],
            "tot_actual_flow": 0.0, "tot_abs_error": 0.0,
            "gap_tp": 0, "gap_fp": 0, "gap_fn": 0, "gap_tn": 0,
            "brier_sum": 0.0, "coverage_hits": 0, "lead_days_list": [], "total_test_days": 0
        } for m in models_to_test}

        for fold in folds:
            train_mask = df["date"] <= pd.to_datetime(fold.train_end)
            test_mask = (df["date"] >= pd.to_datetime(fold.test_start)) & (df["date"] <= pd.to_datetime(fold.test_end))

            df_train = df[train_mask]
            df_test = df[test_mask]
            H = len(df_test)
            if H == 0:
                continue

            init_bal = df_train["closing_balance"].iloc[-1]
            act_inc = df_test["income"].values
            act_exp = df_test["expense"].values
            act_bal = df_test["closing_balance"].values

            # Ground truth balance: use verified bank closing_balance act_bal where present;
            # fallback to init_bal + cumsum flow only if act_bal is missing/NaN
            true_bal_traj = np.where(np.isnan(act_bal), init_bal + np.cumsum(act_inc - act_exp), act_bal)

            for m in models_to_test:
                # Evaluate exactly the runtime model and uncertainty pipeline on past-only inputs.
                from src.services.forecaster_service import ForecasterService
                from src.schemas.contracts import CreateForecastRequest
                snapshot = await ForecasterService.create_forecast(db, company_id,
                    CreateForecastRequest(as_of_date=fold.train_end, horizon_days=test_days,
                        initial_balance=None, include_planned_events=False, model=m,
                        expected_data_revision=comp.data_revision), persist=False)
                pred_inc = np.array([day.predicted_income for day in snapshot.days])
                pred_exp = np.array([day.predicted_expense for day in snapshot.days])
                pred_bal = np.array([day.predicted_balance for day in snapshot.days])

                obs_mask = (df_test["observation"] != "missing").values
                if not np.any(obs_mask):
                    continue

                # Collect metrics on valid observed or confirmed days
                inc_err = np.abs(act_inc - pred_inc)[obs_mask]
                exp_err = np.abs(act_exp - pred_exp)[obs_mask]
                bal_err = np.abs(true_bal_traj - pred_bal)[obs_mask]

                res = model_results[m]
                res["inc_errs"].extend(inc_err.tolist())
                res["exp_errs"].extend(exp_err.tolist())
                res["bal_errs"].extend(bal_err.tolist())
                res["tot_actual_flow"] += float(np.sum(act_inc[obs_mask] + act_exp[obs_mask]))
                res["tot_abs_error"] += float(np.sum(inc_err + exp_err))
                res["total_test_days"] += int(np.sum(obs_mask))

                # Genuine empirical coverage, probabilistic Brier score and gap classification
                for i in range(H):
                    if not obs_mask[i]:
                        continue
                    step_t = i + 1
                    forecast_day = snapshot.days[i]
                    bal_lower, bal_upper = forecast_day.balance_lower, forecast_day.balance_upper
                    if bal_lower is not None and bal_lower <= true_bal_traj[i] <= bal_upper:
                        res['coverage_hits'] += 1
                    prob_gap = forecast_day.gap_probability
                    if prob_gap is None:
                        raise ValueError('unavailable: Runtime risk estimate is unavailable')
                    actual_gap = true_bal_traj[i] < 0
                    actual_binary = 1.0 if actual_gap else 0.0
                    res["brier_sum"] += (prob_gap - actual_binary) ** 2

                    pred_gap = pred_bal[i] < 0
                    if actual_gap and pred_gap:
                        res["gap_tp"] += 1
                        res["lead_days_list"].append(float(step_t))
                    elif not actual_gap and pred_gap:
                        res["gap_fp"] += 1
                    elif actual_gap and not pred_gap:
                        res["gap_fn"] += 1
                    else:
                        res["gap_tn"] += 1

        metrics_list: list[ModelMetrics] = []
        for m in models_to_test:
            res = model_results[m]
            n_eval = res["total_test_days"]
            if n_eval == 0:
                continue

            mae_i = round(float(np.mean(res["inc_errs"])), 2)
            mae_e = round(float(np.mean(res["exp_errs"])), 2)
            mae_b = round(float(np.mean(res["bal_errs"])), 2)
            wape = round(float(res["tot_abs_error"] / res["tot_actual_flow"]), 4) if res["tot_actual_flow"] > 0 else None

            tp, fp, fn = res["gap_tp"], res["gap_fp"], res["gap_fn"]
            gap_prec = round(float(tp / (tp + fp)), 4) if (tp + fp) > 0 else None
            gap_rec = round(float(tp / (tp + fn)), 4) if (tp + fn) > 0 else None
            cov_emp = round(float(res["coverage_hits"] / n_eval), 4) if n_eval > 0 else None
            brier_sc = round(float(res["brier_sum"] / n_eval), 4) if n_eval > 0 else None
            avg_lead = round(float(np.mean(res["lead_days_list"])), 1) if res["lead_days_list"] else None

            metrics_list.append(ModelMetrics(
                model=m,
                mae_income=mae_i,
                mae_expense=mae_e,
                mae_balance=mae_b,
                wape_cashflow=wape,
                interval_empirical_coverage=cov_emp,
                gap_precision=gap_prec,
                gap_recall=gap_rec,
                average_warning_lead_days=avg_lead,
                gap_brier_score=brier_sc,
                evaluated_days=n_eval
            ))

        # Select best model using composite multi-objective ranking (daily flow WAPE + balance MAE)
        # Normalize MAE balance by objective actual flow scale rather than model's own flow error
        mean_daily_actual_turnover = float((df["income"] + df["expense"]).mean())
        actual_flow_scale = max(mean_daily_actual_turnover, 1.0)
        if math.isnan(actual_flow_scale) or actual_flow_scale < 1.0:
            actual_flow_scale = 1.0

        def model_rank_score(m: ModelMetrics) -> float:
            wape = m.wape_cashflow if m.wape_cashflow is not None else 1.0
            bal_ratio = m.mae_balance / max(actual_flow_scale * m.evaluated_days, 1.0)
            return 0.6 * wape + 0.4 * bal_ratio

        metrics_list.sort(key=lambda x: (model_rank_score(x), x.wape_cashflow if x.wape_cashflow is not None else 1.0, x.mae_balance))
        best_model = metrics_list[0].model
        best_mae = metrics_list[0].mae_balance

        reason = (
            f"Модель «{best_model}» показала оптимальный композитный баланс точности суточных потоков (WAPE) "
            f"и ошибки баланса (MAE = {best_mae:,.2f} руб.) на {len(folds)} скользящих временных окнах по {test_days} дней."
        )

        limitations = [
            "Оценка проведена на исторических данных без учета будущих внешних макроэкономических шоков.",
            "Прогноз выручки предполагает сохранение сезонных паттернов и структуры рабочих дней РФ.",
            "Точность модели зависит от полноты и непрерывности загружаемой банковской выписки."
        ]

        eval_rec = EvaluationRecord(
            company_id=company_id,
            data_revision=comp.data_revision,
            split_method="rolling_origin",
            test_days=test_days,
            folds=[f.model_dump() for f in folds],
            metrics=[m.model_dump() for m in metrics_list],
            selected_model=best_model,
            selection_reason=reason,
            limitations=limitations
        )
        db.add(eval_rec)
        await db.commit()
        await db.refresh(eval_rec)

        return EvaluationResponse(
            id=eval_rec.id,
            company_id=company_id,
            created_at=eval_rec.created_at.isoformat(),
            data_revision=comp.data_revision,
            split_method="rolling_origin",
            test_days=test_days,
            folds=folds,
            metrics=metrics_list,
            selected_model=best_model,
            selection_reason=reason,
            limitations=limitations
        )

    @staticmethod
    async def get_evaluation(
        db: AsyncSession,
        company_id: str,
        evaluation_id: str
    ) -> EvaluationResponse:
        res = await db.execute(
            select(EvaluationRecord).where(
                EvaluationRecord.company_id == company_id,
                EvaluationRecord.id == evaluation_id
            )
        )
        rec = res.scalar_one_or_none()
        if not rec:
            raise KeyError(f"Evaluation {evaluation_id} not found")

        return EvaluationResponse(
            id=rec.id,
            company_id=rec.company_id,
            created_at=rec.created_at.isoformat(),
            data_revision=rec.data_revision,
            split_method=rec.split_method,
            test_days=rec.test_days,
            folds=[EvaluationFold(**f) for f in rec.folds],
            metrics=[ModelMetrics(**m) for m in rec.metrics],
            selected_model=rec.selected_model,
            selection_reason=rec.selection_reason,
            limitations=rec.limitations or []
        )
