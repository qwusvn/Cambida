import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {audioOnlyMp4,createReplayAudio} from '../static/hevc-audio.mjs';

test('audio MP4 preserves offsets and media bytes but hides HEVC track',async()=>{
  const fixture=fs.readdirSync('cctv_videos').find(x=>x.endsWith('.mp4'));
  const bytes=fs.readFileSync('cctv_videos/'+fixture);
  const buffer=bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength);
  const blob=audioOnlyMp4(buffer);assert.ok(blob);
  const out=new Uint8Array(await blob.arrayBuffer());
  assert.equal(out.length,bytes.length);
  let changed=0;for(let i=0;i<out.length;i++)if(out[i]!==bytes[i])changed++;
  assert.equal(changed,3,'trak → free changes t/a/k; the r byte stays identical');
  assert.deepEqual(new Uint8Array(buffer),new Uint8Array(bytes),'original remains unchanged');
});

test('missing audio returns null; malformed boxes fail safely',()=>{
  assert.equal(audioOnlyMp4(new ArrayBuffer(0)),null);
  const fixture=fs.readdirSync('cctv_videos').find(x=>x.endsWith('.mp4'));
  const data=fs.readFileSync('cctv_videos/'+fixture);
  const sound=data.indexOf(Buffer.from('soun'));assert.ok(sound>=0);
  data.write('vide',sound);
  assert.equal(audioOnlyMp4(data.buffer.slice(data.byteOffset,data.byteOffset+data.byteLength)),null);
  const bytes=new Uint8Array(8);bytes[3]=30;
  assert.throws(()=>audioOnlyMp4(bytes.buffer),/MP4/);
});

test('audio blocked policy, rate/seek, pause and URL disposal',async()=>{
  const fixture=fs.readdirSync('cctv_videos').find(x=>x.endsWith('.mp4'));
  const bytes=fs.readFileSync('cctv_videos/'+fixture);
  const buffer=bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength);
  const saved=globalThis.Audio;let plays=0,pauses=0,loads=0,blocked=0;
  globalThis.Audio=class extends EventTarget{
    readyState=1;currentTime=0;playbackRate=1;
    play(){plays++;return Promise.reject(Object.assign(Error('blocked'),{name:'NotAllowedError'}));}
    pause(){pauses++;}removeAttribute(){}load(){loads++;}
  };
  try{
    const audio=createReplayAudio(buffer,{onBlocked:()=>blocked++});
    audio.sync(10,2);assert.equal(audio.element.currentTime,10);assert.equal(audio.element.playbackRate,2);
    await audio.play(20,.5);assert.equal(blocked,1);assert.equal(plays,1);
    audio.pause();audio.dispose();audio.dispose();assert.ok(pauses>=2);assert.equal(loads,1);
    await audio.play(0,1);assert.equal(plays,1);
  }finally{globalThis.Audio=saved;}
});
