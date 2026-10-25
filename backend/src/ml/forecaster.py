import numpy as np
import pandas as pd
from pydantic import BaseModel

class TransactionPoint(BaseModel):
    date: str
    income: float
    expense: float

class ForecastResult(BaseModel):
    predicted_dates: list[str]
    predicted_income: list[float]
    predicted_expense: list[float]
    predicted_balance: list[float]
    cashflow_gap_detected: bool
    gap_dates: list[str]
    minimum_expected_balance: float
    risk_level: str  # low, medium, critical

class FinancialForecaster:
    @staticmethod
    def forecast_cashflow(
        history: list[TransactionPoint],
        initial_balance: float = 100000.0,
        horizon_days: int = 14
    ) -> ForecastResult:
        if not history:
            raise ValueError('insufficient_data: Financial history is required')

        df = pd.DataFrame([p.model_dump() for p in history])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")

        # Trend estimation using simple linear regression / moving averages
        n = len(df)
        x = np.arange(n)
        
        # Fit trend or average
        if n >= 3:
            slope_inc, intercept_inc = np.polyfit(x, df["income"], 1)
            slope_exp, intercept_exp = np.polyfit(x, df["expense"], 1)
        else:
            slope_inc, intercept_inc = 0.0, df["income"].mean()
            slope_exp, intercept_exp = 0.0, df["expense"].mean()

        last_date = df["date"].iloc[-1]
        future_dates = [last_date + pd.Timedelta(days=i) for i in range(1, horizon_days + 1)]
        future_x = np.arange(n, n + horizon_days)

        pred_inc = [max(0.0, float(slope_inc * xi + intercept_inc)) for xi in future_x]
        pred_exp = [max(0.0, float(slope_exp * xi + intercept_exp)) for xi in future_x]

        rounded_inc = [round(v, 2) for v in pred_inc]
        rounded_exp = [round(v, 2) for v in pred_exp]

        current_bal = round(initial_balance, 2)
        pred_bal = []
        gap_dates = []

        for d, r_inc, r_exp in zip(future_dates, rounded_inc, rounded_exp):
            current_bal = round(current_bal + r_inc - r_exp, 2)
            pred_bal.append(current_bal)
            if current_bal < 0:
                gap_dates.append(d.strftime("%Y-%m-%d"))

        min_bal = min(pred_bal) if pred_bal else current_bal
        risk = "low"
        if min_bal < 0:
            risk = "critical"
        elif min_bal < (initial_balance * 0.2):
            risk = "medium"

        return ForecastResult(
            predicted_dates=[d.strftime("%Y-%m-%d") for d in future_dates],
            predicted_income=rounded_inc,
            predicted_expense=rounded_exp,
            predicted_balance=pred_bal,
            cashflow_gap_detected=len(gap_dates) > 0,
            gap_dates=gap_dates,
            minimum_expected_balance=round(min_bal, 2),
            risk_level=risk
        )

forecaster = FinancialForecaster()
