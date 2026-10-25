import type { CreateForecastRequest } from '../../../contracts/types';

type StorageAccess = Pick<Storage, 'getItem' | 'setItem'>;
const requestKey = (companyId: string) => `forecast-requests:${companyId}`;

export function clearWorkspaceSession(storage: Pick<Storage, 'length' | 'key' | 'removeItem'>) {
  try {
    const keys = Array.from({length:storage.length}, (_, index) => storage.key(index));
    for (const key of keys) {
      if (key === 'selected-company' || key?.startsWith('snapshots:') || key?.startsWith('forecast-requests:')) {
        storage.removeItem(key);
      }
    }
  } catch { /* Blocked browser storage must not prevent signing out. */ }
}

export function readForecastRequest(storage: StorageAccess, companyId: string, snapshotId: string): CreateForecastRequest | null {
  try {
    const value = JSON.parse(storage.getItem(requestKey(companyId)) ?? '{}')[snapshotId];
    if (!value || typeof value.as_of_date !== 'string' || ![30,60,90].includes(value.horizon_days)
      || !(value.initial_balance === null || (typeof value.initial_balance === 'number' && Number.isFinite(value.initial_balance)))
      || typeof value.include_planned_events !== 'boolean' || !['auto','seasonal_naive','weekday_mean','ridge_calendar'].includes(value.model)
      || !Number.isInteger(value.expected_data_revision)) return null;
    return value;
  } catch { return null; }
}
export function storeForecastRequest(storage: StorageAccess, companyId: string, snapshotId: string, request: CreateForecastRequest) {
  try {
    const previous = JSON.parse(storage.getItem(requestKey(companyId)) ?? '{}');
    const entries = [[snapshotId, request], ...Object.entries(previous).filter(([id]) => id !== snapshotId)].slice(0,30);
    storage.setItem(requestKey(companyId), JSON.stringify(Object.fromEntries(entries)));
  } catch { /* Current in-memory request remains available if storage is disabled. */ }
}
export function refreshForecastRequest(request: CreateForecastRequest, revision: number): CreateForecastRequest {
  return { ...request, expected_data_revision: revision };
}
export function forecastControlValues(request: CreateForecastRequest | null, fallback: {as_of_date:string;horizon_days:30|60|90}) {
  return {
    cutoff:request?.as_of_date??fallback.as_of_date,
    horizon:request?.horizon_days??fallback.horizon_days,
    model:request?.model??'auto',
    events:request?.include_planned_events??true,
    override:request?.initial_balance===null||request?.initial_balance===undefined?'':String(request.initial_balance),
    known:request!==null,
  };
}
export function canEditQueryResult(expectedKey:string, receivedKey:string|undefined, loading:boolean, stale:boolean):boolean {
  return expectedKey===receivedKey&&!loading&&!stale;
}

/** An auth transition invalidates even responses whose transport could not be cancelled. */
export function createRequestGate() {
  let generation = 0;
  let controller: AbortController | null = null;
  const invalidate = () => { generation++; controller?.abort(); controller = null; };
  return {
    invalidate,
    begin() {
      invalidate();
      const current = generation;
      controller = new AbortController();
      const signal = controller.signal;
      return { signal, isCurrent: () => generation === current && !signal.aborted };
    },
  };
}

export function createDebouncedRequest<T>(dispatch: (request: T) => void, delay: number) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const cancel = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined; };
  return {
    cancel,
    schedule(request: T) { cancel(); timer = setTimeout(() => { timer = undefined; dispatch(request); }, delay); },
    run(request: T) { cancel(); dispatch(request); },
  };
}

export function decodeStatementBytes(buffer: ArrayBuffer): string {
  try { return new TextDecoder('utf-8', { fatal: true }).decode(buffer); }
  catch { return new TextDecoder('windows-1251').decode(buffer); }
}
