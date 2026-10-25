import type { ColumnMapping } from '../../../contracts/types';
import { validDate } from '../lib/cashflow';

export interface StatementPreview { headers: string[]; rows: string[][] }
export const mappingLabels: Record<keyof ColumnMapping, string> = { date: 'Дата операции', amount: 'Сумма', direction: 'Направление', counterparty_name: 'Контрагент', counterparty_inn: 'Обезличенный ИНН', purpose: 'Назначение платежа', closing_balance: 'Остаток на конец дня', external_id: 'ID операции (необязательно)' };
export function is1CStatement(text: string): boolean {
  return /1CClientBankExchange/i.test(text.slice(0, 300));
}

export function parse1CStatement(text: string): StatementPreview {
  const clean = text.replace(/^\uFEFF/, '');
  const lines = clean.split(/\r?\n/);
  const rows: string[][] = [];
  let inDoc = false;
  let doc: Record<string, string> = {};
  let closingBalance: number | null = null;
  let initialBalance: number | null = null;
  const headerAccounts = new Set<string>();
  const headerInns = new Set<string>();
  const readBalance = (line: string) => {
    const value = line.slice(line.indexOf('=') + 1).replace(/\s/g, '').replace(',', '.');
    if (!value || !Number.isFinite(Number(value))) throw new Error('В выписке 1C указан некорректный остаток.');
    return Number(value);
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith('КонечныйОстаток=')) {
      closingBalance = readBalance(trimmed);
    } else if (trimmed.startsWith('НачальныйОстаток=')) {
      initialBalance = readBalance(trimmed);
    } else if (trimmed.startsWith('РасчСчет=')) {
      const acc = trimmed.split('=', 2)[1].trim();
      if (acc) headerAccounts.add(acc);
    } else if (trimmed.startsWith('ИНН=')) {
      const inn = trimmed.split('=', 2)[1].trim();
      if (inn) headerInns.add(inn);
    }
  }

  for (const line of lines) {
    const trimmed = line.trim();
    const isDocStart = trimmed.startsWith('СекцияДокумент=') ||
      (trimmed.includes('=') && trimmed.split('=')[0].replace(/[^\uFFFD]/g, '').length === 14 && trimmed.indexOf('=') <= 15);
    const isDocEnd = trimmed === 'КонецДокумента' ||
      (!trimmed.includes('=') && (trimmed.includes('КонецДокумента') || (trimmed.replace(/[^\uFFFD]/g, '').length === 14 && trimmed.length === 14)));

    if (isDocStart) {
      if (inDoc) throw new Error('В файле не завершен платежный документ.');
      inDoc = true;
      doc = { 'СекцияДокумент': trimmed.split('=', 2)[1]?.trim() ?? '' };
    } else if (isDocEnd) {
      if (inDoc) {
        let rawDate = doc['Дата'];
        let rawSum = doc['Сумма'];

        for (const [k, v] of Object.entries(doc)) {
          if (!rawDate && /^\d{2}\.\d{2}\.\d{4}$/.test(v)) rawDate = v;
          if (!rawSum && /^\d+([.,]\d+)?$/.test(v) && k.replace(/[^\uFFFD]/g, '').length === 5) rawSum = v;
        }

        if (!rawDate || !rawSum) throw new Error('В платежном документе отсутствует дата или сумма.');
        if (rawDate && rawSum) {
          const normalizedDate = /^\d{2}\.\d{2}\.\d{4}$/.test(rawDate)
            ? rawDate.split('.').reverse().join('-')
            : rawDate;
          const amount = rawSum.replace(/\s/g, '').replace(',', '.');
          const payer = doc['Плательщик'] || doc['Плательщик1'] || '';
          const payerInn = doc['ПлательщикИНН'] || '';
          const payerAcc = doc['ПлательщикСчет'] || doc['ПлательщикРасчСчет'] || '';
          const receiver = doc['Получатель'] || doc['Получатель1'] || '';
          const receiverInn = doc['ПолучательИНН'] || '';
          const receiverAcc = doc['ПолучательСчет'] || doc['ПолучательРасчСчет'] || '';
          const purpose = doc['НазначениеПлатежа'] || 'Банковский перевод';
          const extId = doc['Номер'] || '';

          let isIncome = false;
          if (headerAccounts.size > 0) {
            if (receiverAcc && headerAccounts.has(receiverAcc) && !headerAccounts.has(payerAcc)) {
              isIncome = true;
            } else if (payerAcc && headerAccounts.has(payerAcc) && !headerAccounts.has(receiverAcc)) {
              isIncome = false;
            } else if (headerInns.size > 0) {
              if (receiverInn && headerInns.has(receiverInn) && !headerInns.has(payerInn)) {
                isIncome = true;
              } else if (payerInn && headerInns.has(payerInn)) {
                isIncome = false;
              }
            }
          } else if (headerInns.size > 0) {
            if (receiverInn && headerInns.has(receiverInn) && !headerInns.has(payerInn)) {
              isIncome = true;
            } else if (payerInn && headerInns.has(payerInn)) {
              isIncome = false;
            }
          } else {
            const sec = doc['СекцияДокумент'] || '';
            if (sec.toLowerCase().includes('поступлени') || sec.toLowerCase().includes('приход')) {
              isIncome = true;
            }
          }

          const direction = isIncome ? 'Поступление' : 'Списание';
          const counterparty = isIncome
            ? (payer || receiver || 'Контрагент')
            : (receiver || payer || 'Контрагент');
          const inn = isIncome
            ? (payerInn || receiverInn || '0000000000')
            : (receiverInn || payerInn || '0000000000');

          rows.push([
            normalizedDate,
            amount,
            direction,
            counterparty,
            inn,
            purpose,
            '',
            extId
          ]);
        }
      }
      inDoc = false;
    } else if (inDoc && trimmed.includes('=')) {
      const idx = trimmed.indexOf('=');
      doc[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
    }
  }

  const headers = ['Дата', 'Сумма', 'Направление', 'Контрагент', 'ИНН', 'Назначение', 'Остаток', 'Номер'];
  if (inDoc) throw new Error('В файле не завершен платежный документ.');
  if (rows.length === 0) {
    throw new Error('В файле 1CClientBankExchange не найдено платежных документов.');
  }
  if (initialBalance === null && closingBalance === null) throw new Error('В выписке 1C отсутствует начальный или конечный остаток.');
  if ([initialBalance, closingBalance].some(value => value !== null && !Number.isFinite(value))) throw new Error('В выписке 1C указан некорректный остаток.');
  for (const row of rows) {
    if (!validDate(row[0]) || !Number.isFinite(Number(row[1])) || Number(row[1]) <= 0) throw new Error('Проверьте дату и положительную сумму в платежном документе.');
  }
  const net = (row: string[]) => (row[2] === 'Поступление' ? 1 : -1) * Math.round(Number(row[1]) * 100);
  let running = initialBalance === null ? Math.round(closingBalance! * 100) - rows.reduce((sum, row) => sum + net(row), 0) : Math.round(initialBalance * 100);
  const byDate = new Map<string, string[][]>();
  for (const row of rows) {
    const daily = byDate.get(row[0]) ?? [];
    daily.push(row);
    byDate.set(row[0], daily);
  }
  for (const date of [...byDate.keys()].sort()) {
    const daily = byDate.get(date)!;
    running += daily.reduce((sum, row) => sum + net(row), 0);
    for (const row of daily) row[6] = (running / 100).toFixed(2);
  }
  return { headers, rows };
}

export function parseStatementText(text: string): StatementPreview {
  if (is1CStatement(text)) {
    return parse1CStatement(text);
  }
  return parseStatementCsv(text);
}

export function parseStatementCsv(text: string): StatementPreview {
  if (is1CStatement(text)) {
    return parse1CStatement(text);
  }
  text = text.replace(/^\uFEFF/, '');
  const counts = new Map([[',',0],[';',0],['\t',0]]);
  let inQuotes=false;
  for(let i=0;i<text.length;i++) {
    const char=text[i];
    if(char==='"') {
      if(inQuotes&&text[i+1]==='"')i++;
      else inQuotes=!inQuotes;
    } else if(!inQuotes) {
      if(char==='\n'||char==='\r')break;
      if(counts.has(char))counts.set(char,counts.get(char)!+1);
    }
  }
  const delimiter=[...counts].reduce((best,candidate)=>candidate[1]>best[1]?candidate:best)[0];
  const rows: string[][] = []; let row: string[] = [], cell = '', quoted = false;
  for (let i=0; i<text.length; i++) {
    const char=text[i];
    if (char === '"') { if (quoted && text[i+1] === '"') { cell+='"'; i++; } else quoted=!quoted; }
    else if (char === delimiter && !quoted) { row.push(cell.trim()); cell=''; }
    else if ((char === '\n' || char === '\r') && !quoted) { if (char==='\r' && text[i+1]==='\n') i++; row.push(cell.trim()); if(row.some(Boolean)) rows.push(row); row=[]; cell=''; }
    else cell+=char;
  }
  if (quoted) throw new Error('В CSV не закрыта кавычка. Проверьте файл.');
  row.push(cell.trim()); if (row.some(Boolean)) rows.push(row);
  return previewFromRows(rows);
}

export function previewFromRows(rows: string[][]): StatementPreview {
  if (rows.length < 2) throw new Error('В файле нет операций. Нужны заголовок и строки данных.');
  if (rows.length > 100001) throw new Error('Не более 100 000 операций в одном файле.');
  const headers=rows[0].map(h=>h.trim());
  if (headers.some(h=>!h) || new Set(headers).size!==headers.length) throw new Error('Имена столбцов должны быть непустыми и уникальными.');
  return { headers, rows: rows.slice(1) };
}
export function inferMapping(headers: string[]): ColumnMapping {
  const names: Record<keyof ColumnMapping, string[]> = {
    date: ['operation_date','date','дата','дата операции'], amount: ['amount','сумма'], direction: ['direction','направление'],
    counterparty_name: ['counterparty_name','контрагент','наименование контрагента'], counterparty_inn: ['counterparty_inn','инн','инн контрагента'],
    purpose: ['payment_purpose','purpose','назначение платежа','назначение'], closing_balance: ['balance_eod','closing_balance','остаток','остаток на конец дня'], external_id: ['operation_id','external_id','id','номер'],
  };

  return Object.fromEntries(Object.entries(names).map(([key, aliases]) => [key, headers.find(h=>aliases.includes(h.toLowerCase())) ?? ''])) as unknown as ColumnMapping;
}
export function validateStatement(preview: StatementPreview, mapping: ColumnMapping): string[] {
  const errors: string[]=[];
  const required=(Object.keys(mappingLabels) as (keyof ColumnMapping)[]).filter(k=>k!=='external_id');
  for (const key of required) if (!mapping[key] || !preview.headers.includes(mapping[key]!)) errors.push(`Выберите столбец «${mappingLabels[key]}».`);
  const chosen=Object.values(mapping).filter(Boolean);
  if(new Set(chosen).size!==chosen.length) errors.push('Один столбец нельзя назначить нескольким полям.');
  if(errors.length) return errors;
  const column = (row: string[], key: keyof ColumnMapping) => row[preview.headers.indexOf(mapping[key]!)] ?? '';
  for(let i=0;i<preview.rows.length;i++) {
    const row=preview.rows[i];
    while(row.length < preview.headers.length) row.push('');
    const date=column(row,'date');
    const normalized=/^\d{2}\.\d{2}\.\d{4}$/.test(date) ? date.split('.').reverse().join('-') : date;
    const amount=Number(column(row,'amount').replace(/\s/g,'').replace(',','.'));
    const balance=Number(column(row,'closing_balance').replace(/\s/g,'').replace(',','.'));
    const direction=column(row,'direction').toLowerCase();
    if(!validDate(normalized) || !column(row,'amount') || !Number.isFinite(amount) || amount<=0 || !column(row,'closing_balance') || !Number.isFinite(balance) || !['income','expense','поступление','списание','приход','расход','+','-','credit','debit'].includes(direction)) errors.push(`Строка ${i+2}: проверьте дату (ГГГГ-ММ-ДД или ДД.ММ.ГГГГ), направление, сумму и остаток.`);
    if(errors.length>=20) { errors.push('Показаны первые 20 ошибок. Исправьте файл и загрузите заново.'); break; }
  }
  return errors;
}
