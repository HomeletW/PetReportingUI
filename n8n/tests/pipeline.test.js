const test=require('node:test');
const assert=require('node:assert/strict');
const {DateTime}=require('luxon');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const common=fs.readFileSync(path.join(__dirname,'../lib/logic.js'),'utf8').split('if (typeof module')[0]+'\n'+fs.readFileSync(path.join(__dirname,'../lib/pipeline.js'),'utf8').split('if(typeof module')[0];
const context=vm.createContext({Date,console});
vm.runInContext(common,context);
const cfg={...require('../config.example.json'),max_backfill_days:0,inference_enabled:false,notifications_enabled:false};
const now=DateTime.fromISO('2026-11-07T08:00:00',{zone:cfg.timezone});
const run={run_id:'00000000-0000-4000-8000-000000000001',lease_token:'00000000-0000-4000-8000-000000000002',notification_permitted:false};

test('calendar intervals use Toronto DST rather than fixed hours',()=>{
 const fall=context.calendar('2026-11-01',now,cfg.timezone);
 assert.equal(fall.plus({days:1}).diff(fall,'hours').hours,25);
 const spring=context.calendar('2026-03-08',now,cfg.timezone);
 assert.equal(spring.plus({days:1}).diff(spring,'hours').hours,23);
 assert.throws(()=>context.calendar('2026-02-30',now,cfg.timezone));
});

test('complete dry-run simulation has one daily report and no delivery',()=>{
 let s=context.initialize(cfg,{dry_run:true,notify:true},now,'fixture');
 const seen=[];
 for(let i=0;i<100;i++){
   const tag=s.request.tag;seen.push(tag);
   if(tag==='done')break;
   if(tag==='group'){s=context.afterCollection(s,now);continue;}
   let body={},headers={};
   if(tag==='readiness')body={ready:true};
   if(tag==='claim')body=run;
   if(tag==='processing')body={days:[],open_event_ids:[]};
   if(tag==='login')headers={'set-cookie':['session=fixture; HttpOnly']};
   if(tag==='stored_boundary')body={events:[],next_cursor:null};
   if(tag==='source_page')body=[];
   if(tag==='reconcile')body={visit_ids:[]};
   if(tag==='prepare')body={report_id:'fixture-report',revision:1,facts:{counts:{feeder:0,litterbox:0}}};
   if(tag==='finalize')body={report_id:'fixture-report',revision:1,url:'http://localhost:8092/reports/fixture-report',usable:false};
   if(tag==='heartbeat')headers={};
   s=context.advance(s,{statusCode:200,headers,body},now.plus({milliseconds:i}));
 }
 assert.equal(s.request.tag,'done');
 assert.equal(s.reports.length,1);
 assert.equal(s.notify,false);
 assert.ok(!seen.includes('delivery_claim') && !seen.includes('delivery_send'));
});

test('heartbeat and cleanup bodies match their special server contracts',()=>{
 const s={run,seq:0,execution_id:'fixture'};
 context.reportRequest(s,'POST','/runs/'+run.run_id+'/heartbeat',{lease_token:run.lease_token},'heartbeat');
 assert.equal(s.request.body.run_id,undefined);
 context.reportRequest(s,'POST','/maintenance/cleanup',{dry_run:true},'cleanup');
 assert.equal(s.request.body.run_id,undefined);
});

test('midnight continuation expands reconciliation ownership range',()=>{
 const start=DateTime.fromISO('2026-11-05T23:59:00',{zone:cfg.timezone}).toSeconds();
 const end=start+120;
 const raw={id:'cross-midnight',label:'cat',camera:'feeder_camera',zones:['feeder'],start_time:start,end_time:end};
 const s={cfg,run,seq:0,execution_id:'boundary',dates:['2026-11-06'],local_day_ends:{'2026-11-06':'2026-11-07'},collection:{events:{[raw.id]:raw},pages:1},collection_complete:true,context_loaded:'2026-11-05:2026-11-07'};
 context.afterCollection(s,now);
 assert.equal(s.affected.local_start,'2026-11-05');
 assert.equal(s.affected.local_end,'2026-11-07');
 assert.equal(s.candidates.length,1);
});

test('weekly mode still stores a daily report and selects only weekly for notification',()=>{
 const sunday=DateTime.fromISO('2026-11-08T08:00:00',{zone:cfg.timezone});
 const s=context.initialize(cfg,{dry_run:true},sunday,'weekly');
 s.run=run;s.selected_kind='weekly';s.week_start='2026-11-01';s.dates=['2026-11-07'];s.local_day_ends={'2026-11-07':'2026-11-08'};
 context.prepareNext(s);
 assert.equal(s.current_report.kind,'daily');
 assert.equal(s.report_queue.length,1);
 assert.equal(s.report_queue[0].kind,'weekly');
 assert.equal(s.report_queue[0].local_start,'2026-11-01');
});

test('normal morning selects yesterday even when older processing dates are missing',()=>{
 const result=context.selectDates({days:[{local_date:'2026-10-30',collection_status:'missing'}]},now,cfg);
 assert.deepEqual(Array.from(result),['2026-11-06']);
});

test('source windows cover one completed day plus boundary context',()=>{
 const settings={...cfg,boundary_context_seconds:300};
 let s=context.initialize(settings,{dry_run:true},now,'window');
 s=context.advance(s,{statusCode:200,body:{ready:true}},now);
 s=context.advance(s,{statusCode:200,body:run},now);
 s=context.advance(s,{statusCode:200,body:{days:[],open_event_ids:[]}},now);
 assert.equal(s.request.tag,'stored_boundary');
 assert.equal(s.query_upper-s.query_lower,86400+600);
 s=context.advance(s,{statusCode:200,body:{events:[],next_cursor:null}},now);
 s=context.advance(s,{statusCode:200,headers:{'set-cookie':['session=fixture']}},now);
 assert.equal(s.request.tag,'source_page');
 assert.ok(s.request.body.after>s.query_lower-0.001);
 assert.ok(s.request.body.before<s.query_upper+0.001);
});

test('notification calls exactly the selected allowlisted group and preserves mobile links',()=>{
 for(const group of ['all_devices','apple_devices','android_devices']){
   const s={cfg:{...cfg,notification_service:'notify.'+group},seq:0,execution_id:'notify',local_date:'2026-11-07',request:{tag:'delivery_claim'},selected_report:{summary:'Fixture summary',url:'http://localhost:8092/reports/daily/2026-11-06'}};
   context.advance(s,{statusCode:200,body:{may_send:true,attempt_id:'fixture-attempt'}},now);
   assert.equal(s.request.tag,'delivery_send');
   assert.equal(s.request.path,'/api/services/notify/'+group);
   assert.equal(s.request.body.data.url,s.selected_report.url);
   assert.equal(s.request.body.data.clickAction,s.selected_report.url);
 }
 assert.throws(()=>context.initialize({...cfg,notification_service:'notify.unapproved'},{},now,'invalid'));
});

test('replayed or denied delivery claims never reach a notification service',()=>{
 for(const response of [{statusCode:200,body:{may_send:false}},{statusCode:200,headers:{'idempotency-replayed':'true'},body:{may_send:true}}]){
   const s={cfg,seq:0,execution_id:'replay',request:{tag:'delivery_claim'},dry_run:false};
   context.advance(s,response,now);
   assert.equal(s.request.tag,'cleanup');
 }
});

test('notification failures are recorded as unknown without a second send',()=>{
 for(const response of [{error:'fixture timeout'},{statusCode:500,body:{}}]){
   const s={cfg,run,seq:0,execution_id:'error',request:{tag:'delivery_send'},delivery_attempt:'fixture-attempt'};
   context.advance(s,response,now);
   assert.equal(s.request.tag,'delivery_result');
   assert.equal(s.request.body.result,'unknown');
 }
});

test('uncached eligible visits beyond the batch budget are counted for the next run',()=>{
 const s={cfg:{...cfg,inference_enabled:true,max_classifications_per_run:3},run,seq:0,execution_id:'batch',request:{tag:'visit_detail'},current_media:{media_id:'media-id'},current_visit:{id:'visit-id',member_event_ids:['event-id']},classification_count:3,classification_deferred:0,media_index:0,candidates:[],clock_ms:now.toMillis(),last_heartbeat:now.toMillis(),dates:['2026-11-06'],local_day_ends:{'2026-11-06':'2026-11-07'}};
 context.advance(s,{statusCode:200,body:{media:[{id:'media-id',status:'ready',sha256:'fixture'}]}},now);
 assert.equal(s.classification_deferred,1);
 assert.equal(s.request.tag,'prepare');
});

test('pending classifications prevent premature delivery even for a send-enabled request',()=>{
 const s={cfg:{...cfg,notifications_enabled:true,delivery_verified:true},run:{...run,notification_permitted:true},dry_run:false,notify:true,local_date:'2026-11-07',actual_today:'2026-11-07',classification_deferred:1,errors:[]};
 assert.equal(context.notificationAllowed(s),false);
 s.classification_deferred=0;assert.equal(context.notificationAllowed(s),true);
});

test('morning notification uses saved Sunday weekly report without recollection or inference',()=>{
 const sunday=DateTime.fromISO('2026-11-08T08:00:00',{zone:cfg.timezone});
 const enabled={...cfg,notifications_enabled:true,delivery_verified:true};
 const input=context.selectRunOptions(enabled,'notify_only','2026-11-08');
 let s=context.initialize(enabled,input,sunday,'saved-notification');
 const seen=[];
 for(let i=0;i<20;i++){
   const tag=s.request.tag;seen.push(tag);
   if(tag==='done')break;
   let body={};
   if(tag==='notification_report'){
     assert.match(s.request.path,/kind=weekly/);
     body={available:true,usable:true,report_id:'saved-week',summary:'Saved summary',url:'https://example.invalid/reports/weekly/2026-11-01'};
   }
   if(tag==='claim')body={...run,notification_permitted:true};
   if(tag==='delivery_claim')body={may_send:true,attempt_id:'delivery-test'};
   if(tag==='delivery_send')assert.equal(s.request.body.message,'Saved summary');
   if(tag==='cleanup')assert.equal(s.request.body.dry_run,false);
   s=context.advance(s,{statusCode:200,body},sunday);
 }
 assert.equal(s.request.tag,'done');
 assert.ok(seen.includes('delivery_send'));
 assert.deepEqual(seen,['readiness','notification_report','claim','delivery_claim','delivery_send','delivery_result','cleanup','finish','done']);
});

test('missing morning report stops without claiming a run, recollecting or sending',()=>{
 const enabled={...cfg,notifications_enabled:true,delivery_verified:true};
 let s=context.initialize(enabled,context.selectRunOptions(enabled,'notify_only','2026-11-07'),now,'missing-report');
 s=context.advance(s,{statusCode:200,body:{ready:true}},now);
 assert.equal(s.request.tag,'notification_report');
 assert.match(s.request.path,/kind=daily/);
 s=context.advance(s,{statusCode:200,body:{available:false,reason:'generation_not_complete'}},now);
 assert.equal(s.request.tag,'done');
 assert.ok(s.failure);
 assert.equal(s.run,null);
});

test('unfinished classification batches are durably marked as classify, not finished',()=>{
 const s={cfg,run,seq:0,execution_id:'pending',request:{tag:'cleanup'},errors:[],classification_deferred:2};
 context.advance(s,{statusCode:200,body:{}},now);
 assert.equal(s.request.tag,'finish');
 assert.equal(s.request.body.stage,'classify');
});
