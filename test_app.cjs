'use strict';
// Isolated UI regression: real app.js, synthetic DOM/API, no server or database.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function fixture() {
  const nodes = new Map(), storage = new Map(), calls = [];
  function node() {
    return {hidden:false, disabled:false, checked:false, value:'', textContent:'', children:[], handlers:{},
      classList:{remove(){}, toggle(){}}, setAttribute(){},
      set id(value){nodes.set(value,this);},
      addEventListener(k,f){this.handlers[k]=f;}, replaceChildren(){this.children=[];},
      append(...items){this.children.push(...items);}};
  }
  function el(id) {if(!nodes.has(id)) nodes.set(id,node());return nodes.get(id);}
  const review = {operation_id:'00000000-0000-4000-8000-000000000001',phase:'LOCAL_REVIEW',
    amount:{currency_code:'USD',value:'12.00'},items:[{sku:'NOTEBOOK',quantity:1}]};
  const context = {Intl, document:{getElementById:el,createElement:node},
    sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    crypto:{randomUUID:()=> 'fixture-nonce'},
    fetch:async(url,options)=>{
      calls.push({url,options});
      let result;
      if(url==='/api/catalog') result={catalog:[{sku:'NOTEBOOK',name:'Notebook',usd_cents:1200}],csrf:'fixture'};
      else if(url==='/api/analyze') result={model_suggestion:{suggested_intent:el('intent').value.includes('status')?'status':'checkout'},cart_sha256:'fixture-hash'};
      else if(url==='/api/reviews/'+review.operation_id) result=review;
      else throw Error('Unexpected request: '+url);
      return {ok:true,json:async()=>result};
    }};
  vm.createContext(context);
  await vm.runInContext(fs.readFileSync(path.join(__dirname,'app.js'),'utf8'),context);
  context.fixtureReview=review;
  vm.runInContext('showReview(fixtureReview)',context);
  return {el,storage,calls,review,context};
}

test('typing and analyzing status preserves the saved review and permits refresh',async()=>{
  const f=await fixture();
  f.el('intent').value='What is my order status?';f.el('intent').handlers.input();
  await f.el('intent-form').handlers.submit({preventDefault(){}});
  assert.equal(f.storage.get('turncart-review'),f.review.operation_id);
  assert.equal(f.el('review').hidden,false);
  await f.el('refresh').handlers.click();
  assert.ok(f.calls.some(c=>c.url==='/api/reviews/'+f.review.operation_id));
  assert.match(f.el('review-status').textContent,/Saved review total: \$12\.00/);
  assert.equal(f.el('confirm').disabled,true);
  assert.equal(f.el('create').disabled,true);
});

test('quantity changes invalidate confirmation and cannot submit a stale cart',async()=>{
  const f=await fixture();
  f.el('intent').value='buy now';f.el('intent').handlers.input();
  await f.el('intent-form').handlers.submit({preventDefault(){}});
  f.el('confirm').checked=true;f.el('confirm').handlers.change();
  assert.equal(f.el('create').disabled,false);
  f.el('qty-NOTEBOOK').value='2';f.el('qty-NOTEBOOK').handlers.change();
  assert.equal(f.el('confirm').checked,false);
  assert.equal(f.el('confirm').disabled,true);
  assert.equal(f.el('create').disabled,true);
  await f.el('create').handlers.click();
  assert.equal(f.calls.filter(c=>c.url==='/api/reviews').length,0);
  assert.equal(f.storage.get('turncart-review'),f.review.operation_id);
  await f.el('refresh').handlers.click();
  assert.match(f.el('total').textContent,/24\.00/);
  assert.match(f.el('review-status').textContent,/12\.00/);
  assert.equal(f.el('confirm').checked,false);
  assert.equal(f.el('create').disabled,true);
});
