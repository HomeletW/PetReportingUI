const test=require('node:test');
const assert=require('node:assert/strict');
const {normalize,groupEvents,initialWindows,acceptPage,safeModelResult,notificationAllowed}=require('../lib/logic');
const cfg={stations:[{id:'feeder',camera:'feeder_camera',zone:'feeder'},{id:'litterbox',camera:'litterbox_camera',zone:'litterbox'}],pet_id:'coconut',merge_gap_seconds:60,max_cluster_seconds:1800,max_cluster_fragments:30,page_limit:2,max_pages:500,max_events:20000};
const event=(id,start,end,extra={})=>({id,start_time:start,end_time:end,camera:'feeder_camera',zones:['feeder'],label:'cat',...extra});

test('station grouping is deterministic and respects gaps',()=>{
 const rows=[event('c',181,190),event('b',70,120),event('a',0,10),event('unrelated',3,4,{camera:'living_camera'})];
 const groups=groupEvents(rows,cfg);
 assert.deepEqual(groups.map(g=>g.member_event_ids),[['a','b'],['c']]);
 assert.deepEqual(groupEvents([...rows].reverse(),cfg),groups);
});
test('ongoing tracks never invent ends or bridge other events',()=>{
 const groups=groupEvents([event('a',0,null),event('b',20,30)],cfg);
 assert.equal(groups.length,2);assert.equal(groups[0].end,null);assert.equal(groups[0].provisional,true);
});
test('one event can support two stations without implying chronology',()=>{
 const sameCamera={...cfg,stations:[{id:'one',camera:'feeder_camera',zone:'feeder'},{id:'two',camera:'feeder_camera',zone:'litterbox'}]};
 assert.equal(groupEvents([event('a',1,5,{zones:['feeder','litterbox']})],sameCamera).length,2);
});
test('long chains are flagged rather than truncated',()=>{
 const rows=Array.from({length:40},(_,i)=>event('e'+i,i*50,i*50+10));
 const result=groupEvents(rows,cfg);assert.equal(result.length,1);assert.equal(result[0].member_event_ids.length,40);assert.equal(result[0].review_required,true);
});
test('saturated windows split and overlap deduplicates event IDs',()=>{
 const s={events:{},pages:0,pending:[],current:{lo:0,hi:100,depth:0}};
 acceptPage(s,[event('a',10,11),event('b',50,51)],cfg);
 assert.ok(s.current && s.pending.length===1);
 acceptPage(s,[event('a',10,11)],cfg);
 acceptPage(s,[event('b',50,51)],cfg);
 assert.deepEqual(Object.keys(s.events).sort(),['a','b']);assert.equal(s.current,null);
});
test('dense timestamp ties fail closed instead of skipping records',()=>{
 const s={events:{},pages:0,pending:[],current:{lo:10,hi:10.00001,depth:48}};
 assert.throws(()=>acceptPage(s,[event('a',10,11),event('b',10,11)],cfg),/saturated_timestamp_boundary/);
});
test('invalid timestamps and IDs are rejected',()=>{
 assert.throws(()=>normalize(event('../bad',0,5)));
 assert.throws(()=>normalize(event('bad',50,5)));
 assert.equal(normalize(event('valid',0,null)).end_time,null);
});
test('model evidence and identity cannot be invented',()=>{
 const visit={id:'visit',pet_id:'coconut',identity_method:'single_pet_assumption'};
 const value={schema_version:1,...visit,visit_id:'visit',observations:[{behavior:'eating',assessment:'supported',evidence_ids:['media'],reason:'Visible eating'}],evidence_quality:'limited'};delete value.id;
 assert.ok(safeModelResult(JSON.stringify(value),visit,'media'));
 value.observations[0].evidence_ids=['invented'];assert.equal(safeModelResult(JSON.stringify(value),visit,'media'),null);
 assert.equal(safeModelResult('not json',visit,'media'),null);
});
test('notification requires all independent permissions',()=>{
 const state={cfg:{notifications_enabled:true,delivery_verified:true},run:{notification_permitted:true},dry_run:false,notify:true,local_date:'2026-11-07',actual_today:'2026-11-07'};
 assert.equal(notificationAllowed(state),true);
 for(const field of ['dry_run','notify']){const s={...state,[field]:field==='dry_run'};assert.equal(notificationAllowed(s),false);}
 assert.equal(notificationAllowed({...state,local_date:'2026-11-06'}),false);
 assert.equal(notificationAllowed({...state,cfg:{...state.cfg,delivery_verified:false}}),false);
});

test('old history retains compact grouping metadata instead of large raw payloads',()=>{
 const s={events:{},raw:{},raw_after:1000,pages:0,pending:[],current:{lo:0,hi:2000,depth:0}};
 const old=event('old',5,6,{data:{large:'x'.repeat(100000)}});
 acceptPage(s,[old],cfg);
 assert.equal(s.events.old.data,undefined);
 assert.equal(s.raw.old,undefined);
 assert.ok(JSON.stringify(s.events).length<500);
});

test('a copied prompt placeholder is not accepted as evidence reasoning',()=>{
 const visit={id:'visit',pet_id:'coconut',identity_method:'single_pet_assumption'};
 const value={schema_version:1,visit_id:'visit',pet_id:'coconut',identity_method:'single_pet_assumption',observations:[{behavior:'unknown',assessment:'uncertain',evidence_ids:['media'],reason:'Brief visible-evidence rationale'}],evidence_quality:'limited'};
 assert.equal(safeModelResult(JSON.stringify(value),visit,'media'),null);
});
