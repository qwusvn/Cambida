// Minimal bounded ISO-BMFF demuxer for finalized camera HEVC MP4 files.
export function demuxHevc(buffer) {
  const b=new Uint8Array(buffer),v=new DataView(buffer),u=p=>v.getUint32(p),text=p=>String.fromCharCode(...b.subarray(p,p+4));
  function boxes(start,end){const out=[];for(let p=start;p+8<=end;){let size=u(p),head=8;if(size===1){size=u(p+8)*4294967296+u(p+12);head=16;}if(size===0)size=end-p;if(size<head||p+size>end)throw Error('MP4 bị hỏng.');out.push({type:text(p+4),start:p+head,end:p+size});p+=size;}return out;}
  const child=(box,name)=>boxes(box.start,box.end).find(x=>x.type===name);
  const moov=boxes(0,b.length).find(x=>x.type==='moov');if(!moov)throw Error('MP4 chưa hoàn tất.');
  for(const trak of boxes(moov.start,moov.end).filter(x=>x.type==='trak')){
    const mdia=child(trak,'mdia'),minf=mdia&&child(mdia,'minf'),stbl=minf&&child(minf,'stbl');if(!stbl)continue;
    const stsd=child(stbl,'stsd');if(!stsd)continue;
    const entry=boxes(stsd.start+8,stsd.end).find(x=>['hvc1','hev1'].includes(x.type));if(!entry)continue;
    const config=boxes(entry.start+78,entry.end).find(x=>x.type==='hvcC');if(!config)throw Error('Thiếu cấu hình HEVC.');
    let p=config.start+22;const arrays=b[p++],params=[];for(let i=0;i<arrays;i++){p++;const count=v.getUint16(p);p+=2;for(let j=0;j<count;j++){const n=v.getUint16(p);p+=2;params.push(b.slice(p,p+n));p+=n;}}
    const lengthSize=(b[config.start+21]&3)+1;
    const sz=child(stbl,'stsz'),co=child(stbl,'stco')||child(stbl,'co64'),sc=child(stbl,'stsc'),ts=child(stbl,'stts'),mdhd=child(mdia,'mdhd');
    if(!sz||!co||!sc||!ts||!mdhd)throw Error('Thiếu bảng mẫu MP4.');
    const count=u(sz.start+8);if(count>100000)throw Error('Clip quá dài.');
    const fixed=u(sz.start+4),sizes=Array.from({length:count},(_,i)=>fixed||u(sz.start+12+i*4));
    const scale=u(mdhd.start+(b[mdhd.start]===1?20:12));if(!scale)throw Error('Sai thang thời gian.');
    const durations=[];for(let i=0;i<u(ts.start+4);i++){const n=u(ts.start+8+i*8),d=u(ts.start+12+i*8)/scale;if(durations.length+n>count)throw Error('Sai bảng thời gian.');for(let j=0;j<n;j++)durations.push(d);}
    const rules=Array.from({length:u(sc.start+4)},(_,i)=>({first:u(sc.start+8+i*12),n:u(sc.start+12+i*12)}));
    const sync=child(stbl,'stss'),keys=sync?new Set(Array.from({length:u(sync.start+4)},(_,i)=>u(sync.start+8+i*4))):null;
    const ct=child(stbl,'ctts'),offsets=[];if(ct){for(let i=0;i<u(ct.start+4);i++){const n=u(ct.start+8+i*8),o=(b[ct.start]===1?v.getInt32(ct.start+12+i*8):u(ct.start+12+i*8))/scale;if(offsets.length+n>count)throw Error('Sai bảng PTS.');for(let j=0;j<n;j++)offsets.push(o);}}
    let index=0,dts=0,rule=0;const samples=[];
    for(let chunk=1;chunk<=u(co.start+4);chunk++){
      while(rule+1<rules.length&&rules[rule+1].first<=chunk)rule++;
      const cp=co.start+8+(chunk-1)*(co.type==='co64'?8:4);let pos=co.type==='co64'?u(cp)*4294967296+u(cp+4):u(cp);
      for(let j=0;j<rules[rule].n&&index<count;j++,index++){
        const size=sizes[index],duration=durations[index];if(pos+size>b.length||!duration)throw Error('Mẫu video không hợp lệ.');
        samples.push({pos,size,time:dts+(offsets[index]||0),duration,key:!keys||keys.has(index+1)});pos+=size;dts+=duration;
      }
    }
    if(index!==count)throw Error('MP4 thiếu mẫu video.');
    function packet(i){const s=samples[i],parts=s.key?[...params]:[];let p=s.pos;while(p<s.pos+s.size){let n=0;for(let j=0;j<lengthSize;j++)n=n*256+b[p++];if(!n||p+n>s.pos+s.size)throw Error('NAL không hợp lệ.');parts.push(b.subarray(p,p+n));p+=n;}const data=new Uint8Array(parts.reduce((a,x)=>a+x.length+4,0));let q=0;for(const x of parts){data.set([0,0,0,1],q);q+=4;data.set(x,q);q+=x.length;}return data;}
    return {samples,duration:dts,packet};
  }
  throw Error('Clip không chứa H.265. Hãy dùng trình phát thông thường.');
}
