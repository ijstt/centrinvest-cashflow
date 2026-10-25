"""Statistical baseline and past-only calibration shared by runtime and control tooling."""
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge


def calendar_features(dates):
    dates=pd.Series(pd.to_datetime(dates)).reset_index(drop=True)
    weekday=np.eye(7)[dates.dt.dayofweek.to_numpy()]
    return np.column_stack((weekday,
        np.sin(2*np.pi*dates.dt.day/31),np.cos(2*np.pi*dates.dt.day/31),
        np.sin(2*np.pi*dates.dt.month/12),np.cos(2*np.pi*dates.dt.month/12)))


def predict_baseline(history, future_dates, model):
    future=pd.Series(pd.to_datetime(future_dates))
    predictions=[]
    for column in ('income','expense'):
        if model=='ridge_calendar':
            regression=Ridge(alpha=10.0).fit(calendar_features(history['date']),history[column].to_numpy())
            values=regression.predict(calendar_features(future))
        elif model=='seasonal_naive':
            recent=history.tail(7)
            last_by_weekday=dict(zip(recent['date'].dt.dayofweek,recent[column]))
            values=[last_by_weekday.get(day,history[column].mean()) for day in future.dt.dayofweek]
        elif model=='weekday_mean':
            means=history.groupby(history['date'].dt.dayofweek)[column].mean().to_dict()
            values=[means.get(day,history[column].mean()) for day in future.dt.dayofweek]
        else:
            raise ValueError(f'Unknown model: {model}')
        predictions.append(np.maximum(0,np.asarray(values,dtype=float)))
    return tuple(predictions)


def calibration_window(history, model):
    count=min(14,max(5,len(history)//4))
    train,validation=history.iloc[:-count],history.iloc[-count:]
    income,expense=predict_baseline(train,validation['date'],model)
    balance=float(train['closing_balance'].iloc[-1])+np.cumsum(income-expense)
    scale=max(50.0,float(np.sqrt(np.var(train['income'])+np.var(train['expense']))))
    spread=1.28*scale*np.sqrt(np.arange(1,count+1))
    return {'actual':validation['closing_balance'].tolist(),
            'lower':(balance-spread).tolist(),'upper':(balance+spread).tolist()}
