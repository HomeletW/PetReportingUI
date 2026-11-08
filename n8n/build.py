"""Build a portable SDK workflow. Actual installation settings never enter this file."""

import json
from pathlib import Path

ROOT = Path(__file__).parent


class Expr(str):
    pass


class Cred(str):
    pass


def emit(value):
    if isinstance(value, Expr):
        return "expr(" + json.dumps(value) + ")"
    if isinstance(value, Cred):
        return "newCredential(" + json.dumps(value) + ")"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + emit(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(emit(v) for v in value) + "]"
    return json.dumps(value)


def runtime(value):
    if isinstance(value, Expr):
        return "=" + value
    if isinstance(value, dict):
        return {k: runtime(v) for k, v in value.items()}
    if isinstance(value, list):
        return [runtime(v) for v in value]
    return value


logic = (ROOT / "lib/logic.js").read_text().split("if (typeof module")[0]
pipeline = (ROOT / "lib/pipeline.js").read_text().split("if(typeof module")[0]
common = logic + "\n" + pipeline + "\n"
cfg = json.loads((ROOT / "config.example.json").read_text())
cfg["run_options"] = {"dry_run": True, "notify": False, "mode": "auto"}
cfg["schedule_options"] = {"dry_run": False, "notify": True, "mode": "auto"}
nodes = []


def add(var, name, kind, version, params, output=None, credentials=None, **settings):
    config = {"name": name, "parameters": params, **settings}
    if credentials:
        config["credentials"] = {k: Cred(v) for k, v in credentials.items()}
    node = {
        "type": kind,
        "version": version,
        "config": config,
        "output": output if output is not None else [{"action": 0, "ok": True}],
    }
    nodes.append((var, node))


add("manual", "Manual dry run", "n8n-nodes-base.manualTrigger", 1, {})
add(
    "schedule",
    "Morning schedule (inactive)",
    "n8n-nodes-base.scheduleTrigger",
    1.4,
    {
        "rule": {
            "interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 8, "triggerAtMinute": 0}]
        },
        "misfirePolicy": "skip",
    },
)
add(
    "settingsNode",
    "Deployment settings",
    "n8n-nodes-base.set",
    3.5,
    {"mode": "raw", "jsonOutput": json.dumps(cfg), "includeOtherFields": False},
    [cfg],
)
add(
    "initializeNode",
    "Plan local calendar",
    "n8n-nodes-base.code",
    2,
    {
        "mode": "runOnceForAllItems",
        "jsCode": common
        + "return [{json:initialize($input.first().json,$execution.mode === 'trigger' ? ($input.first().json.schedule_options || {}) : ($input.first().json.run_options || {}),$now,$execution.id)}];",
    },
    [{"action": 1, "request": {"method": "GET", "path": "/readiness"}, "dry_run": True}],
)
add(
    "dispatch",
    "Dispatch action",
    "n8n-nodes-base.switch",
    3.5,
    {"mode": "expression", "numberOutputs": 8, "output": Expr("{{ $json.action }}")},
)
opts = {
    "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}},
    "redirect": {"redirect": {"followRedirects": False}},
    "timeout": 300000,
}
add(
    "write",
    "Persist report progress",
    "n8n-nodes-base.httpRequest",
    4.5,
    {
        "method": Expr("{{ $json.request.method }}"),
        "url": Expr('{{ $json.cfg.report_url + "/internal/v1" + $json.request.path }}'),
        "authentication": "genericCredentialType",
        "genericAuthType": "httpTemplatedCustomAuth",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [{"name": "Idempotency-Key", "value": Expr("{{ $json.request.key }}")}]
        },
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": Expr("{{ JSON.stringify($json.request.body) }}"),
        "options": opts,
    },
    [{"statusCode": 200, "headers": {}, "body": {"ok": True}}],
    {"httpTemplatedCustomAuth": "Pet Reporting API"},
    onError="continueRegularOutput",
    retryOnFail=True,
    maxTries=3,
    waitBetweenTries=1000,
)
add(
    "read",
    "Read report checkpoint",
    "n8n-nodes-base.httpRequest",
    4.5,
    {
        "method": "GET",
        "url": Expr('{{ $json.cfg.report_url + "/internal/v1" + $json.request.path }}'),
        "authentication": "genericCredentialType",
        "genericAuthType": "httpTemplatedCustomAuth",
        "options": opts,
    },
    [{"statusCode": 200, "headers": {}, "body": {"ready": True}}],
    {"httpTemplatedCustomAuth": "Pet Reporting API"},
    onError="continueRegularOutput",
    retryOnFail=True,
    maxTries=3,
    waitBetweenTries=1000,
)
add(
    "login",
    "Authenticate Frigate",
    "n8n-nodes-base.httpRequest",
    4.5,
    {
        "method": "POST",
        "url": Expr('{{ $json.cfg.frigate_url + "/api/login" }}'),
        "authentication": "genericCredentialType",
        "genericAuthType": "httpTemplatedCustomAuth",
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": "{}",
        "options": {
            **opts,
            "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "text"}},
        },
    },
    [{"statusCode": 200, "headers": {"set-cookie": ["session=fixture-only; HttpOnly"]}, "body": {}}],
    {"httpTemplatedCustomAuth": "Pet Reporting Frigate Login"},
    onError="continueRegularOutput",
)
source_opts = {**opts, "timeout": 30000}
add(
    "source",
    "Read yesterday detections",
    "n8n-nodes-base.httpRequest",
    4.5,
    {
        "method": "GET",
        "url": Expr("{{ $json.cfg.frigate_url + $json.request.path }}"),
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "Cookie", "value": Expr("{{ $json.source_cookie }}")}]},
        "sendQuery": True,
        "specifyQuery": "json",
        "jsonQuery": Expr("{{ JSON.stringify($json.request.body || {}) }}"),
        "options": source_opts,
    },
    [{"statusCode": 200, "headers": {}, "body": []}],
    onError="continueRegularOutput",
    retryOnFail=True,
    maxTries=3,
    waitBetweenTries=1000,
)
add(
    "advanceNode",
    "Advance run checkpoint",
    "n8n-nodes-base.code",
    2,
    {
        "mode": "runOnceForAllItems",
        "jsCode": common
        + "const state=$('Dispatch action').item.json;\ntry{return [{json:advance(state,$input.first().json,$now)}];}catch{state.failure_kind='invalid_response';return [{json:markFailed(state,'dependency_unavailable')}];}",
    },
)
add(
    "group",
    "Reconcile station visits",
    "n8n-nodes-base.code",
    2,
    {
        "mode": "runOnceForAllItems",
        "jsCode": common
        + "const state=$input.first().json;\ntry{return [{json:afterCollection(state,$now)}];}catch{return [{json:markFailed(state,'collection_incomplete')}];}",
    },
)
image_opts = {
    **opts,
    "timeout": 60000,
    "response": {
        "response": {
            "responseFormat": "file",
            "outputPropertyName": "data",
            "fullResponse": True,
            "neverError": False,
        }
    },
}
add(
    "image",
    "Load classification evidence",
    "n8n-nodes-base.httpRequest",
    4.5,
    {
        "method": "GET",
        "url": Expr("{{ $json.cfg.report_url + $json.request.path }}"),
        "authentication": "genericCredentialType",
        "genericAuthType": "httpTemplatedCustomAuth",
        "options": image_opts,
    },
    [{"ok": True}],
    {"httpTemplatedCustomAuth": "Pet Reporting API"},
    onError="continueRegularOutput",
)
add(
    "vision",
    "Observe visible behavior",
    "@n8n/n8n-nodes-langchain.ollama",
    1,
    {
        "resource": "image",
        "operation": "analyze",
        "modelId": {"__rl": True, "mode": "id", "value": cfg["model"]},
        "text": Expr('{{ $("Dispatch action").item.json.current_prompt }}'),
        "inputType": "binary",
        "binaryPropertyName": "data",
        "simplify": True,
        "options": {"temperature": 0, "think": False, "num_predict": 512, "format": "json", "num_ctx": 4096},
    },
    [{"content": "{}"}],
    {"ollamaApi": "Ollama account"},
    onError="continueRegularOutput",
)
parse = (
    common
    + """const state=$('Dispatch action').item.json;
state.clock_ms=$now.toMillis();
const parsed=safeModelResult($input.first().json,state.current_visit,state.current_media.media_id);
if(parsed){return [{json:reportRequest(state,'PUT','/visits/'+state.current_visit.id+'/classification',{result:parsed,model:{name:state.cfg.model},prompt_version:state.cfg.prompt_version,input_fingerprint:state.current_fingerprint || ('uncached:'+state.execution_id+':'+state.seq)},'classification')}];}
state.classification_failures+=1;state.errors.push('model_failed');state.media_index+=1;
return [{json:nextMedia(state)}];"""
)
add(
    "validateModel",
    "Validate model observations",
    "n8n-nodes-base.code",
    2,
    {"mode": "runOnceForAllItems", "jsCode": parse},
)
add(
    "notify",
    "Notify report devices",
    "n8n-nodes-base.homeAssistant",
    1,
    {
        "resource": "service",
        "operation": "call",
        "domain": "notify",
        "service": Expr("{{ $json.request.path.split('/').pop() }}"),
        "serviceAttributes": {
            "attributes": [
                {"name": "title", "value": Expr("{{ $json.request.body.title }}")},
                {"name": "message", "value": Expr("{{ $json.request.body.message }}")},
                {"name": "data", "value": Expr("{{ $json.request.body.data }}")},
            ]
        },
    },
    [{}],
    {"homeAssistantApi": "Home Assistant account"},
    onError="continueRegularOutput",
    retryOnFail=False,
    alwaysOutputData=True,
)

summary = "const s=$input.first().json; return [{json:{run_id:s.run?.run_id,dry_run:s.dry_run,local_date:s.local_date,status:s.failure?'failed':s.errors.length?'partial':'complete',errors:[...new Set(s.errors)],review_clusters:s.review_clusters,classification_failures:s.classification_failures,reports:s.reports,selected_report:s.selected_report || null,notification_enabled:s.cfg.notifications_enabled && s.cfg.delivery_verified}}];"
add("complete", "Run summary", "n8n-nodes-base.code", 2, {"mode": "runOnceForAllItems", "jsCode": summary})
lines = [
    "import { workflow, node, trigger, switchCase, sticky, newCredential, expr } from '@n8n/workflow-sdk';"
]
for var, value in nodes:
    fn = "trigger" if var in ("manual", "schedule") else "switchCase" if var == "dispatch" else "node"
    lines.append("const " + var + " = " + fn + "(" + emit(value) + ");")
lines.append(
    "const note = sticky('## Inactive draft\\nManual runs default to dry_run=true, notify=false. Validate footage before activation. Select one notify group: all_devices (default), apple_devices, or android_devices. Verify the connection before delivery. Do not export installation settings or credentials to a public repository.');"
)
lines.append("export default workflow('pet-reporting-morning','Pet Journal · Morning reports')")
lines += [
    ".add(manual).to(settingsNode).to(initializeNode).to(dispatch)",
    ".add(schedule).to(settingsNode)",
    ".add(dispatch.onCase(0,write.to(advanceNode)).onCase(1,read.to(advanceNode)).onCase(2,login.to(advanceNode)).onCase(3,source.to(advanceNode)).onCase(4,group.to(dispatch)).onCase(5,image.to(vision).to(validateModel).to(dispatch)).onCase(6,notify.to(advanceNode)).onCase(7,complete))",
    ".add(advanceNode).to(dispatch)",
    ".add(note)",
    ".group('Plan report dates',[settingsNode,initializeNode],{description:'Chooses local calendar dates and defaults every manual run to unsent processing.'})",
    ".group('Build activity journal',[dispatch,write,read,login,source,advanceNode,group,image,vision,validateModel,notify],{description:'Collects yesterday’s detections, reconciles visits, caches evidence and saves daily or weekly reports with guarded delivery.'});",
]
code = "\n".join(lines) + "\n"
(ROOT / "workflows/morning.sdk.ts").write_text(code)
(ROOT / "workflows/node-configs.json").write_text(
    json.dumps(
        [
            {
                "name": v["config"]["name"],
                "type": v["type"],
                "typeVersion": v["version"],
                "parameters": runtime(v["config"]["parameters"]),
            }
            for _, v in nodes
        ],
        indent=2,
    )
    + "\n"
)
print(f"Built {len(nodes)} nodes; {len(code)} SDK characters")
