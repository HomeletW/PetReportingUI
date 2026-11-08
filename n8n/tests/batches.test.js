const test=require('node:test');
const assert=require('node:assert/strict');
const {DateTime}=require('luxon');
const {newBatchPlan,advanceBatches}=require('../lib/batches');
const {selectRunOptions}=require('../lib/logic');
const now=DateTime.fromISO('2026-11-08T08:00:00',{zone:'America/Toronto'});
const cfg={report_workflow_id:'fixture-workflow',timezone:'America/Toronto',max_batches:120};
const result=(saved,deferred)=>({status:'complete',errors:[],classification_saved:saved,classification_deferred:deferred});

test('live run drains successive three-image batches before enabling delivery',()=>{
 const s=newBatchPlan(cfg,now);
 for(const deferred of [8,5,2]){
  const options=selectRunOptions({},s.phase==='send'?'manual_send':'manual_batch',s.local_date);
  assert.equal(options.notify,false);
  assert.equal(options.dry_run,true);
  advanceBatches(s,result(3,deferred),now);
  assert.equal(s.phase,'classify');
 }
 advanceBatches(s,result(2,0),now);
 assert.equal(s.phase,'send');
 assert.equal(s.action,0);
 const send=selectRunOptions({},'manual_send',s.local_date);
 assert.equal(send.notify,true);assert.equal(send.dry_run,false);
 advanceBatches(s,{...result(0,0),delivery_state:'accepted'},now);
 assert.equal(s.action,1);
 assert.equal(s.total_classified,11);
 assert.equal(s.batches,5);
});

test('dry-run entry cannot be switched to sending via deployment defaults',()=>{
 const options=selectRunOptions({run_options:{dry_run:false,notify:true,mode:'weekly'}},null);
 assert.equal(options.dry_run,true);assert.equal(options.notify,false);
 assert.equal(options.mode,'weekly');
 assert.throws(()=>selectRunOptions({},'unknown','2026-11-08'));
 assert.throws(()=>selectRunOptions({},'manual_send','bad-date'));
});

test('new work found during final preparation returns to classification batches',()=>{
 const s=newBatchPlan(cfg,now);
 advanceBatches(s,result(3,0),now);
 assert.equal(s.phase,'send');
 advanceBatches(s,result(3,4),now);
 assert.equal(s.phase,'classify');
 assert.equal(s.action,0);
});

test('failed, stuck, oversized, or midnight-crossing runs stop instead of looping or notifying',()=>{
 assert.throws(()=>advanceBatches(newBatchPlan(cfg,now),{...result(0,2),status:'partial',errors:['model_failed']},now));
 const stuck=newBatchPlan(cfg,now);advanceBatches(stuck,result(3,8),now);
 assert.throws(()=>advanceBatches(stuck,result(0,8),now),/no_progress/);
 assert.throws(()=>advanceBatches(newBatchPlan({...cfg,max_batches:1},now),result(3,4),now),/batch_limit/);
 assert.throws(()=>advanceBatches(newBatchPlan(cfg,now),result(3,4),now.plus({days:1})),/midnight/);
});

test('processing-only validation drains all batches without ever enabling notification',()=>{
 const s=newBatchPlan({...cfg,send_notification:false},now);
 advanceBatches(s,result(3,2),now);
 assert.equal(s.phase,'classify');
 advanceBatches(s,result(2,0),now);
 assert.equal(s.action,1);assert.equal(s.phase,'classify');
});
