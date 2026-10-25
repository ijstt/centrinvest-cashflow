import {test} from 'node:test';
import assert from 'node:assert/strict';
import {inferMapping,parseStatementCsv,validateStatement} from '../v2/statement';
import {groupPlanWeeks} from '../v2/plan';
import type {PlanActualDay} from '../../../contracts/types';
import {readFileSync} from 'node:fs';

const header='operation_id;operation_date;amount;direction;counterparty_name;counterparty_inn;payment_purpose;balance_eod';
test('tab-delimited bank statements and quoted delimiter characters preserve column mapping',()=>{
 const tsv=header.replaceAll(';','\t')+'\nx;2026-10-01;100;income;Client;0012345678;Payment;1000'.replaceAll(';','\t');
 const parsed=parseStatementCsv(tsv);
 assert.equal(parsed.headers.length,8);
 assert.deepEqual(validateStatement(parsed,inferMapping(parsed.headers)),[]);
 const comma=parseStatementCsv('operation_date,amount,direction,counterparty_name,counterparty_inn,"purpose;with;semicolons;inside;header",balance_eod\n2026-10-01,100,income,Client,0012345678,Payment,1000');
 assert.equal(comma.headers.length,7);
 assert.equal(comma.rows[0][4],'0012345678');
});
test('benchmark C001 statement has 1131 valid mapped operations',()=>{
 const csv=readFileSync(new URL('../../../fixtures/statements/C001_logistics.csv',import.meta.url),'utf8');
 const preview=parseStatementCsv(csv);assert.equal(preview.rows.length,1131);assert.deepEqual(validateStatement(preview,inferMapping(preview.headers)),[]);
});
test('bank CSV maps standard headers and preserves quoted semicolons, decimal comma and multiline purpose',()=>{
 const parsed=parseStatementCsv('\uFEFF'+header+'\r\nx1;2026-09-01;100,50;income;"Клиент; А";token-1;"Оплата\nпо счету";1000,50\r\n');
 assert.equal(parsed.rows.length,1);assert.equal(parsed.rows[0][4],'Клиент; А');assert.equal(parsed.rows[0][6],'Оплата\nпо счету');
 const mapping=inferMapping(parsed.headers);assert.equal(mapping.purpose,'payment_purpose');assert.equal(mapping.external_id,'operation_id');assert.deepEqual(validateStatement(parsed,mapping),[]);
});
test('bank CSV rejects invalid dates, amounts, incomplete and duplicate mappings',()=>{
 for(const row of ['x;2026-02-30;100;income;A;anon;Purpose;1000','x;2026-02-28;-1;expense;A;anon;Purpose;1000','x;2026-02-28;NaN;income;A;anon;Purpose;1000']){const p=parseStatementCsv(header+'\n'+row);assert.ok(validateStatement(p,inferMapping(p.headers)).length);}
 const p=parseStatementCsv(header+'\nx;2026-02-28;100;income;A;anon;Purpose;1000');
 assert.ok(validateStatement(p,{...inferMapping(p.headers),date:''}).length);
 assert.ok(validateStatement(p,{...inferMapping(p.headers),purpose:'amount'}).some(e=>e.includes('нескольким')));
 assert.throws(()=>parseStatementCsv('date,date\n1,2'));assert.throws(()=>parseStatementCsv('a,b\n"unclosed,1'));
});
function day(date:string,actual:number|null):PlanActualDay{return{date,planned_income:100,actual_income:actual,planned_expense:50,actual_expense:actual===null?null:50,planned_balance:1000,actual_balance:actual===null?null:1050,net_flow_variance:actual===null?null:actual-100,overdue_event_ids:['a']};}
test('weekly plan sums flows, takes last balance and preserves missing observations',()=>{
 const [week]=groupPlanWeeks([day('2026-09-07',150),day('2026-09-08',200)]);
 assert.equal(week.date,'2026-09-07');assert.equal(week.planned_income,200);assert.equal(week.actual_income,350);assert.equal(week.actual_balance,1050);assert.equal(week.net_flow_variance,150);assert.deepEqual(week.overdue_event_ids,['a']);
 const [missing]=groupPlanWeeks([day('2026-09-07',150),day('2026-09-08',null)]);assert.equal(missing.actual_income,null);assert.equal(missing.net_flow_variance,null);assert.equal(missing.actual_balance,null);
});
test('1CClientBankExchange format parses sections, counterparty, and closing balance correctly',()=>{

 const oneCText=`1CClientBankExchange
ВерсияФормата=1.03
Кодировка=Windows
КонечныйОстаток=540000.00
СекцияДокумент=Платежное поручение
Номер=105
Дата=20.10.2026
Сумма=45000.00
Плательщик=ООО "Север"
ПлательщикИНН=7701234567
Получатель=ООО "Юг"
ПолучательИНН=6161234567
НазначениеПлатежа=Оплата за поставку оборудования без НДС
КонецДокумента
`;
 const preview=parseStatementCsv(oneCText);
 assert.equal(preview.rows.length,1);
 assert.equal(preview.rows[0][0],'2026-10-20');
 assert.equal(preview.rows[0][1],'45000.00');
 assert.equal(preview.rows[0][3],'ООО "Юг"');
 assert.equal(preview.rows[0][6],'540000.00');
 const mapping=inferMapping(preview.headers);
 assert.deepEqual(validateStatement(preview,mapping),[]);
});

