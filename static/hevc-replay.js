import {demuxHevc} from './hevc-mp4.mjs';
import {createHevcPlayer} from './vendor/libde265-web-0.1.4/dist/esm/index.js';
const $=id=>document.getElementById(id),status=$('status'),button=$('play'),seek=$('seek');
let decoder,clip,index=0,playing=false,serial=0,clock=0,origin=0,timer,failed=false;
const pause=()=>{playing=false;clearTimeout(timer);button.textContent='Phát';};
function fail(e){failed=true;pause();status.textContent='Không phát được: '+e.message;}
function start(){if(index>=clip.samples.length){decoder.reset();index=0;}failed=false;playing=true;clock=performance.now();origin=clip.samples[index].time;button.textContent='Tạm dừng';tick();}
function tick(){if(!playing)return;try{const s=clip.samples[index];if(!s){pause();decoder.flush().catch(fail);return;}const delay=(s.time-origin)*1000-(performance.now()-clock);if(delay>2){timer=setTimeout(tick,Math.min(delay,100));return;}if(decoder.getStats().decodeQueueSize>4){timer=setTimeout(tick,10);return;}decoder.pushAccessUnit(clip.packet(index),{timestamp:Math.round(s.time*1e6),duration:Math.round(s.duration*1e6),key:s.key});index++;timer=setTimeout(tick,0);}catch(e){fail(e);}}
button.onclick=()=>playing?pause():start();
seek.onchange=async()=>{const token=++serial;pause();decoder.reset();index=0;const target=Number(seek.value);for(let i=0;i<clip.samples.length&&clip.samples[i].time<=target;i++)if(clip.samples[i].key)index=i;await Promise.resolve();if(token===serial)start();};
document.addEventListener('visibilitychange',()=>{if(document.hidden)pause();});
window.addEventListener('pagehide',()=>{pause();decoder?.close();});
try{
 const src=new URL(new URLSearchParams(location.search).get('src')||'',location.origin);
 if(src.origin!==location.origin||!src.pathname.startsWith('/video/')||src.search||src.hash)throw Error('Chỉ nhận bản ghi nội bộ.');
 const response=await fetch(src,{credentials:'same-origin'});if(!response.ok)throw Error('Không tải được video ('+response.status+').');
 const reader=response.body.getReader(),parts=[];let length=0;while(true){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>32*1024*1024){await reader.cancel();throw Error('Clip vượt 32 MB; hãy chọn đoạn ngắn hơn.');}parts.push(value);}
 const data=new Uint8Array(length);let p=0;for(const x of parts){data.set(x,p);p+=x.length;}clip=demuxHevc(data.buffer);
 decoder=await createHevcPlayer({canvas:$('screen'),mode:'wasm',wasmBaseUrl:'/static/vendor/libde265-web-0.1.4/dist/wasm/',wasmWorker:'required',workerFrameOutput:'planes',lowLatency:false,onFrame:frame=>{seek.value=String(frame.timestamp/1e6);$('time').textContent=(frame.timestamp/1e6).toFixed(1)+' / '+clip.duration.toFixed(1)+' s';},onError:fail});
 decoder.on('frame',()=>{const stats=decoder.getStats();$('screen').dataset.decodedFrames=String(stats.decodedFrames);$('screen').dataset.backend=stats.backend;if(!failed)status.textContent='Đang dùng bộ giải mã H.265 phần mềm · không âm thanh';});
 seek.max=String(clip.duration);seek.disabled=false;button.disabled=false;status.textContent='Sẵn sàng. Bấm Phát để xem.';
 window.hevcReplayStats=()=>decoder.getStats();
}catch(e){fail(e);}
