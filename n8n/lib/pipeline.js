'use strict';

function atSeconds(value, now, zone) {
  return now.plus({milliseconds:value*1000-now.toMillis()}).setZone(zone);
}

function calendar(iso, now, zone) {
  const parts = iso.split('-').map(Number);
  const result = now.setZone(zone).set({year:parts[0],month:parts[1],day:parts[2],hour:0,minute:0,second:0,millisecond:0});
  if (!result.isValid || result.toISODate() !== iso) throw new Error('invalid_local_date');
  return result;
}

function request(state, kind, method, path, body = null, tag = path) {
  state.seq += 1;
  if(kind==='image')state.current_prompt=observationPrompt(state.current_visit,state.current_media.media_id);
  state.request = {kind,method,path,body,tag,key:`pet:${state.execution_id}:${state.seq}`};
  state.action = kind === 'report' ? (method === 'GET' ? 1 : 0) : kind === 'login' ? 2 : kind === 'source' ? 3 : kind === 'group' ? 4 : kind === 'image' ? 5 : kind === 'notify' ? 6 : 7;
  if(state.cfg?.max_state_bytes && JSON.stringify(state).length>state.cfg.max_state_bytes){
    state.collection={events:{},raw:{},pending:[],current:null,pages:0};
    state.events_to_store=[];state.candidates=[];state.request.body=null;
    throw new Error('workflow_state_limit');
  }
  return state;
}

function withLease(state, value) {
  return {run_id:state.run.run_id, lease_token:state.run.lease_token, ...value};
}

function initialize(cfg, input, now, executionId) {
  notificationService(cfg);
  const today = now.setZone(cfg.timezone).startOf('day');
  const date = input.local_date || today.toISODate();
  const day = calendar(date, now, cfg.timezone);
  if (date > today.toISODate() || today.diff(day,'days').days > 89) throw new Error('invalid_run_date');
  const dryRun = input.dry_run !== false;
  const state = {cfg,execution_id:String(executionId),seq:0,local_date:date,actual_today:today.toISODate(),dry_run:dryRun,notify:!dryRun && input.notify === true,notification_only:input.notification_only===true,resend_key:input.manual_resend===true && input.notification_only===true && !dryRun ? `manual-${executionId}` : null,requested_dates:input.backfill_dates || null,mode:input.mode || 'auto',stage:'collect',clock_ms:now.toMillis(),errors:[],reports:[],classification_failures:0,classification_count:0,classification_saved:0,classification_deferred:0,delivery_state:'not_requested',review_clusters:0,source_cookie:'',run:null};
  if (!['auto','daily','weekly'].includes(state.mode)) throw new Error('invalid_report_mode');
  return request(state,'report','GET','/readiness',null,'readiness');
}

function claimRun(state) {
  return reportRequest(state,'POST','/runs/claim',{household_id:state.cfg.household_id,local_date:state.local_date,config_hash:state.cfg.grouping_version+':'+state.cfg.prompt_version,dry_run:state.dry_run,notify:state.notify},'claim');
}

function reportRequest(state, method, path, value, tag) {
  if (state.run && value != null && path !== '/maintenance/cleanup' && !path.endsWith('/heartbeat')) value = withLease(state,value);
  return request(state,'report',method,path,value,tag);
}

function boundaryRead(state,start,end,tag,cursor){
  state.boundary_read_start=start;state.boundary_read_end=end;
  const path='/events?start='+encodeURIComponent(new Date(start*1000).toISOString())+'&end='+encodeURIComponent(new Date(end*1000).toISOString())+'&limit=100'+(cursor?'&cursor='+cursor:'');
  return reportRequest(state,'GET',path,null,tag);
}

function nextSource(state) {
  const current=state.collection.current;
  if (!current) {
    const open=state.known_open.shift();
    if(open) return request(state,'source','GET','/api/events/'+encodeURIComponent(open),null,'refresh_open');
    return request(state,'group','LOCAL','',null,'group');
  }
  const q = {labels:'cat',cameras:[...new Set(state.cfg.stations.filter(x=>x.enabled!==false).map(x=>x.camera))].join(','),limit:state.cfg.page_limit,include_thumbnails:0,sort:'date_asc',after:current.lo-0.00001,before:current.hi+0.00001};
  return request(state,'source','GET','/api/events',q,'source_page');
}

function afterCollection(state, now) {
  state.clock_ms=now.toMillis();
  const cfg=state.cfg;
  const events=Object.values(state.collection.events);
  const visits=groupEvents(events,cfg);
  const first=calendar(state.dates[0],now,cfg.timezone);
  const last=calendar(state.dates[state.dates.length-1],now,cfg.timezone).plus({days:1});
  let lo=first.toSeconds(), hi=last.toSeconds();
  let changed=true;
  while(changed) {
    changed=false;
    for(const v of visits) {
      const vend=v.provisional?Infinity:v.end;
      if(v.start < hi && vend >= lo) {
        const nlo=calendar(atSeconds(v.start,now,cfg.timezone).toISODate(),now,cfg.timezone).toSeconds();
        const bound = Math.max(...v.member_event_ids.map(id=>state.collection.events[id].end_time ?? state.collection.events[id].start_time));
        const nhi=atSeconds(bound,now,cfg.timezone).startOf('day').plus({days:1}).toSeconds();
        if(nlo<lo){lo=nlo;changed=true;}
        if(nhi>hi){hi=nhi;changed=true;}
      }
    }
  }
  const from=atSeconds(lo,now,cfg.timezone);
  const to=atSeconds(hi,now,cfg.timezone);
  if(to.diff(from,'days').days>31) throw new Error('boundary_range_exceeds_31_days');
  state.affected={local_start:from.toISODate(),local_end:to.toISODate()};
  const contextKey=from.toISODate()+':'+to.toISODate();
  if((from.toISODate()<state.dates[0] || to.toISODate()>state.local_day_ends[state.dates[state.dates.length-1]]) && state.context_loaded!==contextKey){
    state.context_loaded=contextKey;
    return boundaryRead(state,lo,hi,'boundary_context');
  }
  state.candidates=visits.filter(v=>v.start>=lo && v.start<hi);
  state.review_clusters=state.candidates.filter(v=>v.review_required).length;
  const raw=state.collection.raw || state.collection.events;
  const required=[...new Set(state.candidates.flatMap(v=>v.member_event_ids))];
  const missing=required.filter(id=>!raw[id]);
  if(missing.length){
    if(state.member_fetch_failed)throw new Error('source_member_missing');
    state.raw_pending=missing;
    const id=state.raw_pending.shift();
    return request(state,'source','GET','/api/events/'+encodeURIComponent(id),null,'member_detail');
  }
  state.candidates=state.candidates.map(v=>{
    const members=v.member_event_ids.map(id=>state.collection.events[id]);
    const best=members.find(e=>e.has_snapshot)||members[0];
    return {...v,evidence:{id:best.id,has_clip:best.has_clip}};
  });
  state.collection.raw=raw;
  state.events_to_store=Object.keys(raw);
  state.source_cookie='';
  state.upsert_index=0;
  state.day_index=0;
  state.stage='group';
  return nextUpsert(state);
}

function nextUpsert(state) {
  const ids=state.events_to_store.slice(state.upsert_index,state.upsert_index+200);
  const batch=ids.map(id=>normalize(state.collection.raw[id]));
  state.last_upsert_ids=ids;
  state.upsert_index+=ids.length;
  const day=state.dates[state.day_index];
  const dayEnd=state.local_day_ends[day];
  return reportRequest(state,'POST','/events/upsert',{source_id:state.cfg.source_id,local_start:day,local_end:dayEnd,events:batch,collection_status:state.collection_complete?'complete':'partial',coverage:{status:'unknown',retained_history_only:true,pages:state.collection.pages}},'upsert');
}

function nextDayStatus(state) {
  state.day_index+=1;
  if(state.day_index<state.dates.length) return nextUpsert(state);
  if(!state.collection_complete) return prepareNext(state);
  const candidates=state.candidates.map(v=>({station_id:v.station_id,pet_id:v.pet_id,identity_method:v.identity_method,member_event_ids:v.member_event_ids}));
  if(candidates.length>1000 || candidates.some(v=>v.member_event_ids.length>1000)) throw new Error('grouping_batch_limit');
  return reportRequest(state,'PUT','/visits/batch',{...state.affected,grouping_version:state.cfg.grouping_version,candidates},'reconcile');
}

function nextMedia(state) {
  if(state.clock_ms-state.last_heartbeat>60000){state.after_heartbeat='media';return reportRequest(state,'POST',`/runs/${state.run.run_id}/heartbeat`,{lease_token:state.run.lease_token},'heartbeat');}
  if(state.media_index>=state.candidates.length) return prepareNext(state);
  const v=state.candidates[state.media_index];
  const best=v.evidence;
  state.current_visit={...v,id:state.visit_ids[state.media_index]};
  state.current_event=best.id;
  const variants=['snapshot'];
  if(state.cfg.bbox_verified) variants.push('bbox');
  if(state.cfg.clip_verified && best.has_clip && !v.provisional) variants.push('clip');
  return reportRequest(state,'POST','/media/cache',{event_ids:[best.id],variants},'cache');
}

function prepareNext(state) {
  state.stage='report';
  if(!state.report_queue) {
    state.report_queue=state.dates.map(d=>({kind:'daily',local_start:d,local_end:state.local_day_ends[d],timezone:state.cfg.timezone}));
    if(state.selected_kind==='weekly') state.report_queue.push({kind:'weekly',local_start:state.week_start,local_end:state.local_date,timezone:state.cfg.timezone});
  }
  if(!state.report_queue.length) {
    if(notificationAllowed(state) && state.selected_report?.usable) return reportRequest(state,'POST','/deliveries/claim',{local_send_date:state.local_date,channel:'home_assistant',report_id:state.selected_report.report_id,...(state.resend_key?{resend_key:state.resend_key}:{})},'delivery_claim');
    return reportRequest(state,'POST','/maintenance/cleanup',{dry_run:state.dry_run},'cleanup');
  }
  state.current_report=state.report_queue.shift();
  return reportRequest(state,'POST','/reports/prepare',state.current_report,'prepare');
}

function notificationPayload(state, now) {
  const report=state.selected_report;
  const info=report.notification || {};
  const name=info.pet_name || state.cfg.pet_name || 'Coconut';
  const weekly=(info.kind || state.selected_kind)==='weekly';
  const end=calendar(info.local_end || state.local_date,now,state.cfg.timezone).minus({days:1});
  const start=calendar(info.local_start || end.minus({days:weekly?6:0}).toISODate(),now,state.cfg.timezone);
  const period=weekly ? start.toFormat(start.year===end.year?'MMM d':'MMM d, yyyy')+'–'+end.toFormat('MMM d, yyyy') : start.toFormat('MMM d, yyyy');
  const title=`New ${period}${weekly?' weekly':''} journal for ${name} available! 🐾`;
  const counts=info.counts;
  let message;
  if(counts && ['feeder','litterbox'].every(k=>Number.isInteger(counts[k] ?? 0) && (counts[k] ?? 0)>=0)) {
    const feeder=counts.feeder || 0, litter=counts.litterbox || 0;
    const visits=n=>n===1?'visit':'visits';
    message=`🐾 A little peek at ${name}’s ${weekly?'week':'day'}: the camera spotted ${feeder} ${visits(feeder)} by the feeder and ${litter} litterbox ${visits(litter)}.`;
  } else {
    message=`🐾 ${name}’s ${weekly?'weekly':'daily'} journal is ready, with the moments caught on camera.`;
  }
  if(info.collection_status && info.collection_status!=='complete')message+=` This is a partial ${weekly?'week':'day'}, so some moments may be missing.`;
  message+=' Take a peek! 💛\n\n'+report.url;
  const icon=info.icon_url || state.cfg.report_url.replace(/\/$/,'')+'/static/icon.png';
  return {title,message,data:{url:report.url,clickAction:report.url,tag:`pet-report-${state.local_date}${state.resend_key?"-"+state.resend_key:""}`,icon_url:icon,notification_icon:'mdi:cat'}};
}

function markFailed(state, code) {
  state.errors.push(code);
  if(state.run) return reportRequest(state,'POST',`/runs/${state.run.run_id}/finish`,{status:'failed',stage:state.stage,error_codes:[code],diagnostic:{request_stage:state.request.tag,http_status:state.last_status || 0,code:state.failure_kind || 'http_failure'}},'failed_finish');
  state.failure=code;
  return request(state,'done','LOCAL','',null,'done');
}

function advance(state, response, now) {
  state.clock_ms=now.toMillis();
  const r=state.request;
  const status=response.statusCode ?? (response.error ? 0 : 200);
  const body=response.body ?? response;
  state.last_status=status;
  if(r.tag==='finish'||r.tag==='failed_finish') {
    if(status!==200) state.errors.push('dependency_unavailable');
    state.source_cookie='';
    delete state.collection;
    delete state.events_to_store;
    return request(state,'done','LOCAL','',null,'done');
  }
  if(r.tag==='delivery_send') {
    // No HTTP or transport error is automatically retried after submission.
    return reportRequest(state,'POST',`/deliveries/${state.delivery_attempt}/result`,{result:status>=200 && status<300?'accepted':'unknown',...(status>=200 && status<300?{}:{error_code:'timeout'})},'delivery_result');
  }
  if(status<200 || status>=300) {
    if(r.tag==='refresh_open' && status===404){state.collection_complete=false;state.errors.push('collection_incomplete');return nextSource(state);}
    if(r.kind==='source' && status===401 && !state.auth_refreshed){state.auth_refreshed=true;state.resume_request=r;return request(state,'login','POST','/api/login',{},'relogin');}
    if(r.tag==='cache' || r.tag==='classification' || r.tag==='visit_detail') {state.errors.push(r.tag==='cache'?'media_unavailable':'model_failed');state.media_index+=1;return nextMedia(state);}
    return markFailed(state,r.kind==='source'?'collection_incomplete':'dependency_unavailable');
  }
  switch(r.tag) {
    case 'readiness': {
      if(state.notification_only) {
        const day=calendar(state.local_date,now,state.cfg.timezone);
        const kind=state.mode==='auto'?(day.weekday===state.cfg.weekly_weekday?'weekly':'daily'):state.mode;
        return reportRequest(state,'GET',`/reports/for-delivery?local_send_date=${state.local_date}&kind=${kind}`,null,'notification_report');
      }
      return claimRun(state);
    }
    case 'notification_report': {
      if(!body.available || !body.usable)return markFailed(state,'dependency_unavailable');
      state.selected_report=body;
      return claimRun(state);
    }
    case 'claim': {
      state.run=body;
      state.last_heartbeat=now.toMillis();
      const day=calendar(state.local_date,now,state.cfg.timezone);
      state.week_start=day.minus({days:7}).toISODate();
      state.selected_kind=state.mode==='auto'?(day.weekday===state.cfg.weekly_weekday?'weekly':'daily'):state.mode;
      if(state.notification_only) {
        if(!notificationAllowed(state))return markFailed(state,'dependency_unavailable');
        return reportRequest(state,'POST','/deliveries/claim',{local_send_date:state.local_date,channel:'home_assistant',report_id:state.selected_report.report_id,...(state.resend_key?{resend_key:state.resend_key}:{})},'delivery_claim');
      }
      return reportRequest(state,'GET',`/processing-days?local_start=${day.minus({days:7}).toISODate()}&local_end=${state.local_date}`,null,'processing');
    }
    case 'processing': {
      const day=calendar(state.local_date,now,state.cfg.timezone);
      state.dates=selectDates(body,day,state.cfg,state.requested_dates);
      state.local_day_ends=Object.fromEntries(state.dates.map(d=>[d,calendar(d,now,state.cfg.timezone).plus({days:1}).toISODate()]));
      state.local_day_end_seconds=Object.fromEntries(state.dates.map(d=>[d,calendar(d,now,state.cfg.timezone).plus({days:1}).toSeconds()]));
      state.collection={events:{},raw:{},raw_after:calendar(state.dates[0],now,state.cfg.timezone).toSeconds()-state.cfg.merge_gap_seconds,pending:[],current:null,pages:0};
      state.known_open=body.open_event_ids || [];
      state.query_lower=calendar(state.dates[0],now,state.cfg.timezone).toSeconds()-state.cfg.boundary_context_seconds;
      state.query_upper=Math.min(now.toSeconds(),calendar(state.local_day_ends[state.dates[0]],now,state.cfg.timezone).toSeconds()+state.cfg.boundary_context_seconds);
      state.context_loaded=null;
      return boundaryRead(state,state.query_lower,state.query_upper,'stored_boundary');
    }
    case 'login': case 'relogin': {
      const headers=response.headers || {};
      const setCookie=headers['set-cookie'] || headers['Set-Cookie'];
      if(!setCookie) return markFailed(state,'dependency_unavailable');
      state.source_cookie=(Array.isArray(setCookie)?setCookie:[setCookie]).map(c=>c.split(';')[0]).join('; ');
      if(r.tag==='relogin'){const next=state.resume_request;delete state.resume_request;return request(state,next.kind,next.method,next.path,next.body,next.tag);}
      state.collection.pending=initialWindows(state.query_lower,state.query_upper);
      state.collection.current=state.collection.pending.pop()||null;
      state.collection_complete=true;
      return nextSource(state);
    }
    case 'stored_boundary': case 'boundary_context': {
      for(const record of body.events || []){
        const raw=record.raw;
        normalize(raw);
        if(!state.collection.events[raw.id]){
          state.collection.events[raw.id]={id:raw.id,start_time:raw.start_time,end_time:raw.end_time,camera:raw.camera,zones:raw.zones,label:raw.label,has_snapshot:raw.has_snapshot,has_clip:raw.has_clip};
          state.collection.raw[raw.id]=raw;
        }
      }
      if(Object.keys(state.collection.events).length>state.cfg.max_events)return markFailed(state,'collection_incomplete');
      if(body.next_cursor)return boundaryRead(state,state.boundary_read_start,state.boundary_read_end,r.tag,body.next_cursor);
      if(r.tag==='stored_boundary')return request(state,'login','POST','/api/login',{},'login');
      return request(state,'group','LOCAL','',null,'group');
    }
    case 'source_page': {
      try{acceptPage(state.collection,body,state.cfg);}catch{state.collection_complete=false;state.errors.push('collection_incomplete');state.collection.current=null;state.collection.pending=[];}
      if(now.toMillis()-state.last_heartbeat>60000){state.after_heartbeat='source';return reportRequest(state,'POST',`/runs/${state.run.run_id}/heartbeat`,{lease_token:state.run.lease_token},'heartbeat');}
      return nextSource(state);
    }
    case 'refresh_open': {
      try{normalize(body);state.collection.events[body.id]={id:body.id,start_time:body.start_time,end_time:body.end_time,camera:body.camera,zones:body.zones,label:body.label,has_snapshot:body.has_snapshot,has_clip:body.has_clip};state.collection.raw[body.id]=body;}catch{state.collection_complete=false;state.errors.push('collection_incomplete');}
      return nextSource(state);
    }
    case 'member_detail': {
      try{normalize(body);state.collection.raw[body.id]=body;}catch{state.member_fetch_failed=true;return markFailed(state,'collection_incomplete');}
      const id=state.raw_pending.shift();
      if(id)return request(state,'source','GET','/api/events/'+encodeURIComponent(id),null,'member_detail');
      return request(state,'group','LOCAL','',null,'group');
    }
    case 'heartbeat': state.last_heartbeat=now.toMillis(); return state.after_heartbeat==='media'?nextMedia(state):nextSource(state);
    case 'upsert': {
      for(const id of state.last_upsert_ids || []) delete state.collection.raw[id];
      if(state.upsert_index<state.events_to_store.length)return nextUpsert(state);
      state.collection.events={};state.collection.raw={};state.events_to_store=[];state.last_upsert_ids=[];
      return nextDayStatus(state);
    }
    case 'reconcile': state.visit_ids=body.visit_ids;state.media_index=0;state.stage='classify';return nextMedia(state);
    case 'cache': {
      const snapshot=body.media.find(m=>m.status==='ready' && m.variant==='snapshot');
      state.current_media=snapshot || null;
      if(!snapshot) state.errors.push('media_unavailable');
      if(snapshot && state.cfg.inference_enabled){return reportRequest(state,'GET','/visits/'+state.current_visit.id,null,'visit_detail');}
      state.media_index+=1;
      return nextMedia(state);
    }
    case 'visit_detail': {
      const media=body.media.find(m=>m.id===state.current_media.media_id && m.status==='ready');
      const stamp=media?.sha256 ? inputFingerprint(state.current_visit,media,state.cfg) : null;
      state.current_fingerprint=stamp;
      if(stamp && body.classification && body.input_fingerprint===stamp && body.prompt_version===state.cfg.prompt_version && body.model?.name===state.cfg.model){state.media_index+=1;return nextMedia(state);}
      if(state.classification_count>=state.cfg.max_classifications_per_run){state.classification_deferred+=1;state.media_index+=1;return nextMedia(state);}
      state.classification_count+=1;
      return request(state,'image','GET',state.current_media.inference_path,null,'image');
    }
    case 'classification': state.classification_saved+=1;state.media_index+=1;return nextMedia(state);
    case 'prepare': state.current_prepared=body;return reportRequest(state,'PUT',`/reports/${body.report_id}/finalize`,{expected_revision:body.revision,use_template:true},'finalize');
    case 'finalize': {
      state.reports.push({kind:state.current_report.kind,local_start:state.current_report.local_start,...body});
      const yesterday=calendar(state.local_date,now,state.cfg.timezone).minus({days:1}).toISODate();
      if(state.current_report.kind===state.selected_kind && (state.selected_kind==='weekly'||state.current_report.local_start===yesterday))state.selected_report=body;
      return prepareNext(state);
    }
    case 'delivery_claim': {
      state.delivery_state=body.state || 'claimed';
      if(!body.may_send || response.headers?.['idempotency-replayed']==='true')return reportRequest(state,'POST','/maintenance/cleanup',{dry_run:state.dry_run},'cleanup');
      state.delivery_attempt=body.attempt_id;
      return request(state,'notify','POST','/api/services/notify/'+notificationService(state.cfg),notificationPayload(state,now),'delivery_send');
    }
    case 'delivery_result': state.delivery_state=body.state;if(body.state==='unknown')state.errors.push('delivery_unknown');return reportRequest(state,'POST','/maintenance/cleanup',{dry_run:state.dry_run},'cleanup');
    case 'cleanup': return reportRequest(state,'POST',`/runs/${state.run.run_id}/finish`,{status:state.errors.length?'partial':'complete',stage:state.classification_deferred>0?'classify':'finished',error_codes:[...new Set(state.errors)]},'finish');
    default: throw new Error('unexpected_pipeline_stage');
  }
}

if(typeof module!=='undefined')module.exports={calendar,request,withLease,initialize,reportRequest,nextSource,afterCollection,nextMedia,prepareNext,advance,markFailed};
