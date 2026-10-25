import re
import numpy as np
from datetime import datetime, date, timedelta
from calendar import monthrange
from sqlalchemy import select, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.entities import TransactionRecord, RecurringPatternRecord
from src.schemas.contracts import RecurringPattern, Direction, CategoryId

def add_months(sourcedate: date, months: int, target_day: int | None = None) -> date:
    month = sourcedate.month - 1 + months
    year = sourcedate.year + month // 12
    month = month % 12 + 1
    max_target = monthrange(year, month)[1]
    if target_day is not None:
        day = min(target_day, max_target)
    else:
        # If sourcedate was the end of its month, preserve end-of-month cadence
        if sourcedate.day == monthrange(sourcedate.year, sourcedate.month)[1]:
            day = max_target
        else:
            day = min(sourcedate.day, max_target)
    return date(year, month, day)

class RecurringService:
    @staticmethod
    async def detect_recurring(
        db: AsyncSession,
        company_id: str,
        as_of_date: date | None = None,
        persist: bool = True
    ) -> list[RecurringPattern]:
        # Query all transactions up to as_of_date (excluding sandbox)
        conditions = [
            TransactionRecord.company_id == company_id,
            (TransactionRecord.source_kind != "sandbox") | (TransactionRecord.source_kind.is_(None))
        ]
        if as_of_date:
            conditions.append(TransactionRecord.date <= as_of_date)

        res = await db.execute(
            select(TransactionRecord)
            .where(and_(*conditions))
            .order_by(TransactionRecord.date)
        )
        transactions = res.scalars().all()
        if not transactions:
            return []

        # Group by (direction, category_id, counterparty_name)
        groups: dict[tuple, list[TransactionRecord]] = {}
        for tx in transactions:
            c_name = tx.counterparty_name.strip()
            # If payroll, separate advance vs final settlement so both monthly cycles are recognized
            if tx.category_id == "payroll":
                is_advance = bool(re.search(r"(аванс|заработная\s+плата\s+за\s+первую\s+половину|зарплата\s+за\s+первую\s+половину|з/п\s+за\s+первую\s+половину|(?:перв\w*|1-?[еяюой]?)\s+половин)", tx.purpose, re.IGNORECASE))
                sub_label = "Сотрудники (Аванс по заработной плате)" if is_advance else "Сотрудники (Окончательный расчет по ЗП)"
                key = (tx.direction, "payroll", sub_label)
            elif tx.category_id == "taxes":
                purp_lower = tx.purpose.lower()
                if "ндфл" in purp_lower:
                    part = "до 5-го числа" if tx.date.day <= 10 else "до 28-го числа"
                    tax_type = f"НДФЛ ({part})"
                elif "страхов" in purp_lower:
                    tax_type = "Страховые взносы"
                elif "усн" in purp_lower:
                    tax_type = "УСН"
                elif "ндс" in purp_lower:
                    tax_type = "НДС"
                else:
                    tax_type = "ЕНП"
                key = (tx.direction, "taxes", f"ФНС России ({tax_type})")
            elif tx.category_id == "fees" and "банк" in c_name.lower():
                key = (tx.direction, "fees", "ПАО «Банк» (РКО и обслуживание)")
            else:
                key = (tx.direction, tx.category_id, c_name)
            groups.setdefault(key, []).append(tx)

        patterns: list[RecurringPattern] = []

        for (direction, cat_id, c_name), txs in groups.items():
            # Aggregate transactions by unique date to prevent periodicity collapse
            # (e.g. multiple salary payments to different employees on the exact same date)
            daily_amounts: dict[date, float] = {}
            for t in txs:
                daily_amounts[t.date] = daily_amounts.get(t.date, 0.0) + float(t.amount)

            if len(daily_amounts) < 3:
                continue

            unique_dates = sorted(daily_amounts.keys())
            amounts = [daily_amounts[d] for d in unique_dates]
            intervals = [(unique_dates[i + 1] - unique_dates[i]).days for i in range(len(unique_dates) - 1)]
            if not intervals:
                continue

            median_interval = float(np.median(intervals))
            median_amount = round(float(np.median(amounts)), 2)

            # Robust outlier-resistant coefficient of variation using MAD
            mad = float(np.median(np.abs(np.array(amounts) - median_amount)))
            robust_std = 1.4826 * mad
            cv_amount = float(robust_std / (median_amount + 1e-6))

            cadence = None
            is_semi_monthly = False
            if 5 <= median_interval <= 9:
                cadence = "weekly"
            elif 12 <= median_interval <= 18:
                cadence = "monthly"
                is_semi_monthly = True
                if "полумесячный" not in c_name.lower():
                    c_name = f"{c_name} (полумесячный график)"
            elif 24 <= median_interval <= 35:
                cadence = "monthly"
            elif 80 <= median_interval <= 100:
                cadence = "quarterly"

            if not cadence:
                continue

            # Check regularity of intervals
            std_interval = float(np.std(intervals))
            regularity = max(0.0, 1.0 - (std_interval / (median_interval + 1e-6)))
            amount_stability = max(0.0, 1.0 - cv_amount)
            confidence = round(float(np.clip(0.5 * regularity + 0.5 * amount_stability, 0.6, 0.98)), 2)

            # Compute next date strictly after as_of_date or last date
            ref_date = as_of_date or unique_dates[-1]
            last_date = unique_dates[-1]

            if cadence == "weekly":
                next_d = last_date + timedelta(days=7)
                while next_d <= ref_date:
                    next_d += timedelta(days=7)
            elif cadence == "monthly":
                if is_semi_monthly:
                    step_days = int(round(median_interval))
                    next_d = last_date + timedelta(days=step_days)
                    while next_d <= ref_date:
                        next_d += timedelta(days=step_days)
                else:
                    # Target calendar day of month based on historical median day
                    target_dom = int(np.median([d.day for d in unique_dates]))
                    y = ref_date.year
                    m = ref_date.month
                    max_d = monthrange(y, m)[1]
                    cand_d = date(y, m, min(target_dom, max_d))
                    if cand_d <= ref_date:
                        cand_d = add_months(cand_d, 1)
                        max_d_next = monthrange(cand_d.year, cand_d.month)[1]
                        cand_d = date(cand_d.year, cand_d.month, min(target_dom, max_d_next))
                    next_d = cand_d
            else:  # quarterly
                next_d = add_months(last_date, 3)
                while next_d <= ref_date:
                    next_d = add_months(next_d, 3)

            txs_sorted = sorted(txs, key=lambda x: x.date)
            pattern = RecurringPattern(
                id=f"rec_{cat_id}_{company_id[:8]}_{len(patterns) + 1}",
                direction=direction,
                category_id=cat_id,
                counterparty_name=c_name,
                cadence=cadence,
                expected_amount=median_amount,
                next_date=next_d.strftime("%Y-%m-%d"),
                confidence=confidence,
                sample_count=len(txs),
                transaction_ids=[t.id for t in txs_sorted]
            )
            patterns.append(pattern)

        if not persist:
            return patterns
        # Clear old and save new patterns to db
        await db.execute(delete(RecurringPatternRecord).where(RecurringPatternRecord.company_id == company_id))
        for p in patterns:
            db_pat = RecurringPatternRecord(
                id=p.id,
                company_id=company_id,
                direction=p.direction,
                category_id=p.category_id,
                counterparty_name=p.counterparty_name,
                cadence=p.cadence,
                expected_amount=p.expected_amount,
                next_date=datetime.strptime(p.next_date, "%Y-%m-%d").date(),
                confidence=p.confidence,
                sample_count=p.sample_count,
                transaction_ids=p.transaction_ids
            )
            db.add(db_pat)
        await db.commit()

        return patterns
