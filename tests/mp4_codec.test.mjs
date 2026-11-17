import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {mp4CodecFromBytes,probeMp4Codec} from '../static/mp4-codec.mjs';

const ffmpeg=path.resolve('ffmpeg.exe');

function fixture(container,name='fixture.mp4',input='testsrc=size=64x64:rate=5',duration='1'){
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'cambida-codec-'));
  const file=path.join(dir,name);
  execFileSync(ffmpeg,['-y','-hide_banner','-loglevel','error','-f','lavfi','-i',input,'-t',duration,...container,file]);
  return {dir,file,bytes:new Uint8Array(fs.readFileSync(file))};
}

// Serve byte ranges like Flask send_file so the probe can be checked offline.
function rangeFetch(bytes){
  const calls=[];
  const impl=async(url,{headers}={})=>{
    calls.push({url,range:headers.Range});
    const m=String(headers?.Range||'').match(/^bytes=(\d+)-(\d+)$/);
    const start=m?Number(m[1]):0,end=m?Number(m[2]):bytes.length-1;
    const body=bytes.slice(start,end+1);
    return {ok:true,status:206,headers:{get:name=>{
      if(name==='content-range')return `bytes ${start}-${end}/${bytes.length}`;
      if(name==='content-length')return String(body.length);
      return null;
    }},arrayBuffer:async()=>body.buffer.slice(body.byteOffset,body.byteOffset+body.byteLength)};
  };
  return {impl,calls};
}

test('detects HEVC and H.264 sample entries in real MP4 files',()=>{
  const hevc=fs.readFileSync('cctv_videos/'+fs.readdirSync('cctv_videos').find(name=>name.endsWith('.mp4')));
  assert.equal(mp4CodecFromBytes(new Uint8Array(hevc)),'h265');
  const h264=fixture(['-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart']);
  try{assert.equal(mp4CodecFromBytes(h264.bytes),'h264');}
  finally{fs.rmSync(h264.dir,{recursive:true,force:true});}
});

test('range probe finds a tail moov without faststart and stays bounded',async()=>{
  const h264=fixture(['-c:v','libx264','-preset','ultrafast','-crf','16'],'large.mp4','testsrc2=size=1280x720:rate=25','4');
  try{
    assert.ok(h264.bytes.length>1024*1024,'fixture is larger than the probe window');
    const {impl,calls}=rangeFetch(h264.bytes);
    assert.equal(await probeMp4Codec('/video/tail.mp4',{fetchImpl:impl}),'h264');
    const used=calls.length;
    assert.equal(used,2,'head window misses moov, tail window finds it');
    assert.ok(calls.every(call=>/^bytes=\d+-\d+$/.test(call.range||'')),'only ranged reads');
    assert.equal(await probeMp4Codec('/video/tail.mp4',{fetchImpl:impl}),'h264');
    assert.equal(calls.length,used,'second probe uses the in-memory cache');
  }finally{fs.rmSync(h264.dir,{recursive:true,force:true});}
});

test('unknown or malformed input returns null so the default decoder is kept',()=>{
  assert.equal(mp4CodecFromBytes(new Uint8Array(0)),null);
  assert.equal(mp4CodecFromBytes(new Uint8Array(64)),null);
});
