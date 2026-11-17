import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

test('main replay defaults to WASM without compatibility UI or native preloading',()=>{
  const html=fs.readFileSync('index.html','utf8');
  assert.ok(!html.includes('id="replayCompatLink"'));
  assert.ok(!html.includes('id="replayDecoderStatus"'));
  assert.match(html,/player\.preload=nvr\?"metadata":"none"/);
  assert.match(html,/if\(!nvr\)Promise\.resolve\(\)\.then/);
  assert.ok(!html.includes('replayInlinePlay'),'removed overlay has no markup or dangling handlers');
});

test('replay routes H.264 to the native player and H.265 to the WASM decoder',()=>{
  const html=fs.readFileSync('index.html','utf8');
  assert.match(html,/import\('\/static\/mp4-codec\.mjs\?v=1'\)/);
  assert.ok(html.includes("if(info?.codec==='h264'){playReplayNative(player,autoplay,Boolean(info.audio),resumeAt);return;}"));
  assert.match(html,/enableHevc\(player,autoplay,resumeAt\)/);
  assert.match(html,/disposeHevc\(player\);[\s\S]{0,220}?playReplayNative\(player,autoplay,Boolean\(info\?\.audio\)\)/,'a non-HEVC clip still falls back to native playback');
});

test('live preview falls back to codec-agnostic MJPEG when WebRTC cannot render HEVC',()=>{
  const html=fs.readFileSync('index.html','utf8');
  assert.ok(html.includes('H265\\/90000'),'HEVC SDP payload is detected');
  assert.match(html,/watchFrames\(\)/);
  assert.match(html,/video\.videoWidth>0/);
  assert.match(html,/fallbackLiveMjpeg\(\)/);
});

test('inline adapter seeks, pauses, changes speed and restores native properties',async()=>{
  const frames=[],canvases=[];
  const clip={duration:60,samples:[{time:0,duration:1,key:true},{time:30,duration:1,key:true}],packet:()=>new Uint8Array([0,0,0,1])};
  let closed=0,reset=0;
  globalThis.__hevcTest={clip,create:async options=>({reset(){reset++;},close(){closed++;},getStats:()=>({decodeQueueSize:0}),pushAccessUnit(data,meta){frames.push(()=>options.onFrame({timestamp:meta.timestamp}));},flush:async()=>{}})};
  const source=fs.readFileSync('static/hevc-inline.mjs','utf8')
    .replace(/^import .*demuxHevc.*$/m,'const demuxHevc=()=>globalThis.__hevcTest.clip;')
    .replace(/^import .*createHevcPlayer.*$/m,'const createHevcPlayer=globalThis.__hevcTest.create,createRenderer=()=>({render(){},close(){}});');
  const isolatedSource=source.replace(/^import .*createReplayAudio.*$/m,'const createReplayAudio=()=>null;');
  const {attachHevc}=await import('data:text/javascript;base64,'+Buffer.from(isolatedSource).toString('base64'));
  const saved={document:globalThis.document,location:globalThis.location,fetch:globalThis.fetch};
  globalThis.location={origin:'http://test'};
  globalThis.document={hidden:false,createElement:()=>({style:{},dataset:{},setAttribute(){},remove(){canvases.pop();}}),addEventListener(){},removeEventListener(){}};
  globalThis.fetch=async()=>({ok:true,body:new ReadableStream({start(c){c.enqueue(new Uint8Array(8));c.close();}})});
  class Video extends EventTarget {constructor(){super();this.dataset={};this.hidden=false;this.playbackRate=1;this.currentTime=5;this.paused=true;}pause(){this.paused=true;}play(){this.paused=false;return Promise.resolve();}after(c){canvases.push(c);}}
  const video=new Video(),nativePlay=video.play;
  try{
    const adapter=attachHevc(video,{url:'/video/a.mp4',time:0,autoplay:false});
    await adapter.ready;
    assert.equal(video.readyState,4);assert.equal(canvases.length,1);
    frames.shift()();assert.equal(video.seeking,false);assert.equal(video.paused,true);
    video.currentTime=30;assert.equal(video.seeking,true);frames.shift()();
    assert.equal(video.currentTime,30);assert.equal(video.seeking,false);assert.ok(reset>=2);
    video.playbackRate=2;assert.equal(video.playbackRate,2);
    video.pause();assert.equal(video.paused,true);
    adapter.dispose();adapter.dispose();assert.equal(closed,1);assert.equal(canvases.length,0);
    assert.equal(video.play,nativePlay);assert.equal(video.currentTime,5);assert.equal(video._hevc,undefined);
  }finally{Object.assign(globalThis,saved);delete globalThis.__hevcTest;}
});

test('replay buffers accept clips up to 1 GiB and reject larger ones',()=>{
  const inline=fs.readFileSync('static/hevc-inline.mjs','utf8');
  const replay=fs.readFileSync('static/hevc-replay.js','utf8');
  for(const source of [inline,replay]){
    assert.match(source,/1024\*1024\*1024/,'a 1 GiB ceiling is enforced before decoding');
    assert.ok(!/\b32\*1024\*1024\b/.test(source),'the old 32 MiB ceiling is gone');
  }
  assert.match(inline,/vượt giới hạn 1 GB/);
  assert.match(replay,/vượt 1 GB/);
});

test('declared size preallocates the buffer and oversized clips are refused early',async()=>{
  const captured=[];
  const clip={duration:10,samples:[{time:0,duration:1,key:true}],packet:()=>new Uint8Array([0,0,0,1])};
  globalThis.__hevcTest={clip,captured,create:async()=>({reset(){},close(){},getStats:()=>({decodeQueueSize:0}),pushAccessUnit(){},flush:async()=>{}})};
  const source=fs.readFileSync('static/hevc-inline.mjs','utf8')
    .replace(/^import .*demuxHevc.*$/m,'const demuxHevc=buffer=>{globalThis.__hevcTest.captured.push(Array.from(new Uint8Array(buffer)));return globalThis.__hevcTest.clip;};')
    .replace(/^import .*createHevcPlayer.*$/m,'const createHevcPlayer=globalThis.__hevcTest.create,createRenderer=()=>({render(){},close(){}});')
    .replace(/^import .*createReplayAudio.*$/m,'const createReplayAudio=()=>null;');
  const {attachHevc}=await import('data:text/javascript;base64,'+Buffer.from(source+'\n').toString('base64'));
  const saved={document:globalThis.document,location:globalThis.location,fetch:globalThis.fetch};
  globalThis.location={origin:'http://test'};
  globalThis.document={hidden:false,createElement:()=>({style:{},dataset:{},setAttribute(){},remove(){}}),addEventListener(){},removeEventListener(){}};
  class Video extends EventTarget {constructor(){super();this.dataset={};this.hidden=false;this.playbackRate=1;this.currentTime=0;this.paused=true;}pause(){this.paused=true;}play(){this.paused=false;return Promise.resolve();}after(){}}
  const stream=()=>new ReadableStream({start(c){c.enqueue(new Uint8Array([1,2,3,4]));c.enqueue(new Uint8Array([5,6,7,8]));c.close();}});
  try{
    globalThis.fetch=async()=>({ok:true,headers:{get:name=>name==='content-length'?'8':null},body:stream()});
    const video=new Video(),adapter=attachHevc(video,{url:'/video/a.mp4'});
    await adapter.ready;
    assert.deepEqual(captured[0],[1,2,3,4,5,6,7,8],'declared length keeps every media byte intact');
    adapter.dispose();
    globalThis.fetch=async()=>({ok:true,headers:{get:name=>name==='content-length'?String(1024*1024*1024+1):null},body:stream()});
    let reported=null;
    const oversized=attachHevc(new Video(),{url:'/video/b.mp4',onError:error=>{reported=error;}});
    await oversized.ready;
    assert.equal(oversized.failed,true,'an oversized clip never reaches the decoder');
    assert.match(reported.message,/1 GB/);
    oversized.dispose();
  }finally{Object.assign(globalThis,saved);delete globalThis.__hevcTest;}
});
