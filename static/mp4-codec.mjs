// Tiny ISO-BMFF codec probe used before choosing a replay decoder. It reads a
// bounded prefix/tail with Range requests, so an H.264 clip can play with the
// native <video> element while H.265 keeps using the WASM decoder.
const WINDOW=1024*1024;
const cache=new Map();

function fourcc(bytes,p){return String.fromCharCode(bytes[p],bytes[p+1],bytes[p+2],bytes[p+3]);}

export function mp4CodecFromBytes(bytes){
  return mp4TracksFromBytes(bytes)?.codec||null;
}

// Returns {codec, audio} where codec is 'h264'|'h265'|'av1'|null and audio says
// whether the clip has a sound track. Null when no movie header is present.
export function mp4TracksFromBytes(bytes){
  const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
  const text=p=>fourcc(bytes,p);
  function boxes(start,end){
    const out=[];
    for(let p=start;p+8<=end;){
      let size=view.getUint32(p),head=8;
      if(size===1){if(p+16>end)break;size=view.getUint32(p+8)*4294967296+view.getUint32(p+12);head=16;}
      if(size===0)size=end-p;
      if(size<head||p+size>end)break;
      out.push({type:text(p+4),start:p+head,end:p+size});
      p+=size;
    }
    return out;
  }
  const child=(box,name)=>boxes(box.start,box.end).find(x=>x.type===name);
  function readMoov(moov){
    let codec=null,audio=false;
    for(const trak of boxes(moov.start,moov.end).filter(x=>x.type==='trak')){
      const mdia=child(trak,'mdia');
      const hdlr=mdia&&child(mdia,'hdlr');
      const handler=hdlr&&hdlr.start+12<=hdlr.end?text(hdlr.start+8):'';
      if(handler==='soun'){audio=true;continue;}
      if(handler&&handler!=='vide')continue;
      const minf=mdia&&child(mdia,'minf');
      const stbl=minf&&child(minf,'stbl');
      const stsd=stbl&&child(stbl,'stsd');
      if(!stsd||codec)continue;
      for(const entry of boxes(stsd.start+8,stsd.end)){
        if(entry.type==='hvc1'||entry.type==='hev1'){codec='h265';break;}
        if(entry.type==='avc1'||entry.type==='avc3'){codec='h264';break;}
        if(entry.type==='av01'){codec='av1';break;}
      }
    }
    return {codec,audio};
  }
  const moov=boxes(0,bytes.length).find(x=>x.type==='moov');
  if(moov)return readMoov(moov);
  // moov at the end (no faststart): find a box whose size ends exactly at EOF.
  for(let p=bytes.length-8;p>=0;p--){
    if(text(p+4)!=='moov')continue;
    const size=view.getUint32(p);
    if(size<8||p+size!==bytes.length)continue;
    const info=readMoov({start:p+8,end:bytes.length});
    if(info.codec||info.audio)return info;
  }
  return null;
}

async function readRange(fetchImpl,url,range,signal){
  const response=await fetchImpl(url,{headers:{Range:range},credentials:'same-origin',signal});
  if(!response.ok&&response.status!==206)throw Error('Không đọc được thông tin video ('+response.status+').');
  return {bytes:new Uint8Array(await response.arrayBuffer()),headers:response.headers};
}

// Returns {codec, audio} or null when the movie header is unavailable.
export async function probeMp4Info(url,{fetchImpl=fetch,signal}={}){
  const cached=cache.get(url);
  if(cached!==undefined)return cached;
  const head=await readRange(fetchImpl,url,'bytes=0-'+(WINDOW-1),signal);
  let info=mp4TracksFromBytes(head.bytes);
  if(!info){
    const range=(head.headers?.get('content-range')||'').match(/\/(\d+)$/);
    const total=range?Number(range[1]):Number(head.headers?.get('content-length'))||0;
    if(total>head.bytes.length){
      const start=Math.max(0,total-WINDOW);
      const tail=await readRange(fetchImpl,url,'bytes='+start+'-'+(total-1),signal);
      info=mp4TracksFromBytes(tail.bytes);
    }
  }
  if(info)cache.set(url,info);
  return info;
}

// Backwards-compatible codec-only probe used by the compatibility page.
export async function probeMp4Codec(url,options={}){
  const info=await probeMp4Info(url,options);
  return info?.codec||null;
}
