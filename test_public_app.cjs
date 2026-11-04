'use strict';
// Real app.js with a synthetic DOM/SDK/API. Never loads PayPal or creates orders.
const test=require('node:test'), assert=require('node:assert/strict');
const fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
async function fixture(options={}){
  const nodes=new Map(),calls=[],scripts=[],configs=[],storage=new Map();let durable=null,createCalls=0,captureCalls=0;
  function node(){return {children:[],handlers:{},hidden:false,disabled:false,checked:false,value:'',textContent:'',
    classList:{remove(){},toggle(){}},setAttribute(){},set id(v){nodes.set(v,this);},
    addEventListener(k,f){this.handlers[k]=f;},replaceChildren(){this.children=[];},append(...x){this.children.push(...x);}};}
  function el(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);}
  const review={operation_id:'00000000-0000-4000-8000-000000000001',cart_sha256:'a'.repeat(64),
    phase:'LOCAL_REVIEW',amount:{currency_code:'USD',value:'14.95'},items:[{sku:'NOTEBOOK',quantity:'1'},{sku:'PEN',quantity:'2'}]};
  const sdk={FUNDING:{PAYPAL:'paypal'},Buttons:config=>{const buttons={closed:false,
    render:async()=>{if(options.renderWait)await options.renderWait.promise;},close(){buttons.closed=true;}};
    configs.push({config,buttons});return buttons;}};
  const context={Intl,Promise,Error,console,document:{getElementById:el,createElement:node,
    head:{append(script){scripts.push(script);queueMicrotask(()=>script.onload());}}},
    paypal:sdk,crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000002'},
    sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    fetch:async(url,request)=>{
      const data=request?.body?JSON.parse(request.body):null;calls.push({url,data});let result,status=200;
      if(url==='/api/catalog')result={catalog:[{sku:'NOTEBOOK',name:'Fictional notebook',usd_cents:995},{sku:'PEN',name:'Fictional pen',usd_cents:250}],csrf:'synthetic',public_sandbox_enabled:options.enabled!==false,sdk_csp_nonce:'synthetic-nonce'};
      else if(url.startsWith('/api/public-sandbox/status/'))result={enabled:true,eligible:!durable,attempt:durable,
        cart_sha256:review.cart_sha256,sdk_url:'https://www.paypal.com/sdk/js?client-id=sb&currency=USD&intent=capture&components=buttons'};
      else if(url==='/api/public-sandbox/begin'){
        if(durable){status=400;result={error:'public_attempt_consumed_do_not_retry'};}
        else {durable={attempt_id:'00000000-0000-4000-8000-000000000003',operation_id:data.operation_id,cart_sha256:data.reviewed_cart_sha256,phase:'SDK_CREATE_OUTCOME_UNKNOWN',order_id:null,buyer_approval_reported:false,order_payload:{intent:'CAPTURE',purchase_units:[{amount:{currency_code:'USD',value:'14.95'}}]}};result=durable;if(options.beginWait)await options.beginWait.promise;}
      }else if(url==='/api/public-sandbox/report'){
        if(options.reportFail){status=400;result={error:'synthetic_local_report_failure'};}
        else {durable={...durable,last_client_event:data.event};if(data.event==='order_id')durable={...durable,order_id:data.order_id,phase:'SDK_CLIENT_REPORTED_ORDER_ID'};if(data.event==='approval')durable={...durable,buyer_approval_reported:true};result=durable;}
      }else throw Error('Unexpected request '+url);
      return {ok:status===200,json:async()=>result};
    }};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'app.js'),'utf8'),context);await flush();
  context.fixtureReview=review;vm.runInContext('showReview(fixtureReview)',context);await flush();
  const actions={order:{create:async payload=>{createCalls++;assert.ok(durable,'local intent must commit first');
    if(options.createWait)return options.createWait.promise;
    if(options.createFailure)throw Error('synthetic SDK failure');return 'SYNTHETICORDER1';},
    capture:async()=>{captureCalls++;throw Error('Capture forbidden');}}};
  return {el,calls,scripts,configs,context,review,actions,createCalls:()=>createCalls,captureCalls:()=>captureCalls,
    durable:()=>durable,setDurable:v=>{durable=v;},async load(){el('sdk-consent').checked=true;el('sdk-consent').handlers.change();await el('sdk-load').handlers.click();}};
}

test('default mode does not load PayPal and opt-in load itself creates no order',async()=>{
  const off=await fixture({enabled:false});assert.equal(off.scripts.length,0);
  const f=await fixture();assert.equal(f.scripts.length,0);assert.equal(f.createCalls(),0);
  await f.load();assert.equal(f.scripts.length,1);assert.equal(f.createCalls(),0);
  assert.equal(f.scripts[0].nonce,'synthetic-nonce');assert.equal(f.configs[0].config.fundingSource,'paypal');
  assert.equal(f.calls.filter(c=>c.url.endsWith('/begin')).length,0);
});

test('duplicate create callback reuses the same Promise after durable claim',async()=>{
  const wait=deferred(),f=await fixture({createWait:wait});await f.load();const config=f.configs[0].config;
  const a=config.createOrder({},f.actions),b=config.createOrder({},f.actions);assert.equal(a,b);
  await flush();assert.equal(f.createCalls(),1);assert.equal(f.durable().phase,'SDK_CREATE_OUTCOME_UNKNOWN');
  wait.resolve('SYNTHETICORDER1');assert.equal(await a,'SYNTHETICORDER1');
  assert.equal(await config.createOrder({},f.actions),'SYNTHETICORDER1');assert.equal(f.createCalls(),1);
  await config.onApprove({orderID:'SYNTHETICORDER1'},f.actions);assert.equal(f.captureCalls(),0);
  assert.equal(f.durable().buyer_approval_reported,true);
});

test('rejected SDK Promise and failed result save never repeat create',async()=>{
  for(const settings of [{createFailure:true},{reportFail:true}]){
    const f=await fixture(settings);await f.load();const config=f.configs[0].config;
    const a=config.createOrder({},f.actions);await assert.rejects(a);
    await assert.rejects(config.createOrder({},f.actions));assert.equal(f.createCalls(),1);
    assert.equal(f.durable().phase,'SDK_CREATE_OUTCOME_UNKNOWN');assert.equal(f.captureCalls(),0);
  }
});

test('an old cart Promise can never supply an order ID to another review',async()=>{
  const f=await fixture();await f.load();const first=f.configs[0].config;
  await first.createOrder({},f.actions);vm.runInContext('invalidate()',f.context);
  const next={...f.review,operation_id:'00000000-0000-4000-8000-000000000004',cart_sha256:'b'.repeat(64)};
  f.context.nextReview=next;vm.runInContext('state.review=nextReview; el("sdk-consent").checked=true',f.context);
  f.context.fixtureActions=f.actions;
  await assert.rejects(vm.runInContext('publicCreateOrder(nextReview, fixtureActions)',f.context),/different_review/);
  assert.equal(f.createCalls(),1);assert.equal(f.calls.filter(c=>c.url.endsWith('/begin')).length,1);
});

test('late old callbacks preserve their attempt without replacing a new review UI',async()=>{
  const f=await fixture();await f.load();const old=f.configs[0].config;await old.createOrder({},f.actions);
  vm.runInContext('invalidate()',f.context);
  const next={...f.review,operation_id:'00000000-0000-4000-8000-000000000004',cart_sha256:'b'.repeat(64)};
  f.context.nextReview=next;vm.runInContext('state.review=nextReview; state.savedReview=nextReview; el("sdk-status").textContent="Review B"',f.context);
  await old.onCancel();assert.equal(f.el('sdk-status').textContent,'Review B');
  await old.onApprove({orderID:'SYNTHETICORDER1'});assert.equal(f.el('sdk-status').textContent,'Review B');
  assert.equal(f.calls.at(-1).data.attempt_id,f.durable().attempt_id);assert.equal(f.captureCalls(),0);
});

test('editing while A creation is pending cannot feed A result into B',async()=>{
  const wait=deferred(),f=await fixture({createWait:wait});await f.load();const old=f.configs[0].config;
  const pending=old.createOrder({},f.actions);await flush();assert.equal(f.createCalls(),1);
  vm.runInContext('invalidate()',f.context);
  f.context.nextReview={...f.review,operation_id:'00000000-0000-4000-8000-000000000004',cart_sha256:'b'.repeat(64)};
  f.context.fixtureActions=f.actions;
  vm.runInContext('state.review=nextReview; state.savedReview=nextReview; el("sdk-consent").checked=true; el("sdk-status").textContent="Review B"',f.context);
  await assert.rejects(vm.runInContext('publicCreateOrder(nextReview,fixtureActions)',f.context),/different_review/);
  wait.resolve('SYNTHETICORDER1');await pending;
  assert.equal(f.el('sdk-status').textContent,'Review B');assert.equal(f.createCalls(),1);
  assert.equal(f.calls.filter(c=>c.url.endsWith('/begin')).length,1);
});

test('late durable begin response neither starts SDK nor overwrites edited review',async()=>{
  const wait=deferred(),f=await fixture({beginWait:wait});await f.load();const old=f.configs[0].config;
  const pending=old.createOrder({},f.actions);await flush();assert.ok(f.durable());
  vm.runInContext('invalidate()',f.context);
  f.context.nextReview={...f.review,operation_id:'00000000-0000-4000-8000-000000000004',cart_sha256:'b'.repeat(64)};
  vm.runInContext('state.review=nextReview; state.savedReview=nextReview; el("sdk-status").textContent="Review B"',f.context);
  wait.resolve();await assert.rejects(pending,/review_changed/);
  assert.equal(f.el('sdk-status').textContent,'Review B');assert.equal(f.createCalls(),0);
  assert.equal(vm.runInContext('state.publicAttempt',f.context),null);
  assert.equal(f.durable().phase,'SDK_CREATE_OUTCOME_UNKNOWN');
});

test('cart change during a delayed render closes the stale buttons',async()=>{
  const wait=deferred(),f=await fixture({renderWait:wait});const loading=f.load();await flush();
  vm.runInContext('invalidate()',f.context);wait.resolve();await loading;
  assert.equal(f.configs[0].buttons.closed,true);assert.equal(f.createCalls(),0);
  await assert.rejects(f.configs[0].config.createOrder({},f.actions));assert.equal(f.createCalls(),0);
});

test('restored consumed attempt leaves load disabled and no SDK request',async()=>{
  const f=await fixture();f.setDurable({attempt_id:'saved',phase:'SDK_CREATE_OUTCOME_UNKNOWN',order_id:null});
  vm.runInContext('showReview(fixtureReview)',f.context);await flush();
  assert.equal(f.el('sdk-consent').disabled,true);assert.equal(f.el('sdk-load').disabled,true);
  assert.equal(f.scripts.length,0);assert.match(f.el('sdk-status').textContent,/No retry/);
});
