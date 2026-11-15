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
