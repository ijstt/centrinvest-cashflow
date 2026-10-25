"""Compose identifiable cash-flow components without inventing recurring offsets."""
from datetime import date, timedelta
from collections import defaultdict
import numpy as np
from src.services.recurring_service import add_months


def compose_flows(dates, baseline_income, baseline_expense, events, patterns, transactions):
    income = np.array(baseline_income, dtype=float).copy()
    expense = np.array(baseline_expense, dtype=float).copy()
    ids = [[] for _ in dates]
    by_weekday = defaultdict(lambda: defaultdict(float))
    overall = defaultdict(float)
    for tx in transactions:
        if tx.direction == 'expense':
            by_weekday[tx.date.weekday()][tx.category_id] += float(tx.amount)
            overall[tx.category_id] += float(tx.amount)
    categories = set(overall) | {e.category_id for e in events} | {p.category_id for p in patterns} | {'uncategorized'}
    components = {category: np.zeros(len(dates)) for category in categories}
    for i, day in enumerate(dates):
        amounts = by_weekday.get(day.weekday()) or overall
        total = sum(amounts.values())
        if total:
            for category, amount in amounts.items():
                components[category][i] = expense[i] * amount / total
        else:
            components['uncategorized'][i] = expense[i]
    index = {day:i for i,day in enumerate(dates)}
    remaining_income = np.array(baseline_income, dtype=float).copy()
    remaining_expense = {category:values.copy() for category,values in components.items()}
    pats = {p.id:p for p in patterns}
    replaced = set()

    def apply(day, amount, direction, category, replacement=0):
        if day not in index:
            return
        i = index[day]
        if direction == 'income':
            reduction = min(remaining_income[i], replacement)
            remaining_income[i] -= reduction
            income[i] += amount - reduction
        else:
            # Only an identified component can be replaced. Unrelated costs survive.
            component = components.setdefault(category, np.zeros(len(dates)))
            available = remaining_expense.setdefault(category, np.zeros(len(dates)))
            reduction = min(available[i], replacement)
            available[i] -= reduction
            component[i] += amount - reduction
            expense[i] += amount - reduction

    for event in events:
        if event.due_date not in index:
            continue
        pattern = pats.get(event.recurrence_id)
        if pattern:
            replaced.add((pattern.id, event.due_date))
        if event.status in ('cancelled', 'paid'):
            continue
        outstanding = max(0, float(event.amount) - float(event.paid_amount))
        apply(event.due_date, outstanding, event.direction, event.category_id,
              float(pattern.expected_amount) if pattern else 0)
        ids[index[event.due_date]].append(event.id)
    for pattern in patterns:
        target = pattern.next_date
        if isinstance(target, str):
            target = date.fromisoformat(target)
        anchor = 28 if pattern.category_id == 'taxes' else target.day
        while target <= dates[-1]:
            if target >= dates[0] and (pattern.id,target) not in replaced:
                apply(target, float(pattern.expected_amount), pattern.direction, pattern.category_id,
                      float(pattern.expected_amount))
                ids[index[target]].append(pattern.id)
            if pattern.cadence == 'weekly':
                target += timedelta(days=7)
            elif pattern.cadence == 'monthly' and any(label in pattern.counterparty_name.casefold()
                    for label in ('полумесячн','semi-monthly')):
                target += timedelta(days=15)
            else:
                target = add_months(target, 1 if pattern.cadence == 'monthly' else 3, target_day=anchor)
                if pattern.category_id == 'taxes':
                    from src.services.event_service import adjust_tax_due_date_art_6_1
                    target = adjust_tax_due_date_art_6_1(target)
    # Keep component totals exactly equal to the exported two-decimal cash flows.
    income = np.round(income,2)
    expense = np.round(expense,2)
    for values in components.values():
        values[:] = np.round(values,2)
    components['uncategorized'] += expense - sum(components.values())
    return income, expense, ids, {key:values.tolist() for key,values in components.items()}
