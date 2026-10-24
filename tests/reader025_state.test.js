'use strict';
const assert = require('node:assert/strict');
const R = require('../rss_reader/static/review-state.js');
const C = require('../rss_reader/static/reader-tools.js');
const options = {today:'2026-01-02',isArxiv:true,defaultMinAI:80};
let checks = 0;
function check(condition, label) { assert.ok(condition,label); checks++; }
const step=(state,action)=>R.reducer(state,action);
for (const preset of ['current-week','last-5-days']) {
  const filters=R.normalizeFilters({preset},options);
  check(filters.from==='2025-12-29' && filters.to==='2026-01-02' && filters.read==='all',preset+' boundary');
}
check(R.normalizeFilters({},options).preset==='current-week','fresh default');
check(R.normalizeFilters({preset:'catch-up',read:'unread',from:'1990-01-01'},options).read==='all','old bookmark no unread');
for (const [preset,read] of [['best','all'],['best-unread','unread'],['best-read','read']]) {
  const f=R.normalizeFilters({preset},options);
  check(f.read===read && f.ai==='scored' && f.min_ai===80 && f.sort==='ai',preset);
}
const day='2026-01-02';
const item={id:17,day,is_read:false,is_starred:false,is_read_later:false};
function loaded() {
  let s=R.initialState({preset:'current-week'},options);
  s=step(s,{type:'REQUEST_DAYS',generation:0,requestId:'d'});
  s=step(s,{type:'RECEIVE_DAYS',generation:0,requestId:'d',payload:{days:[{day,total:2,matching:2,unread:2},{day:'undated',total:5,unread:5}],counts:{total:7,unread:7},default_open_day:day,scope:{review_display_mode:'direct'}}});
  s=step(s,{type:'REQUEST_DAY',day,generation:0,requestId:'p'});
  s=step(s,{type:'RECEIVE_DAY',day,generation:0,requestId:'p',payload:{items:[item],next_cursor:'a',has_more:true}});
  return s;
}
let s=loaded();
check(!s.openDays.undated,'undated never auto opens in direct mode');
s=step(s,{type:'TOGGLE_ITEM',itemId:17,open:true});
s=step(s,{type:'OPTIMISTIC_READ',itemId:17,read:true,token:'r'});
check(s.dayData[day].items.length===1 && s.openItems[17] && s.dayData[day].items[0].is_read,'reading retains card and disclosure');
const unread=s.counts.unread;
s=step(s,{type:'OPTIMISTIC_READ',itemId:17,read:true,token:'repeat'});
check(s.counts.unread===unread && s.readMutations[17].token==='r','read single flight');
s=step(s,{type:'ROLLBACK_READ',itemId:17,token:'r'});
check(!s.dayData[day].items[0].is_read && s.counts.unread===7,'read rollback exactly');
s=step(s,{type:'ROLLBACK_READ',itemId:17,token:'r'});
check(s.counts.unread===7,'double rollback ignored');
// A stale response racing with a confirmed mutation cannot undo it.
s=step(s,{type:'REQUEST_DAY',day,generation:0,requestId:'slow'});
s=step(s,{type:'OPTIMISTIC_READ',itemId:17,read:true,token:'r2'});
s=step(s,{type:'CONFIRM_READ',itemId:17,token:'r2'});
s=step(s,{type:'SAVED_CONFIRMED',itemId:17,field:'is_starred',value:true});
s=step(s,{type:'RECEIVE_DAY',day,generation:0,requestId:'slow',payload:{items:[item]}});
check(s.dayData[day].items[0].is_read && s.dayData[day].items[0].is_starred,'stale data cannot reverse read/star');
s=step(s,{type:'REQUEST_DAY',day,generation:0,requestId:'fresh'});
s=step(s,{type:'RECEIVE_DAY',day,generation:0,requestId:'fresh',payload:{items:[item]}});
check(!s.dayData[day].items[0].is_starred,'fresh fetch remains authoritative');
// Collapsing during a load must survive the response.
s=step(s,{type:'REQUEST_DAY',day,generation:0,requestId:'late'});
s=step(s,{type:'TOGGLE_DAY',day,open:false});
s=step(s,{type:'RECEIVE_DAY',day,generation:0,requestId:'late',payload:{items:[item]}});
check(s.openDays[day]===false,'late page does not reopen collapsed day');
// Filters reject all prior generation responses.
s=step(s,{type:'REQUEST_DAY',day,generation:0,requestId:'old'});
s=step(s,{type:'APPLY_PRESET',preset:'best-read',options});
const same=s;
s=step(s,{type:'RECEIVE_DAY',day,generation:0,requestId:'old',payload:{items:[item]}});
check(s===same,'old generation response ignored');
s=loaded();s=step(s,{type:'REQUEST_DAYS',generation:0,requestId:'older'});
s=step(s,{type:'FAIL_DAYS',generation:0,requestId:'older',error:'connection lost'});
check(s.status==='ready' && s.days.length===2,'older-days failure retains loaded days');
s=step(s,{type:'REQUEST_DAY',day,generation:0,requestId:'next',append:true});
s=step(s,{type:'FAIL_DAY',day,generation:0,requestId:'next',error:'connection lost'});
check(s.dayData[day].items.length===1 && s.dayData[day].hasMore,'next-page failure retains cards and cursor');
s=step(s,{type:'REQUEST_DAY',day,generation:0,requestId:'race'});
s=step(s,{type:'FINISH_DAY_START',day});s=step(s,{type:'FINISH_DAY_SUCCESS',day});
s=step(s,{type:'RECEIVE_DAY',day,generation:0,requestId:'race',payload:{items:[item,{...item,id:18}]}});
check(s.dayData[day].items.every(i=>i.is_read),'whole-day read wins over in-flight page');
check(s.openDays[day] && s.counts.unread>=0,'day remains open after marking read');
// URL recognition does not probe any remote source.
check(C.videoId('https://www.youtube.com/shorts/NmiD1YdbrAA')==='NmiD1YdbrAA','shorts id');
check(C.videoId('https://youtu.be/NmiD1YdbrAA')==='NmiD1YdbrAA','short url');
for(const url of ['javascript:alert(1)','https://youtube.com.evil.example/shorts/NmiD1YdbrAA','https://user@youtube.com/shorts/NmiD1YdbrAA','https://youtube.com/watch?v=x']) check(!C.videoId(url),'reject '+url);
check(!C.articleURL('http://example.test') && !C.articleURL('javascript:alert(1)'),'article embed https only');
const chunks=C.chunksFor('A long sentence. '.repeat(300),180);
check(chunks.length>5 && chunks.every(p=>p.length<=181),'speech bounded chunks');
check(C.chunksFor('').length===0,'speech empty');
// Simulated platform API: test cancellation, stale callbacks, pause/resume and errors.
let spoken=[], cancelled=0, paused=0, resumed=0;
const synth={speak:u=>spoken.push(u),cancel:()=>cancelled++,pause:()=>paused++,resume:()=>resumed++};
function Utterance(text){this.text=text;}
let timers=[]; const speech=C.createSpeechController(synth,Utterance,()=>{},{setTimeout:fn=>{timers.push(fn);return fn;},clearTimeout:fn=>{timers=timers.filter(f=>f!==fn);}});
check(!speech.speak('secret',{localService:false}) && spoken.length===0,'never use cloud voice');
const local={localService:true,lang:'fr-FR'};
check(speech.speak('Sentence. '.repeat(100),local),'local voice starts');
const old=spoken[0];speech.pauseResume();check(speech.state().status==='paused' && paused===1,'pause');
speech.pauseResume();check(speech.state().status==='speaking' && resumed===1,'resume');
speech.speak('New selection.',local);timers.shift()?.();const n=spoken.length;old.onend();
check(spoken.length===n,'old end cannot resume stale selection');
old.onerror({error:'interrupted'});check(speech.state().status==='speaking','old error cannot stop new selection');
speech.stop();spoken.at(-1).onend();check(spoken.length===n && speech.state().status==='idle','stop cancels queue');
speech.speak('A valid passage.',local);spoken.at(-1).onerror({error:'synthesis-failed'});
check(speech.state().status==='error','real error terminates cleanly');
check(!speech.speak('',local) && speech.state().status==='idle','no text safe');
console.log(`Reader 0.25 state/media/speech suite: ${checks} assertions passed.`);
