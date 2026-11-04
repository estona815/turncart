'use strict';
const el = id => document.getElementById(id);
const state = { catalog: [], csrf: '', revision: 0, analyzed: null, review: null, savedReview: null, nonce: null,
  publicSandboxEnabled:false, sdkCspNonce:null, publicStatus:null, sdkLoadPromise:null,
  sdkButtons:null, publicOrderPromise:null, publicOrderBinding:null, publicAttempt:null };
const money = cents => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100);
const cart = () => state.catalog.map(p => ({sku:p.sku, quantity:Number(el('qty-'+p.sku).value)})).filter(p=>p.quantity>0);
function remember(id) { try { if(id) sessionStorage.setItem('turncart-review',id); else sessionStorage.removeItem('turncart-review'); } catch(error) { /* Review state still persists on the local server. */ } }
function invalidate() {
  lockPublic('Review the current basket again before using the optional test flow.');
  state.revision += 1; state.analyzed = null; state.nonce = null;
  el('confirm').checked = false; el('confirm').disabled = true; el('create').disabled = true;
  state.review = null;
  el('suggestion').textContent = 'Your request is interpreted locally. Payment always needs a separate review.';
  el('suggestion').classList.remove('error'); renderBasket();
}
function renderBasket() {
  const list=el('basket-items'); list.replaceChildren(); let total=0;
  for(const row of cart()) {
    const product=state.catalog.find(p=>p.sku===row.sku), li=document.createElement('li');
    const name=document.createElement('span'), amount=document.createElement('span');
    name.textContent=product.name+' × '+row.quantity; amount.textContent=money(product.usd_cents*row.quantity);
    li.append(name,amount); list.append(li); total+=product.usd_cents*row.quantity;
  }
  if(!list.children.length){const li=document.createElement('li');li.textContent='Choose an item to begin.';list.append(li);}
  el('total').textContent=money(total);
}
async function api(path, data) {
  const response=await fetch(path,data?{method:'POST',headers:{'Content-Type':'application/json','X-TurnCart-CSRF':state.csrf},body:JSON.stringify(data)}:{});
  const result=await response.json(); if(!response.ok)throw new Error(result.error||'request_failed'); return result;
}
function feedback(text,error=false){el('suggestion').textContent=text;el('suggestion').classList.toggle('error',error);}
function showReview(review, active=true){
  state.savedReview=review;remember(review.operation_id);el('review').hidden=false;
  const savedTotal='Saved review total: '+money(Math.round(Number(review.amount.value)*100))+'. ';
  el('review-status').textContent=savedTotal+(review.phase==='LOCAL_REVIEW'?'Payment has not started.':'Current checkout state: '+review.phase);
  el('review-ref').textContent='Review reference: '+review.operation_id;
  if(active){state.review=review;el('create').disabled=true;el('confirm').disabled=true;}
  if(state.publicSandboxEnabled)preparePublicReview(review,active);
}
function sdkFeedback(text){el('sdk-status').textContent=text;}
function feedbackForReview(text,review){if(state.review?.operation_id===review.operation_id&&state.savedReview?.operation_id===review.operation_id)sdkFeedback(text);}
function lockPublic(text){
  state.publicStatus=null;el('sdk-consent').checked=false;el('sdk-consent').disabled=true;el('sdk-load').disabled=true;
  if(state.sdkButtons){try{state.sdkButtons.close();}catch(error){/* Closing the UI never clears the durable attempt. */}state.sdkButtons=null;}
  if(text)sdkFeedback(text);
}
function publicStatusText(attempt){
  let text='Attempt '+attempt.attempt_id+': '+attempt.phase+'. No retry is offered. ';
  if(attempt.order_id)text+='Client-reported order ID: '+attempt.order_id+'. ';
  if(attempt.buyer_approval_reported)text+='The browser reported buyer approval. ';
  return text+'PayPal status is not server-verified. This app does not capture payments.';
}
async function preparePublicReview(review,active){
  lockPublic();el('public-sandbox').hidden=false;const revision=state.revision;
  el('sdk-basket-summary').textContent='Exact saved test basket: '+review.amount.currency_code+' '+review.amount.value+'.';
  try{
    const status=await api('/api/public-sandbox/status/'+review.operation_id);
    if(revision!==state.revision||state.savedReview?.operation_id!==review.operation_id)return;
    state.publicStatus=status;
    if(status.attempt){sdkFeedback(publicStatusText(status.attempt));return;}
    if(!active||!status.eligible||review.phase!=='LOCAL_REVIEW'||status.cart_sha256!==review.cart_sha256){
      sdkFeedback('This test flow requires an exact saved LOCAL_REVIEW from the fictional catalog. Existing or uncertain attempts remain blocked.');return;
    }
    el('sdk-consent').disabled=false;sdkFeedback('Optional test flow is available for this exact saved basket. No PayPal SDK has been loaded by this action.');
  }catch(error){if(revision===state.revision&&state.savedReview?.operation_id===review.operation_id)sdkFeedback('Public Sandbox readiness could not be read. No order request will be started.');}
}
function loadPublicSdk(url){
  if(!state.sdkLoadPromise){
    state.sdkLoadPromise=new Promise((resolve,reject)=>{
      if(url!=='https://www.paypal.com/sdk/js?client-id=sb&currency=USD&intent=capture&components=buttons')return reject(Error('sdk_url'));
      const script=document.createElement('script');script.src=url;script.nonce=state.sdkCspNonce;
      script.setAttribute('data-csp-nonce',state.sdkCspNonce);script.onload=()=>{
        if(typeof paypal==='undefined'||typeof paypal.Buttons!=='function')reject(Error('sdk_unavailable'));else resolve(paypal);
      };script.onerror=()=>reject(Error('sdk_unavailable'));document.head.append(script);
    });
  }
  return state.sdkLoadPromise;
}
async function reportPublicEvent(event,orderId,attempt=state.publicAttempt){
  if(!attempt)throw Error('no_local_attempt');
  const result=await api('/api/public-sandbox/report',{attempt_id:attempt.attempt_id,
    cart_sha256:attempt.cart_sha256,event,order_id:orderId??null});
  if(state.publicAttempt?.attempt_id===attempt.attempt_id)state.publicAttempt=result;
  if(state.review?.operation_id===attempt.operation_id&&state.savedReview?.operation_id===attempt.operation_id)sdkFeedback(publicStatusText(result));
  return result;
}
function publicCreateOrder(review,actions){
  if(!state.publicSandboxEnabled||!el('sdk-consent').checked||state.review?.operation_id!==review.operation_id
      ||state.review?.cart_sha256!==review.cart_sha256||review.phase!=='LOCAL_REVIEW')return Promise.reject(Error('exact_review_required'));
  if(state.publicOrderPromise&&(state.publicOrderBinding.operation_id!==review.operation_id
      ||state.publicOrderBinding.cart_sha256!==review.cart_sha256))return Promise.reject(Error('different_review_cannot_reuse_order'));
  // The same Promise is retained even after rejection; never re-invoke create.
  if(!state.publicOrderPromise){
    const revision=state.revision;
    state.publicOrderBinding={operation_id:review.operation_id,cart_sha256:review.cart_sha256};
    state.publicOrderPromise=(async()=>{
      const attempt=await api('/api/public-sandbox/begin',{operation_id:review.operation_id,
        reviewed_cart_sha256:review.cart_sha256,confirmed:true});
      if(revision!==state.revision||state.review?.operation_id!==review.operation_id)throw Error('review_changed_attempt_stays_consumed');
      state.publicAttempt=attempt;
      feedbackForReview(publicStatusText(attempt),review);
      // This wrapper calls the SDK action once after durable begin. PayPal's
      // internal request/retry behavior is outside this wrapper's guarantee.
      const id=await actions.order.create(attempt.order_payload);
      await reportPublicEvent('order_id',id,attempt);return id;
    })();
  }
  return state.publicOrderPromise;
}
el('sdk-consent').addEventListener('change',()=>{
  el('sdk-load').disabled=!(state.publicSandboxEnabled&&el('sdk-consent').checked&&state.publicStatus?.eligible&&state.review?.phase==='LOCAL_REVIEW');
});
el('sdk-load').addEventListener('click',async()=>{
  const review=state.review,revision=state.revision;
  if(!review||!state.publicStatus?.eligible||!el('sdk-consent').checked)return;
  el('sdk-load').disabled=true;el('sdk-consent').disabled=true;sdkFeedback('Loading PayPal public Sandbox buttons. No order is requested until a separate PayPal button click.');
  try{
    const sdk=await loadPublicSdk(state.publicStatus.sdk_url);
    if(revision!==state.revision||state.review?.operation_id!==review.operation_id)return;
    const boundAttempt=()=>state.publicAttempt?.operation_id===review.operation_id&&state.publicAttempt?.cart_sha256===review.cart_sha256?state.publicAttempt:null;
    const buttons=sdk.Buttons({fundingSource:sdk.FUNDING.PAYPAL,
      onClick:async(data,actions)=>{
        if(revision!==state.revision||state.review?.operation_id!==review.operation_id||!el('sdk-consent').checked)return actions.reject();
        try{const status=await api('/api/public-sandbox/status/'+review.operation_id);return status.eligible&&revision===state.revision&&state.review?.operation_id===review.operation_id&&el('sdk-consent').checked?actions.resolve():actions.reject();}
        catch(error){return actions.reject();}
      },
      createOrder:(data,actions)=>publicCreateOrder(review,actions),
      onApprove:data=>reportPublicEvent('approval',data.orderID,boundAttempt()).catch(()=>feedbackForReview('Approval callback could not be saved. No capture is implemented; the consumed attempt remains blocked.',review)),
      onCancel:()=>{const attempt=boundAttempt();if(attempt)return reportPublicEvent('cancel',attempt.order_id,attempt).catch(()=>feedbackForReview('Cancellation callback could not be saved. The consumed attempt remains blocked.',review));},
      onError:()=>{const attempt=boundAttempt();if(attempt)return reportPublicEvent('error',attempt.order_id,attempt).catch(()=>feedbackForReview('The result is uncertain. Do not retry this basket.',review));feedbackForReview('PayPal buttons reported an error. No automatic retry is performed.',review);}
    });
    state.sdkButtons=buttons;await buttons.render('#paypal-buttons');
    if(revision!==state.revision||state.review?.operation_id!==review.operation_id){try{buttons.close();}catch(error){}if(state.sdkButtons===buttons)state.sdkButtons=null;return;}
    sdkFeedback('Public Sandbox buttons loaded. A separate PayPal button click may create one attempted order for the exact saved '+review.amount.currency_code+' '+review.amount.value+' basket; this app never captures it.');
  }catch(error){feedbackForReview('PayPal test buttons could not load or render. No automatic retry is performed.',review);}
});
el('intent').addEventListener('input',invalidate);
el('confirm').addEventListener('change',()=>{el('create').disabled=!(el('confirm').checked&&state.analyzed?.model_suggestion.suggested_intent==='checkout'&&!state.review);});
el('intent-form').addEventListener('submit',async e=>{
  e.preventDefault();invalidate();const revision=state.revision;el('analyze').disabled=true;feedback('Understanding your request…');
  try{
    const data=await api('/api/analyze',{intent:el('intent').value,cart:cart()}); if(revision!==state.revision)return;
    state.analyzed=data;state.nonce=crypto.randomUUID();const action=data.model_suggestion.suggested_intent;
    const copy={checkout:'Suggested next step: review the exact items and total, then confirm. Nothing has been purchased.',edit:'Suggested next step: adjust your basket, then tell us what’s next.',abandon:'The request appears to decline checkout. No checkout review has been created.',status:'Suggested next step: check your saved review. Nothing has been purchased from this screen.',clarify:'We need a clearer request. Try describing whether you want to buy, edit, leave, or check your basket.'};
    feedback(copy[action]||copy.clarify);el('confirm').disabled=action!=='checkout';
  }catch(error){if(revision===state.revision)feedback(error.message==='fictional_cart_limit'?'This sample basket must stay at $50 or less.':'Please select an item and use a short English request.',true);}
  finally{el('analyze').disabled=false;}
});
el('create').addEventListener('click',async()=>{
  if(!state.analyzed||!el('confirm').checked||state.review)return;const revision=state.revision;el('create').disabled=true;
  try{const review=await api('/api/reviews',{intent:el('intent').value,cart:cart(),reviewed_cart_sha256:state.analyzed.cart_sha256,confirmed:true,nonce:state.nonce});if(revision!==state.revision)return;showReview(review);feedback('Checkout review created. Your intent did not authorize a payment.');}
  catch(error){if(revision===state.revision){feedback('Could not save this review. Try again with the same confirmed basket.',true);el('create').disabled=false;}}
});
el('refresh').addEventListener('click',async()=>{if(!state.savedReview)return;const id=state.savedReview.operation_id,revision=state.revision;el('refresh').disabled=true;try{const review=await api('/api/reviews/'+id);if(revision===state.revision)showReview(review,state.review?.operation_id===id);}catch(error){feedback('This review could not be read. Payment has not been started by this screen.',true);}finally{el('refresh').disabled=false;}});
async function start(){try{const data=await api('/api/catalog');state.catalog=data.catalog;state.csrf=data.csrf;state.publicSandboxEnabled=data.public_sandbox_enabled===true;state.sdkCspNonce=data.sdk_csp_nonce;if(state.publicSandboxEnabled)el('payment-note').textContent='A local review does not start payment. The separately enabled public Sandbox demonstration below uses PayPal’s shared test merchant and never captures payments.';for(const p of data.catalog){const row=document.createElement('div');row.className='product';const copy=document.createElement('div'),name=document.createElement('strong'),price=document.createElement('span'),select=document.createElement('select');name.textContent=p.name;price.textContent=money(p.usd_cents);copy.append(name,price);select.id='qty-'+p.sku;select.setAttribute('aria-label',p.name+' quantity');for(let n=0;n<=5;n++){const option=document.createElement('option');option.value=String(n);option.textContent=String(n);select.append(option);}select.value=p.sku==='NOTEBOOK'?'1':p.sku==='PEN'?'2':'0';select.addEventListener('change',invalidate);row.append(copy,select);el('catalog').append(row);}renderBasket();}catch(error){el('analyze').disabled=true;feedback('The local service is unavailable. Reload when it is running.',true);}}
async function restore(){let id;try{id=sessionStorage.getItem('turncart-review');}catch(error){return;}if(!id||!/^[0-9a-f-]{36}$/.test(id))return;const revision=state.revision;try{const review=await api('/api/reviews/'+id);if(revision!==state.revision)return;for(const p of state.catalog){const item=review.items.find(i=>i.sku===p.sku);el('qty-'+p.sku).value=item?item.quantity:'0';}renderBasket();el('confirm').checked=true;showReview(review);feedback('Your saved cart review is restored. Tell us a new request to start again.');}catch(error){if(revision===state.revision)remember(null);}}
start().then(()=>{if(state.catalog.length)return restore();});
