import {demuxHevc} from './hevc-mp4.mjs';
import {createHevcPlayer, createRenderer} from './vendor/libde265-web-0.1.4/dist/esm/index.js';
import {createReplayAudio} from './hevc-audio.mjs';

// Adapt the existing media element API so timeline, speed and cut controls
// have one clock regardless of the selected decoder. Native APIs are restored
// before a different source or live stream is selected.
export function attachHevc(video, {url, time=0, autoplay=false, onError=()=>{},onAudioState=()=>{}}) {
  const nativePause=video.pause.bind(video);
  nativePause();
  const canvas=document.createElement('canvas');
  canvas.className='hevc-inline-canvas';
  canvas.setAttribute('aria-label','Video xem lại H.265');
  canvas.style.cssText='position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000;pointer-events:none';
  video.after(canvas);
  const abort=new AbortController(), descriptors=new Map();
  let decoder,renderer,clip,index=0,closed=false,paused=!autoplay,ready=0,current=0,rate=video.playbackRate||1;
  let timer,clock=0,origin=0,target=Math.max(0,time),seeking=true,ending=false,epoch=0,failed=false;
  let sound=null,audioEnabled=false,audioPlaying=false;
  function pauseAudio(){audioPlaying=false;sound?.pause();}
  function syncAudio(){
    if(!sound||!audioEnabled||paused||seeking)return;
    if(!audioPlaying){audioPlaying=true;sound.play(current,rate);}
    else sound.sync(current,rate);
  }
  const emit=type=>video.dispatchEvent(new Event(type));
  const originalHidden=video.hidden;
  const stop=()=>{clearTimeout(timer);timer=null;};
  function fail(error){if(closed||error.name==='AbortError')return;failed=true;paused=true;stop();pauseAudio();onError(error);}
  function pause(){paused=true;stop();pauseAudio();emit('pause');}
  function play(){if(closed||failed)return Promise.reject(Error('Bộ giải mã không sẵn sàng.'));stop();if(clip&&index>=clip.samples.length)seek(0);paused=false;clock=performance.now();origin=current;emit('play');tick();return Promise.resolve();}
  function seek(value){
    target=Math.max(0,Math.min(Number(value)||0,clip?.duration||Number(value)||0));
    current=target;seeking=true;epoch++;ending=false;stop();pauseAudio();
    if(!clip)return;
    decoder.reset();index=0;
    for(let i=0;i<clip.samples.length;i++)if(clip.samples[i].key&&clip.samples[i].time<=target)index=i;
    clock=performance.now();origin=target;emit('seeking');tick();
  }
  function tick(){
    if(closed||failed||!decoder||(!seeking&&paused)||ending)return;
    try{
      const sample=clip.samples[index];
      if(!sample){ending=true;const generation=epoch;decoder.flush().then(()=>{
        if(closed||generation!==epoch)return;
        ending=false;seeking=false;current=clip.duration;paused=true;pauseAudio();emit('timeupdate');emit('ended');
      }).catch(fail);return;}
      if(decoder.getStats().decodeQueueSize>2){timer=setTimeout(tick,10);return;}
      const delay=seeking?0:(sample.time-origin)*1000/rate-(performance.now()-clock);
      if(delay>2){timer=setTimeout(tick,Math.min(delay,100));return;}
      decoder.pushAccessUnit(clip.packet(index++),{timestamp:Math.round(sample.time*1e6),duration:Math.round(sample.duration*1e6),key:sample.key});
      timer=setTimeout(tick,0);
    }catch(error){fail(error);}
  }
  const properties={
    currentTime:{get:()=>current,set:seek},duration:{get:()=>clip?.duration||NaN},
    paused:{get:()=>paused},ended:{get:()=>!!clip&&current>=clip.duration},
    seeking:{get:()=>seeking},readyState:{get:()=>ready},
    playbackRate:{get:()=>rate,set:value=>{if(Number(value)>0){rate=Number(value);clock=performance.now();origin=current;sound?.sync(current,rate);emit('ratechange');}}},
    play:{value:play},pause:{value:pause}
  };
  function dispose(){
    if(closed)return;closed=true;abort.abort();stop();sound?.dispose();decoder?.close();renderer?.close?.();canvas.remove();onAudioState('none');
    document.removeEventListener('visibilitychange',visibility);
    for(const [name,descriptor] of descriptors){if(descriptor)Object.defineProperty(video,name,descriptor);else delete video[name];}
    video.hidden=originalHidden;delete video.dataset.hevc;delete video._hevc;
  }
  const controller={dispose,stats:()=>({...decoder?.getStats(),audio:sound?{enabled:audioEnabled,time:sound.element.currentTime,paused:sound.element.paused,ready:sound.element.readyState,error:sound.element.error?.code||null}:null}),canvas,get failed(){return failed;},
    setAudio(enabled){audioEnabled=!!enabled;if(!audioEnabled){pauseAudio();onAudioState('muted');return;}
      if(!sound)return;
      // Called directly by the user's tap, before yielding Safari activation.
      audioPlaying=true;sound.play(current,rate);onAudioState('on');
      if(paused||seeking)pauseAudio();
    }};
  for(const [name,descriptor] of Object.entries(properties)){
    descriptors.set(name,Object.getOwnPropertyDescriptor(video,name));
    Object.defineProperty(video,name,{configurable:true,...descriptor});
  }
  video._hevc=controller;video.dataset.hevc='loading';video.hidden=true;
  const visibility=()=>{if(document.hidden)pause();};
  document.addEventListener('visibilitychange',visibility);
  controller.ready=(async()=>{
    const source=new URL(url,location.origin);
    if(source.origin!==location.origin||!source.pathname.startsWith('/video/')||source.search||source.hash)throw Error('Chỉ hỗ trợ bản ghi MP4 nội bộ.');
    const response=await fetch(source,{credentials:'same-origin',signal:abort.signal});
    if(!response.ok)throw Error('Không tải được video ('+response.status+').');
    const LIMIT=1024*1024*1024,expected=Number(response.headers?.get('content-length'))||0;
    if(expected>LIMIT)throw Error('Clip vượt giới hạn 1 GB.');
    const reader=response.body.getReader(),chunks=[];
    let size=0,bytes=expected>0?new Uint8Array(expected):null;
    try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>LIMIT)throw Error('Clip vượt giới hạn 1 GB.');
      if(bytes&&size<=bytes.length)bytes.set(value,size-value.length);
      else{if(bytes){chunks.push(bytes.subarray(0,size-value.length));bytes=null;}chunks.push(value);}}}
    catch(error){await reader.cancel().catch(()=>{});throw error;}
    if(!bytes){bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}}
    else if(bytes.length!==size)bytes=bytes.slice(0,size);
    clip=demuxHevc(bytes.buffer);
    sound=createReplayAudio(bytes.buffer,{onBlocked:()=>{audioPlaying=false;audioEnabled=false;onAudioState('blocked');},
      onUnavailable:()=>{audioEnabled=false;pauseAudio();onAudioState('unavailable');}});
    onAudioState(sound?'muted':'none');
    renderer=createRenderer(canvas,'auto');
    const created=await createHevcPlayer({mode:'wasm',render:false,wasmWorker:'required',lowLatency:false,
      wasmBaseUrl:'/static/vendor/libde265-web-0.1.4/dist/wasm/',onError:fail,
      onFrame:frame=>{
        if(closed||frame.timestamp/1e6+0.001<target)return;
        if(paused&&!seeking)return;
        renderer.render(frame);current=frame.timestamp/1e6;
        canvas.dataset.frames=String(Number(canvas.dataset.frames||0)+1);
        if(seeking){seeking=false;clock=performance.now();origin=current;emit('seeked');}
        video.dataset.hevc='wasm';syncAudio();emit('timeupdate');
      }});
    if(closed){created.close();return;}
    decoder=created;ready=4;seek(target);emit('loadedmetadata');emit('canplay');
  })().catch(fail);
  return controller;
}
