'use strict';

function newBatchPlan(cfg, now) {
  if(!cfg.report_workflow_id || !Number.isInteger(cfg.max_batches) || cfg.max_batches<1 || cfg.max_batches>1000)throw new Error('invalid_batch_configuration');
  return {cfg,local_date:now.setZone(cfg.timezone).toISODate(),phase:'classify',action:0,batches:0,previous_deferred:null,total_classified:0,result:null};
}

function manualBatchPlan(cfg, workflowId, now) {
  return newBatchPlan({...cfg,report_workflow_id:workflowId,send_notification:cfg.dry_run!==true && cfg.send_notification!==false},now);
}

function advanceBatches(state, result, now) {
  if(now.setZone(state.cfg.timezone).toISODate()!==state.local_date)throw new Error('manual_run_crossed_midnight');
  if(result.status!=='complete' || result.errors?.length)throw new Error('report_batch_incomplete');
  const deferred=result.classification_deferred;
  const saved=result.classification_saved;
  if(!Number.isInteger(deferred) || deferred<0 || !Number.isInteger(saved) || saved<0)throw new Error('invalid_batch_result');
  state.batches+=1;
  state.total_classified+=saved;
  state.result=result;
  if(deferred>0) {
    if(state.previous_deferred!==null && deferred>=state.previous_deferred && saved===0)throw new Error('classification_batch_no_progress');
    state.previous_deferred=deferred;
    state.phase='classify';
  } else if(state.phase==='classify') {
    if(state.cfg.send_notification===false){state.action=1;return state;}
    state.phase='send';
  } else {
    state.action=1;
    return state;
  }
  if(state.batches>=state.cfg.max_batches)throw new Error('classification_batch_limit');
  return state;
}

if(typeof module!=='undefined')module.exports={newBatchPlan,manualBatchPlan,advanceBatches};
