// Keep original MP4 offsets/timing/edit lists, but hide video tracks from the
// native audio demuxer. No transcoding, no second network request, AAC decoded
// by the browser even when its HEVC decoder is unavailable.
export function audioOnlyMp4(buffer) {
  const view=new DataView(buffer),bytes=new Uint8Array(buffer);
  const text=p=>String.fromCharCode(...bytes.subarray(p,p+4));
  function boxes(start,end){
    const result=[];
    for(let p=start;p+8<=end;){
      let size=view.getUint32(p),header=8;
      if(size===1){if(p+16>end)throw Error('MP4 không hợp lệ.');size=view.getUint32(p+8)*4294967296+view.getUint32(p+12);header=16;}
      if(size===0)size=end-p;
      if(size<header||p+size>end||!Number.isSafeInteger(size))throw Error('MP4 không hợp lệ.');
      result.push({offset:p,start:p+header,end:p+size,type:text(p+4)});p+=size;
    }
    return result;
  }
  const moov=boxes(0,bytes.length).find(b=>b.type==='moov');if(!moov)return null;
  const tracks=boxes(moov.start,moov.end).filter(b=>b.type==='trak');
  const kinds=tracks.map(track=>{
    const mdia=boxes(track.start,track.end).find(b=>b.type==='mdia');
    const h=mdia&&boxes(mdia.start,mdia.end).find(b=>b.type==='hdlr');
    return {track,kind:h&&h.start+12<=h.end?text(h.start+8):''};
  });
  if(!kinds.some(t=>t.kind==='soun'))return null;
  const output=bytes.slice();
  for(const {track,kind} of kinds)if(kind!=='soun')output.set([102,114,101,101],track.offset+4); // free
  return new Blob([output],{type:'audio/mp4'});
}

export function createReplayAudio(buffer,{onBlocked=()=>{},onUnavailable=()=>{}}={}) {
  const blob=audioOnlyMp4(buffer);if(!blob)return null;
  const url=URL.createObjectURL(blob),audio=new Audio();
  audio.preload='metadata';audio.src=url;
  let disposed=false,epoch=0,wantedTime=0,wantedRate=1;
  audio.addEventListener('error',()=>{if(!disposed)onUnavailable();});
  function align(){if(audio.readyState>=1){audio.playbackRate=wantedRate;if(Math.abs(audio.currentTime-wantedTime)>.25)audio.currentTime=wantedTime;}}
  audio.addEventListener('loadedmetadata',align);
  return {
    element:audio,
    pause(){epoch++;audio.pause();},
    sync(time,rate){wantedTime=time;wantedRate=rate;align();},
    async play(time,rate){
      if(disposed)return;wantedTime=time;wantedRate=rate;align();const token=++epoch;
      try{await audio.play();if(disposed)audio.pause();}
      catch(error){if(!disposed&&token===epoch&&error.name!=='AbortError'){if(error.name==='NotAllowedError')onBlocked();else onUnavailable();}}
    },
    dispose(){if(disposed)return;disposed=true;epoch++;audio.pause();audio.removeAttribute('src');audio.load();URL.revokeObjectURL(url);}
  };
}
