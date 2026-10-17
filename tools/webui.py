# -*- coding: utf-8 -*-
"""图层标注 WebUI：本地起 HTTP 服务，在浏览器里勾选差分图层、实时预览合成效果、
保存为 tools/characters/{角色}.json 的情绪映射配置。

预览走 build_cards._composite_with（与正式渲染同一套合成规则），所见即所得。

用法:
  py tools/webui.py Noah        # 直接指定角色
  py tools/build_cards.py webui Anan
"""
import argparse
import io
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_cards as bc  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PREVIEW_W = 760

_lock = threading.Lock()          # 合成 + 可见性变更必须串行
_cache = {}                       # (char, tuple(sorted(on))) -> png bytes


def list_psds():
    return sorted(p.stem for p in bc.PSD_DIR.glob("*.psd"))


def build_tree(char: str):
    psd = bc.open_psd({"psd": f"{char}.psd"})

    def node(layer):
        n = {"name": layer.name, "group": layer.is_group(),
             "visible": bool(layer.visible)}
        if layer.is_group():
            n["children"] = [node(c) for c in layer]
        return n

    return [node(l) for l in psd]


def load_config(char: str):
    path = bc.CONF_DIR / f"{char}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def render_preview(char: str, on_names) -> bytes:
    key = (char, tuple(sorted(on_names)))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    psd = bc.open_psd({"psd": f"{char}.psd"})
    w, h = psd.width, psd.height
    # 只合成上半身区域（与统一裁切标准一致），大幅提速
    img = bc._composite_with(psd, set(on_names), set(),
                             viewport=(0, 0, w, h // 2))
    img = img.resize((PREVIEW_W, round(img.height * PREVIEW_W / img.width)),
                     bc.Image.LANCZOS)
    bg = bc.Image.new("RGB", img.size, (34, 34, 38))
    bg.paste(img, (0, 0), img)
    buf = io.BytesIO()
    bg.save(buf, "PNG")
    data = buf.getvalue()
    if len(_cache) > 80:
        _cache.clear()
    _cache[key] = data
    return data


def save_config(char: str, emotions: dict):
    bc.CONF_DIR.mkdir(parents=True, exist_ok=True)
    path = bc.CONF_DIR / f"{char}.json"
    conf = load_config(char) or {}
    conf.setdefault("psd", f"{char}.psd")
    emos = conf.setdefault("emotions", {})
    for emo, on_list in emotions.items():
        entry = emos.get(emo, {})
        entry["on"] = list(on_list)
        emos[emo] = entry          # 保留既有 off 等附加字段
    path.write_text(json.dumps(conf, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return str(path)


PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>DialoCard 差分标注</title>
<style>
 body{margin:0;font:14px/1.5 "Segoe UI",sans-serif;background:#1e1f24;color:#ddd;
      display:flex;flex-direction:column;height:100vh}
 header{padding:8px 14px;background:#26272e;display:flex;gap:14px;align-items:center;
        flex-wrap:wrap;border-bottom:1px solid #000}
 header b{color:#fff}
 select,button{background:#3a3b44;color:#eee;border:1px solid #555;border-radius:4px;
        padding:4px 10px;cursor:pointer}
 button:hover{background:#4a4b56}
 #tabs{display:flex;gap:4px;flex-wrap:wrap}
 #tabs button.on{background:#c46a9d;border-color:#c46a9d;color:#fff}
 main{flex:1;display:flex;min-height:0}
 #left{flex:1;display:flex;align-items:center;justify-content:center;position:relative;
       background:#17181c;overflow:hidden}
 #preview{max-width:100%;max-height:100%}
 #spin{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
       background:rgba(0,0,0,.45);color:#fff;font-size:16px}
 #panel{width:420px;overflow:auto;background:#222329;border-left:1px solid #000;
        padding:10px 14px 40px}
 .grp{margin-top:4px}
 .grp>summary{cursor:pointer;font-weight:600;color:#9ecbff}
 .row{display:flex;align-items:center;gap:6px;padding:1px 0 1px 18px}
 .row.g0{padding-left:0}
 input[type=checkbox]{accent-color:#c46a9d}
 .dis{color:#777;text-decoration:line-through}
 #onlist{font-size:12px;color:#8fd48f;word-break:break-all;padding:4px 2px}
 #status{font-size:12px;color:#aaa}
</style></head><body>
<header>
 <b>DialoCard 差分标注</b>
 角色 <select id="charsel"></select>
 <div id="tabs"></div>
 <label><input type="checkbox" id="auto" checked> 自动预览</label>
 <button id="refresh">刷新预览</button>
 <button id="save">保存配置</button>
 <span id="status"></span>
</header>
<main>
 <div id="left"><img id="preview"><div id="spin">合成中，约 10 秒…</div></div>
 <div id="panel">
   <div>当前情绪 <b id="emoName"></b> 已选层：</div>
   <div id="onlist"></div>
   <div id="tree"></div>
 </div>
</main>
<script>
const EMO=["default","wronged","surprise","smile","paint","flushed",
           "embarrass","cry","confused","angry","afraid"];
let char=null, tree=[], state={}, current="default", timer=null, ctrl=null;
const $=id=>document.getElementById(id);

function groupKey(name){
  const p=name.split("_");
  if(p[0]==="Effect"&&p.length>1) return p.slice(0,2).join("_");
  if(["Pale","Sweat","Shadow","Option"].includes(p[0])) return null;
  return p[0];
}
function setStatus(t){$("status").textContent=t;}

async function init(){
  const psds=await (await fetch("/psds")).json();
  const sel=$("charsel");
  for(const p of psds){const o=document.createElement("option");o.value=p;o.textContent=p;sel.appendChild(o);}
  const want=new URLSearchParams(location.search).get("char");
  sel.value=psds.includes(want)?want:psds[0];
  sel.onchange=()=>loadChar(sel.value);
  $("refresh").onclick=()=>refresh(true);
  $("save").onclick=save;
  renderTabs();
  await loadChar(sel.value);
}
function renderTabs(){
  const t=$("tabs");t.innerHTML="";
  for(const e of EMO){
    const b=document.createElement("button");
    b.textContent=e+(state[e]&&state[e].size?" ("+state[e].size+")":"");
    if(e===current)b.classList.add("on");
    b.onclick=()=>{current=e;renderTabs();renderTree();schedule(0);};
    t.appendChild(b);
  }
}
async function loadChar(c){
  char=c;
  const [tr,cfg]=await Promise.all([
    fetch("/tree?char="+c).then(r=>r.json()),
    fetch("/config?char="+c).then(r=>r.json())]);
  tree=tr;state={};current="default";
  const emos=cfg.emotions||{};
  for(const e of EMO)state[e]=new Set(emos[e]&&emos[e].on||[]);
  renderTabs();renderTree();schedule(0);
}
function renderTree(){
  const root=$("tree");root.innerHTML="";
  const mk=(nodes,parent,depth)=>{
    for(const n of nodes){
      if(n.group){
        const d=document.createElement("details");d.open=depth<2;d.className="grp";
        const s=document.createElement("summary");
        s.textContent=(n.visible?"":"○ ")+n.name+" [组]";
        d.appendChild(s);
        const box=document.createElement("div");
        d.appendChild(box);parent.appendChild(d);
        mk(n.children,box,depth+1);
      }else{
        const row=document.createElement("div");row.className="row"+(depth?"":" g0");
        const cb=document.createElement("input");cb.type="checkbox";
        cb.dataset.name=n.name;
        if(state[current]&&state[current].has(n.name))cb.checked=true;
        if(n.name.startsWith("Pale")){cb.disabled=true;cb.title="fill-opacity 不被支持，自动排除";cb.checked=false;}
        cb.onchange=()=>onCheck(n.name,cb.checked);
        const lb=document.createElement("label");
        lb.textContent=n.name+(n.visible?"":" *");
        if(n.name.startsWith("Pale"))lb.className="dis";
        lb.onclick=()=>{if(!cb.disabled)cb.click();};
        row.appendChild(cb);row.appendChild(lb);parent.appendChild(row);
      }
    }
  };
  mk(tree,root,0);
  $("emoName").textContent=current;
  $("onlist").textContent=[...(state[current]||[])].join(", ")||"（空 = 保持 PSD 原状）";
}
function onCheck(name,ck){
  const set=state[current];
  if(ck){
    const key=groupKey(name);
    if(key){ // 同互斥组的其他已选项自动取消
      for(const other of [...set]){
        if(other!==name&&groupKey(other)===key){set.delete(other);}
      }
    }
    set.add(name);
  }else set.delete(name);
  renderTree();schedule();
}
function schedule(delay){
  if(timer)clearTimeout(timer);
  timer=setTimeout(()=>{if($("auto").checked)refresh();},delay===0?0:900);
}
async function refresh(force){
  if(ctrl)ctrl.abort();
  ctrl=new AbortController();
  $("spin").style.display="flex";
  const t0=Date.now();
  try{
    const on=[...(state[current]||[])].join(",");
    const r=await fetch("/preview?char="+char+"&on="+encodeURIComponent(on),
                        {signal:ctrl.signal});
    if(!r.ok)throw new Error(await r.text());
    $("preview").src=URL.createObjectURL(await r.blob());
    setStatus("预览完成 "+((Date.now()-t0)/1000).toFixed(1)+"s");
  }catch(e){
    if(e.name!=="AbortError")setStatus("出错: "+e.message);
  }finally{$("spin").style.display="none";}
}
async function save(){
  const body={char,emotions:{}};
  for(const e of EMO)body.emotions[e]=[...(state[e]||[])];
  const r=await fetch("/save",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(r.ok){setStatus("已保存 -> "+await r.text());}
  else setStatus("保存失败: "+await r.text());
}
init();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/psds":
                self._json(list_psds())
            elif url.path == "/tree":
                self._json(build_tree(q["char"][0]))
            elif url.path == "/config":
                self._json(load_config(q["char"][0]))
            elif url.path == "/preview":
                with _lock:
                    data = render_preview(q["char"][0],
                                          q.get("on", [""])[0].split(","))
                self._send(200, data, "image/png")
            else:
                self._send(404, b"not found")
        except Exception as e:  # noqa: BLE001
            self._send(500, f"{type(e).__name__}: {e}".encode("utf-8"))

    def do_POST(self):
        if urlparse(self.path).path != "/save":
            self._send(404, b"not found")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            with _lock:
                path = save_config(body["char"], body["emotions"])
            self._send(200, path.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self._send(500, f"{type(e).__name__}: {e}".encode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("char", nargs="?", default=None)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    if not list_psds():
        sys.exit(f"{bc.PSD_DIR} 下没有 PSD")
    url = f"http://{HOST}:{args.port}/"
    if args.char:
        url += f"?char={args.char}"
    print(f"标注 WebUI -> {url}  (Ctrl+C 退出)", flush=True)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    HTTPServer((HOST, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
