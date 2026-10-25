import { test } from 'node:test';
import assert from 'node:assert/strict';
import { clearWorkspaceSession, canEditQueryResult, createDebouncedRequest, createRequestGate, decodeStatementBytes, forecastControlValues, readForecastRequest, refreshForecastRequest, storeForecastRequest } from './requestSafety';
import type { CreateForecastRequest } from '../../../contracts/types';

test('auth boundary removes all financial workspace keys while preserving unrelated storage', () => {
  const values=new Map<string,string>([
    ['selected-company','one'],['snapshots:one','private'],['snapshots:two','private'],
    ['forecast-requests:one','private'],['forecast-requests:two','private'],['preference','keep']
  ]);
  clearWorkspaceSession({get length(){return values.size;},key:index=>[...values.keys()][index]??null,removeItem:key=>{values.delete(key);}});
  assert.deepEqual([...values],[['preference','keep']]);
  assert.doesNotThrow(()=>clearWorkspaceSession({get length():number{throw new Error('Storage denied');},key:()=>null,removeItem:()=>{}}));
});

test('snapshot provenance roundtrips automatic balance and disabled events, isolated per company', () => {
  const values = new Map<string,string>();
  const storage = { getItem: (key:string) => values.get(key) ?? null, setItem: (key:string,value:string) => { values.set(key,value); } };
  const request: CreateForecastRequest = { as_of_date:'2026-10-01',horizon_days:30,initial_balance:null,include_planned_events:false,model:'auto',expected_data_revision:1 };
  assert.equal(readForecastRequest(storage,'one','snapshot'),null);
  storeForecastRequest(storage,'one','snapshot',request);
  const recovered = readForecastRequest(storage,'one','snapshot');
  assert.deepEqual(recovered,request);
  assert.equal(readForecastRequest(storage,'two','snapshot'),null);
  assert.deepEqual(refreshForecastRequest(recovered!,2),{...request,expected_data_revision:2});
  assert.equal(refreshForecastRequest({...request,initial_balance:0},2).initial_balance,0);
  storage.setItem('forecast-requests:one',JSON.stringify({snapshot:{...request,include_planned_events:undefined}}));
  assert.equal(readForecastRequest(storage,'one','snapshot'),null);
});

test('logout invalidation rejects a session response even if its promise resolves after cancellation', async () => {
  const gate=createRequestGate();
  const pending=gate.begin();
  let release!: (value:string)=>void;
  const response=new Promise<string>(resolve=>{release=resolve;});
  let session:string|null=null;
  const handling=response.then(value=>{if(pending.isCurrent())session=value;});
  gate.invalidate();
  release('old authenticated session');await handling;
  assert.equal(session,null);
  assert.equal(pending.signal.aborted,true);
  const next=gate.begin();assert.equal(next.isCurrent(),true);
  gate.begin();assert.equal(next.isCurrent(),false);
});

test('manual calculation cancels earlier debounced options and edits cancel pending requests', t => {
  t.mock.timers.enable({apis:['setTimeout']});
  const sent:boolean[]=[];
  const requests=createDebouncedRequest<boolean>(value=>sent.push(value),280);
  requests.schedule(true);t.mock.timers.tick(100);
  requests.run(false);t.mock.timers.tick(300);
  assert.deepEqual(sent,[false]);
  requests.schedule(true);requests.cancel();t.mock.timers.tick(300);
  assert.deepEqual(sent,[false]);
  requests.schedule(true);requests.schedule(false);t.mock.timers.tick(280);
  assert.deepEqual(sent,[false,false]);
});

test('valid UTF8 with 1251 in payment data remains UTF8; invalid UTF8 falls back to Windows1251', () => {
  const text='Дата;Назначение\n2026-10-01;Оплата счета 1251';
  assert.equal(decodeStatementBytes(new TextEncoder().encode(text).buffer),text);
  assert.equal(decodeStatementBytes(Uint8Array.from([0xc4,0xe0,0xf2,0xe0]).buffer),'Дата');
});

test('restored controls preserve original auto model, disabled events and zero/manual balance', () => {
  const fallback={as_of_date:'2026-10-02',horizon_days:60 as const};
  const request:CreateForecastRequest={as_of_date:'2026-10-01',horizon_days:30,initial_balance:0,include_planned_events:false,model:'auto',expected_data_revision:1};
  assert.deepEqual(forecastControlValues(request,fallback),{cutoff:'2026-10-01',horizon:30,model:'auto',events:false,override:'0',known:true});
  assert.equal(forecastControlValues({...request,initial_balance:-1250.5},fallback).override,'-1250.5');
  assert.equal(forecastControlValues({...request,initial_balance:null},fallback).override,'');
  const unknown=forecastControlValues(null,fallback);
  assert.equal(unknown.known,false);
  assert.equal(unknown.override,'');
  assert.equal(unknown.model,'auto');
});

test('old rows cannot mutate on the render before resource loading effect starts', () => {
  const previous=JSON.stringify(['company',{limit:50},'',1,0]);
  assert.equal(canEditQueryResult(previous,previous,false,false),true);
  for(const next of [
    JSON.stringify(['company',{limit:50,search:'Vendor'},'',1,0]),
    JSON.stringify(['company',{limit:50},'next-cursor',1,0]),
    JSON.stringify(['company',{limit:50},'',2,0]),
    JSON.stringify(['company',{limit:50},'',1,1]),
    JSON.stringify(['other-company',{limit:50},'',1,0]),
  ]) assert.equal(canEditQueryResult(next,previous,false,false),false);
  assert.equal(canEditQueryResult(previous,previous,true,false),false);
  assert.equal(canEditQueryResult(previous,previous,false,true),false);
  assert.equal(canEditQueryResult(previous,undefined,false,false),false);
});
