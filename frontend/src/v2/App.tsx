import { MobileSectionMenu } from './MobileSectionMenu';
import { UserMenu } from './UserMenu';
import { Select } from './Select';
import { lazy, startTransition, Suspense, useEffect, useRef, useState } from 'react';
import { CalendarDays, ChartNoAxesCombined, FileSpreadsheet, FlaskConical, GitCompareArrows, ShieldCheck, SlidersHorizontal } from 'lucide-react';
import type { CapabilitiesResponse, Company, CreateForecastRequest, EventListResponse, ForecastSnapshot, PlannedEvent, SessionResponse } from '../../../contracts/types';
import { Brand } from './Brand';
import Login from './Session';
import { api, ApiFailure, messageOf } from './client';
import { Empty, ErrorBox, Loading, readSnapshots, rememberSnapshot, useResource, validateSnapshot } from './shared';
import type { SnapshotRef } from './shared';
import { ErrorBoundary } from './ErrorBoundary';
import { SourceStatus } from './SourceStatus';
import { RefreshButton } from './RefreshButton';
import { clearWorkspaceSession, createRequestGate, readForecastRequest, refreshForecastRequest, storeForecastRequest } from '../lib/requestSafety';
import './styles.css';

const Legacy=lazy(()=>import('../App'));
const Overview=lazy(()=>import('./Overview')), Operations=lazy(()=>import('./Operations')), Calendar=lazy(()=>import('./Calendar')), Scenarios=lazy(()=>import('./Scenarios')), PlanActual=lazy(()=>import('./PlanActual')), Quality=lazy(()=>import('./Quality'));
const sections=[{id:'overview',name:'Обзор финансов',icon:ChartNoAxesCombined,feature:'forecast_v2'},{id:'operations',name:'Выписки и операции',icon:FileSpreadsheet,feature:'imports'},{id:'calendar',name:'Платежный календарь',icon:CalendarDays,feature:'events'},{id:'scenarios',name:'Стресс-сценарии',icon:SlidersHorizontal,feature:'scenarios'},{id:'plan',name:'План и факт',icon:GitCompareArrows,feature:'plan_actual'},{id:'quality',name:'Качество прогноза',icon:FlaskConical,feature:'evaluation'}] as const;
type Section=typeof sections[number]['id'];
function storedCompany() {try{return sessionStorage.getItem('selected-company')??'';}catch{return '';}}
function forgetWorkspace() {
  try { clearWorkspaceSession(sessionStorage); } catch { /* Storage access may be disabled. */ }
  const url=new URL(window.location.href);
  url.searchParams.delete('company');url.searchParams.delete('section');
  window.history.replaceState(window.history.state,'',url);
}

export default function App() {
  const bootstrap=useResource(async signal=>{
    const capabilities=await api('capabilities',{signal}).catch(e=>{if(e instanceof ApiFailure&&e.status===404)return null;throw e;});
    if(!capabilities)return {capabilities:null,session:null};
    const session=await api('session',{signal}).catch(e=>{if(e instanceof ApiFailure&&e.status===401)return null;throw e;});
    return {capabilities,session};
  },[]);
  const [session,setSession]=useState<SessionResponse|null>(null), [reason,setReason]=useState(''), [logoutError,setLogoutError]=useState(''), [loggingOut,setLoggingOut]=useState(false);
  const sessionGate=useRef(createRequestGate()), logoutInFlight=useRef(false);
  useEffect(()=>{if(bootstrap.data){if(!bootstrap.data.session)forgetWorkspace();setSession(bootstrap.data.session);}},[bootstrap.data]);
  useEffect(()=>{
    const expired=()=>{sessionGate.current.invalidate();forgetWorkspace();setSession(null);setReason('Сессия завершена. Войдите, чтобы продолжить.');};
    const focus=()=>{if(!session||logoutInFlight.current)return;const request=sessionGate.current.begin();void api('session',{signal:request.signal}).then(value=>{if(request.isCurrent())setSession(value);}).catch(e=>{if(request.isCurrent()&&e instanceof ApiFailure&&e.status===401)expired();});};
    window.addEventListener('session-expired',expired);window.addEventListener('focus',focus);return()=>{window.removeEventListener('session-expired',expired);window.removeEventListener('focus',focus);sessionGate.current.invalidate();};
  },[session]);
  if(bootstrap.loading)return <div className="boot-state"><Loading text="Восстанавливаем рабочую сессию…"/></div>;
  if(bootstrap.error)return <div className="boot-state"><h1>Не удалось подключиться</h1><ErrorBox message={bootstrap.error} retry={bootstrap.reload}/></div>;
  if(bootstrap.data?.capabilities===null)return <Suspense fallback={<Loading/>}><Legacy/></Suspense>;
  if(!session)return <Login reason={reason} onLogin={s=>{sessionGate.current.invalidate();setSession(s);setReason('');}}/>;
  return <><Workspace key={session.user.id} session={session} capabilities={bootstrap.data!.capabilities!} logoutPending={loggingOut} onLogout={async()=>{logoutInFlight.current=true;sessionGate.current.invalidate();setLoggingOut(true);setLogoutError('');try{await api('logout');forgetWorkspace();setSession(null);setReason('Вы вышли из рабочего пространства.');}catch(e){setLogoutError(messageOf(e));}finally{logoutInFlight.current=false;setLoggingOut(false);}}}/>{logoutError&&<div className="floating-error"><ErrorBox message={logoutError}/></div>}</>;
}

function Workspace({session,capabilities,onLogout,logoutPending}:{session:SessionResponse;capabilities:CapabilitiesResponse;onLogout:()=>Promise<void>;logoutPending:boolean}) {
  const companies=useResource(signal=>api('listCompanies',{signal}).then(r=>r.items.filter(c=>session.user.company_ids.includes(c.id))),[session.user.id]);
  function readNavigation(){
    const params=new URL(window.location.href).searchParams;
    const requested=sections.find(item=>item.id===params.get('section'));
    return {companyId:params.get('company')??storedCompany(),section:requested&&capabilities.features.includes(requested.feature)?requested.id:'overview' as Section};
  }
  const [navigation,setNavigation]=useState(readNavigation);
  const {companyId,section}=navigation;
  function navigate(next:{companyId:string;section:Section},replace=false){
    const url=new URL(window.location.href);
    url.searchParams.set('section',next.section);
    if(next.companyId)url.searchParams.set('company',next.companyId);else url.searchParams.delete('company');
    url.hash='';
    if(url.href!==window.location.href)window.history[replace?'replaceState':'pushState'](window.history.state,'',url);
    startTransition(()=>setNavigation(next));
  }
  function setSection(next:Section){navigate({companyId,section:next});}
  useEffect(()=>{
    const restore=()=>startTransition(()=>setNavigation(readNavigation()));
    window.addEventListener('popstate',restore);
    return()=>window.removeEventListener('popstate',restore);
  },[capabilities]);
  useEffect(()=>{
    if(!companies.data)return;
    const validId=companies.data.some(c=>c.id===companyId)?companyId:companies.data[0]?.id??'';
    navigate({companyId:validId,section},true);
  },[companies.data,companyId,section]);
  function changeCompany(id:string) {navigate({companyId:id,section:'overview'});try{sessionStorage.setItem('selected-company',id);}catch{/* Session selection remains usable without storage. */}}
  const company=companies.data?.find(c=>c.id===companyId);
  const [name,setName]=useState(''), [creating,setCreating]=useState(false), [createError,setCreateError]=useState('');
  return <div className="workspace v2-workspace unified-workspace"><a className="skip-link" href="#v2-main">Перейти к содержимому</a><header className="workspace-header"><a className="brand bank-brand header-brand" href="#overview" onClick={e=>{e.preventDefault();setSection('overview');}}><Brand/></a><div className="header-controls"><div className="header-company-group"><label className="header-company"><span>Компания</span><Select value={companyId} onChange={e=>changeCompany(e.target.value)} aria-label="Выберите компанию">{companies.data?.map(c=><option value={c.id} key={c.id}>{c.name}</option>)}{!companies.data?.length&&<option value="">Нет доступных компаний</option>}</Select></label><MobileSectionMenu items={sections.map(item=>({...item,disabled:!capabilities.features.includes(item.feature)}))} value={section} onChange={id=>setSection(id as Section)}/></div><UserMenu name={session.user.display_name} pending={logoutPending} onLogout={onLogout}/></div></header><aside className="sidebar"><nav className="desktop-section-nav" aria-label="Разделы">{sections.map(({id,name,icon:Icon,feature})=><button key={id} aria-current={section===id?'page':undefined} disabled={!capabilities.features.includes(feature)} title={!capabilities.features.includes(feature)?'Функция не поддерживается подключенным сервером':undefined} onClick={()=>setSection(id)}><Icon size={19}/><span>{name}</span></button>)}</nav><div className="sidebar-bottom"><div className="privacy-note"><ShieldCheck size={20}/><p>Видны компании, к которым у вас есть доступ. После выхода доступ к данным закрывается.</p></div></div></aside><main id="v2-main" className="main"><div className="page-heading workspace-page-heading"><div><h1>{sections.find(s=>s.id===section)!.name}</h1></div></div><ErrorBox message={companies.error} retry={companies.reload}/>{companies.loading&&<Loading/>}
    {!company&&companies.data&&!companies.data.length&&<section className="panel screen-panel"><Empty>Создайте компанию, затем загрузите выписку.</Empty><form className="action-row" onSubmit={async e=>{e.preventDefault();setCreating(true);setCreateError('');try{const c=await api('createCompany',{data:{name,business_type:'Малый бизнес',timezone:'Europe/Moscow'}});companies.setData([...(companies.data??[]),c]);changeCompany(c.id);}catch(e){setCreateError(messageOf(e));}finally{setCreating(false);}}}><label>Название компании<input required value={name} onChange={e=>setName(e.target.value)}/></label><button className="button button-primary" disabled={creating}>Создать компанию</button></form><ErrorBox message={createError}/></section>}
    {company&&<CompanyWorkspace key={company.id} company={company} section={section} setSection={setSection} capabilities={capabilities}/>}
</main></div>;
}

async function allEvents(companyId:string,signal?:AbortSignal):Promise<PlannedEvent[]> {
  const events:PlannedEvent[]=[];const seen=new Set<string>();let cursor:string|undefined;
  do {const page:EventListResponse=await api('listEvents',{params:{company_id:companyId},data:{limit:100,cursor},signal});events.push(...page.items);if(!page.next_cursor)break;if(seen.has(page.next_cursor))throw new Error('Сервер повторил страницу календаря.');seen.add(page.next_cursor);cursor=page.next_cursor;}while(cursor);
  return events;
}
async function bundle(companyId:string,capabilities?:CapabilitiesResponse,signal?:AbortSignal){
  const params={company_id:companyId};
  const [company,actuals,categories]=await Promise.all([
    api('getCompany',{params,signal}),
    api('actuals',{params,signal}),
    api('categories',{signal})
  ]);
  let events:PlannedEvent[]=[];let eventsError='';
  if(!capabilities||capabilities.features.includes('events')){
    try{events=await allEvents(companyId,signal);}catch(e){if(signal?.aborted)throw e;eventsError=messageOf(e);}
  }
  return {company,actuals,categories:categories.items,events,eventsError,receivedAt:Date.now()};
}

function CompanyWorkspace({company,section,setSection,capabilities}:{company:Company;section:Section;setSection:(s:Section)=>void;capabilities:CapabilitiesResponse}) {
  const source=useResource(signal=>bundle(company.id,capabilities,signal),[company.id]);
  const [forecast,setForecast]=useState<ForecastSnapshot|null>(null), [snapshots,setSnapshots]=useState<SnapshotRef[]>(()=>readSnapshots(company.id)), [forecastPending,setForecastPending]=useState(false), [forecastError,setForecastError]=useState(''), [selectedEvent,setSelectedEvent]=useState(''), [refreshError,setRefreshError]=useState('');
  const [refreshPending,setRefreshPending]=useState(false);
  const active=useRef<AbortController|null>(null), lastRequest=useRef<CreateForecastRequest|null>(null), reqSeq=useRef(0);
  useEffect(()=>{
    const controller=new AbortController(),id=readSnapshots(company.id)[0]?.id;
    if(id){
      const mySeq=++reqSeq.current;
      setForecastPending(true);
      api('getForecast',{params:{company_id:company.id,forecast_id:id},signal:controller.signal})
        .then(validateSnapshot)
        .then(s=>{
          if(!controller.signal.aborted&&mySeq===reqSeq.current){
            setForecast(s);
            if(!lastRequest.current){
try{lastRequest.current=readForecastRequest(sessionStorage,company.id,s.id);}catch{lastRequest.current=null;}

            }
          }
        })
        .catch(e=>{if(!controller.signal.aborted&&mySeq===reqSeq.current)setForecastError(messageOf(e));})
        .finally(()=>{if(!controller.signal.aborted&&mySeq===reqSeq.current)setForecastPending(false);});
    }
    return()=>{controller.abort();active.current?.abort();};
  },[company.id]);
  function accept(s:ForecastSnapshot,request:CreateForecastRequest){
    validateSnapshot(s);
    setForecast(s);
    setSnapshots(rememberSnapshot(company.id,s));
lastRequest.current=request;
    try{storeForecastRequest(sessionStorage,company.id,s.id,request);}catch{/* Storage may be disabled. */}

  }
  async function create(request:CreateForecastRequest){
    active.current?.abort();
    const controller=new AbortController();
    active.current=controller;
    const mySeq=++reqSeq.current;
    setForecastPending(true);
    setForecastError('');
    try{
      const result=await api('createForecast',{params:{company_id:company.id},data:request,signal:controller.signal});
      if(!controller.signal.aborted&&mySeq===reqSeq.current)accept(result,request);
    }catch(e){
      if(!controller.signal.aborted&&mySeq===reqSeq.current)setForecastError(messageOf(e));
      if(e instanceof ApiFailure&&e.status===409)source.reload();
    }finally{
      if(active.current===controller&&mySeq===reqSeq.current)setForecastPending(false);
    }
  }
  async function refresh(){
    setRefreshPending(true);setRefreshError('');
    try{
      const next=await bundle(company.id,capabilities);
      source.setData(next);
      if(forecast){
        const prior=lastRequest.current;
if(prior){
          await create(refreshForecastRequest(prior,next.company.data_revision));
        }else{
          setForecastError('Параметры сохраненного прогноза неизвестны. Выберите условия в обзоре и постройте прогноз, чтобы обновить расчет.');
        }

      }
    }catch(e){setRefreshError(messageOf(e));}finally{setRefreshPending(false);}
  }
  function openEvent(id:string){setSelectedEvent(id);setSection('calendar');}
  if(source.loading&&!source.data)return <Loading/>;
  if(!source.data)return <ErrorBox message={source.error} retry={source.reload}/>;
  const data=source.data;
return <div className="company-body"><div className="workspace-toolbar"><SourceStatus pending={source.loading||refreshPending} error={refreshError||source.error} partial={data.eventsError} receivedAt={data.receivedAt} forecastPending={forecastPending} forecastError={forecastError}/><RefreshButton onRefresh={refresh}/></div><ErrorBox message={source.error} retry={source.reload}/><ErrorBox message={refreshError}/>{section!=='overview'&&<ErrorBox message={forecastError}/>}<Suspense fallback={<Loading text="Открываем раздел…"/>}>
    {section==='overview'&&<ErrorBoundary fallbackTitle="Ошибка в обзоре финансов"><Overview key={company.id} company={data.company} actuals={data.actuals} forecast={forecast} request={lastRequest.current} loading={forecastPending} error={forecastError} create={create} openEvent={openEvent} canProbability={capabilities.features.includes('probability')}/></ErrorBoundary>}

    {section==='operations'&&<ErrorBoundary fallbackTitle="Ошибка в разделе операций"><Operations companyId={company.id} categories={data.categories} totals={data.actuals.category_totals} revision={data.company.data_revision} onChange={refresh}/></ErrorBoundary>}
    {section==='calendar'&&<ErrorBoundary fallbackTitle="Ошибка в платежном календаре"><Calendar companyId={company.id} revision={data.company.data_revision} events={data.events} categories={data.categories} cutoff={forecast?.as_of_date??data.company.balance_date??''} selectedId={selectedEvent} onChange={refresh}/></ErrorBoundary>}
    {section==='scenarios'&&<ErrorBoundary fallbackTitle="Ошибка в стресс-сценариях"><Scenarios companyId={company.id} forecast={forecast} actuals={data.actuals} events={data.events} categories={data.categories}/></ErrorBoundary>}
    {section==='plan'&&<ErrorBoundary fallbackTitle="Ошибка в плане и факте"><PlanActual companyId={company.id} snapshots={snapshots} latestId={forecast?.id??''} revision={data.company.data_revision} openEvent={openEvent} openOverview={()=>setSection('overview')}/></ErrorBoundary>}
    {section==='quality'&&<ErrorBoundary fallbackTitle="Ошибка в проверке качества"><Quality company={data.company} forecast={forecast}/></ErrorBoundary>}
  </Suspense></div>;
}

