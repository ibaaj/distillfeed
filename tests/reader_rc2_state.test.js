'use strict';
const assert=require('node:assert/strict');
const R=require('../rss_reader/static/review-state.js');
const C=require('../rss_reader/static/reader-tools.js');
let checks=0; function check(ok,label){assert.ok(ok,label);checks++;}
function clock(){let serial=0;const tasks=new Map();return {setTimeout(fn){tasks.set(++serial,fn);return serial;},clearTimeout(id){tasks.delete(id);},tick(){const entry=tasks.entries().next().value;if(entry){tasks.delete(entry[0]);entry[1]();}},size(){return tasks.size;}};}
for (const today of ['2026-01-01','2024-02-29','2026-10-24','2026-12-31']) {
 const f=R.normalizeFilters({preset:'current-month'},{today});
 check(f.from===`${today.slice(0,7)}-01` && f.to===today && f.read==='all','month including leap/year boundaries');
}
for(const preset of ['current-week','current-month','everything']) {
 const options={defaultPreset:preset,defaultPageSize:50,today:'2026-10-24'};
 check(R.normalizeFilters({},options).preset===preset,'server adaptive default');
 check(R.normalizeFilters({},options).page_size===50,'page size preference');
 check(R.normalizeFilters({preset:'best-read'},options).preset==='best-read','explicit URL wins');
}
const text='A sentence about reasoning. Another sentence about uncertainty! '.repeat(80);
for(const size of [180,600,2400]){
 const chunks=C.chunksFor(text,size);
 check(chunks.every(v=>v.length<=size),'bounded segments');
 check(chunks.join(' ')===text.trim(),'no omitted/repeated speech words');
 check(chunks.slice(0,-1).every(v=>/[.!?]$/.test(v)),'sentence boundaries preferred');
}
check(C.chunksFor('hello\u0000 world').join(' ')==='hello world','control chars removed');
const timer=clock(); let spoken=[],cancels=0,pauses=0,resumes=0;
const synth={speaking:false,pending:false,paused:false,speak(u){spoken.push(u);},cancel(){cancels++;},pause(){this.paused=true;pauses++;},resume(){this.paused=false;resumes++;}};
function U(text){this.text=text;}
const events=[];
const speech=C.createSpeechController(synth,U,s=>events.push(s),{volume:.4,rate:.9,pauseMs:120,chunkChars:180,...timer});
const voice={localService:true,lang:'fr-FR'};
speech.stop();speech.stop();check(cancels===0,'idle stop never touches system audio');
check(!speech.speak('private',{localService:false}) && spoken.length===0,'no remote voice fallback');
check(speech.speak(text,voice),'starts only from explicit speak');
check(cancels===0 && spoken[0].volume===.4 && spoken[0].rate===.9,'initial speak has no pointless cancel and bounded volume');
const first=spoken[0];first.onend();check(timer.size()===1 && speech.state().status==='waiting','gap after sentence');
first.onend();check(timer.size()===1,'duplicate end ignored');
speech.pauseResume();check(speech.state().status==='paused' && timer.size()===0,'pause between chunks');
timer.tick();check(spoken.length===1,'paused gap never speaks');
speech.pauseResume();timer.tick();check(spoken.length===2,'resume next segment exactly once');
speech.pauseResume();check(pauses===1 && speech.state().status==='paused','pause active utterance');
speech.pauseResume();check(resumes===1 && speech.state().status==='speaking','resume active utterance');
const old=spoken.at(-1);speech.speak('New selection.',voice);
check(speech.state().status==='waiting' && cancels===1 && spoken.length===2,'cancel and restart separated');
old.onend();old.onerror({error:'interrupted'});check(timer.size()===1,'stale native callbacks harmless');
timer.tick();check(spoken.length===3 && spoken.at(-1).text==='New selection.','replacement starts once');
spoken.at(-1).onend();speech.stop();check(cancels===1,'normal finish does not cancel audio');
speech.speak(text,voice);spoken.at(-1).onend();speech.stop();timer.tick();check(speech.state().status==='idle' && timer.size()===0,'stop clears next-segment timer');
speech.speak('Failure',voice);spoken.at(-1).onerror({error:'synthesis-failed'});check(speech.state().status==='error','platform failure visible');
const muted=C.createSpeechController(synth,U,()=>{},{volume:0});const before=spoken.length;
check(!muted.speak('Silent',voice) && before===spoken.length,'muted setting performs no synthesis');
(async()=>{
 let resolvers=[],writes=[],states=[];
 const saver=C.createSerialSaver(value=>new Promise((resolve,reject)=>{writes.push(value);resolvers.push({resolve,reject});}),s=>states.push(s));
 const running=saver.save(16);saver.save(18);saver.save(22);
 check(writes.join(',')==='16','one config request in flight');
 resolvers.shift().resolve();await new Promise(r=>setImmediate(r));
 check(writes.join(',')==='16,22','intermediate drag sizes coalesced');
 check(states.some(s=>s.status==='saved' && !s.current && s.value===16),'old acknowledgment cannot mark newest value saved');
 resolvers.shift().reject(new Error('offline'));await running;
 check(!saver.state().running && states.at(-1).status==='error' && states.at(-1).current,'failure unlocks slider and is reported');
 const retry=saver.save(22);resolvers.shift().resolve();await retry;
 check(states.at(-1).status==='saved' && states.at(-1).current,'retry persists latest chosen size');
 console.log(`RC2 preset/audio/font state suite: ${checks} assertions passed.`);
})().catch(error=>{console.error(error);process.exitCode=1;});
