#!/usr/bin/env python3
"""Offline lab: ways to lose picture quality for fewer bytes, at equal size.

Compares, on recorded footage and against the ORIGINAL colour picture (PSNR of
what the panel would show):

  lossless          the 3-3-2 index frame as it is
  ladder target N   server/frames.py encode_within: coarser colour steps until N fits
  rdo snap T        per pixel, keep the panel's old value or the left neighbour
                    when it is within T (weighted squared RGB error) of the true
                    colour, so deflate sees more repeats; T is a continuous knob
  denoise           the same with ffmpeg hqdn3d in front

Needs numpy, unlike server/. Run from the repo root with PYTHONPATH=.

    python tools/quality_lab.py /path/CCTV13.ts
"""
import sys, subprocess, zlib
sys.path.insert(0,'tools')
import numpy as np, budget_lab as L
from server import frames
W,H=frames.WIDTH,frames.HEIGHT
PAL=L.PAL.astype(np.float64); WT=np.array([3.,6.,1.])
def read(src,n,fmt,extra=""):
    pf="rgb24" if fmt=="rgb24" else "rgb8"
    cmd=["ffmpeg","-v","error","-ss","2","-i",src,"-vf",f"fps=25,{frames.FIT},{extra}format={pf}","-frames:v",str(n),"-pix_fmt",pf,"-sws_dither","none","-f","rawvideo","-"]
    raw=subprocess.run(cmd,capture_output=True,check=True).stdout
    a=np.frombuffer(raw,np.uint8); k=3 if fmt=="rgb24" else 1
    return a.reshape(-1,H,W,3) if k==3 else a.reshape(-1,H,W)
def D(src,idx): return (((src-PAL[idx])**2)*WT).sum(-1)
def snap(src,i0,shown,T):
    out=i0.copy(); d0=D(src,i0)
    if shown is not None:
        sh=np.frombuffer(shown,np.uint8).reshape(H,W); m=D(src,sh)<=d0+T; out[m]=sh[m]
    if T>0:
        for x in range(1,W):
            l=out[:,x-1]; dl=D(src[:,x],l); dc=D(src[:,x],out[:,x])
            m=(dl<=d0[:,x]+T)&(out[:,x]!=l)&(dc>=0)
            out[m,x]=l[m]
    return out
def psnr(shown,rgb):
    e=((PAL[np.frombuffer(shown,np.uint8)].reshape(H,W,3)-rgb)**2).mean(); return 10*np.log10(255**2/e)
def run(rgbs,idxs,mode,param):
    shown=None; sizes=[]; ps=[]
    for t,(rgb,i0) in enumerate(zip(rgbs,idxs)):
        rgb=rgb.astype(np.float64)
        if mode=="ladder":
            raw=i0.tobytes(); ch,drawn,_=frames.encode_within(raw,shown,t,param)
        else:
            raw=snap(rgb,i0,shown if mode=="rdo" else None,param).astype(np.uint8).tobytes()
            drawn=raw; ch=frames.choose_stripes(raw,shown,t,1<<30,0.01)
        shown=frames.apply_stripes(drawn,shown,ch); sizes.append(frames._wire_size(ch)); ps.append(psnr(shown,rgb))
    return np.mean(sizes),np.mean(ps),np.percentile(sizes,95)
src,n=sys.argv[1],80
rgbs=read(src,n,"rgb24"); idx=read(src,n,"rgb8")
print(src.split('/')[-1])
def row(name,r): print(f"  {name:26s} {r[0]:7.0f} B  p95 {r[2]:6.0f}  PSNR {r[1]:5.2f}")
row("lossless (current)",run(rgbs,idx,"ladder",1<<30))
for tg in (16000,12000,9000): row(f"ladder target {tg}",run(rgbs,idx,"ladder",tg))
for T in (1500,4000,8000,16000): row(f"rdo snap T={T}",run(rgbs,idx,"rdo",T))
dn=read(src,n,"rgb8","hqdn3d=3:2:6:4,")
row("denoise, lossless",run(rgbs,dn,"ladder",1<<30))
for T in (4000,8000): row(f"denoise + rdo T={T}",run(rgbs,dn,"rdo",T))
