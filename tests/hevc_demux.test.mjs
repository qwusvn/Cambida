import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {demuxHevc} from '../static/hevc-mp4.mjs';

test('malformed and unfinished MP4 fail closed',()=>{
  assert.throws(()=>demuxHevc(new ArrayBuffer(0)),/chưa hoàn tất/);
  const b=new Uint8Array(8);b[3]=30;
  assert.throws(()=>demuxHevc(b.buffer),/hỏng/);
});
test('real camera MP4 demuxes samples and keyframe parameter sets',()=>{
  const name=fs.readdirSync('cctv_videos').find(x=>x.endsWith('.mp4'));
  assert.ok(name,'existing camera sample required');
  const bytes=fs.readFileSync('cctv_videos/'+name);
  const clip=demuxHevc(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength));
  assert.ok(clip.duration>1&&clip.duration<120);
  assert.ok(clip.samples.length>10);
  const key=clip.samples.findIndex(x=>x.key);assert.ok(key>=0);
  assert.deepEqual([...clip.packet(key).slice(0,4)],[0,0,0,1]);
  assert.equal((clip.packet(key)[4]>>1)&63,32,'VPS included for reset/seek');
  assert.ok(clip.samples.every(x=>x.duration>0));
});
