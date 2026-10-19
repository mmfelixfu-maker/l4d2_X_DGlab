"""Generate SDK manifest and a standalone interactive settings preview."""
import json
from pathlib import Path

from model import EVENTS, defaults


def main():
    root = Path(__file__).resolve().parent
    cfg = defaults()
    def field(key, kind, label, **extra):
        return {"key": key, "type": kind, "label": label, "default": cfg[key], **extra}
    sections = [
        {"section": "🔌 采集与运行", "fields": [
            field("observe_only", "bool", "仅观察状态", description="关闭后才向设备输出；开启时只更新状态卡片。"),
            field("profile_path", "path", "游戏地址配置", description="相对插件目录或绝对路径；版本指纹不符时暂停读取。"),
            field("poll_hz", "number", "轮询频率（Hz）", min=10, max=500, step=10,
                  description="默认 120Hz 观察玩家 tick，无法保证捕获全部服务器 tick。"),
            field("stale_s", "duration", "数据过期时间", min=0.2, max=10, step=0.1),
            field("temp_decay", "number", "临时血量每秒衰减", min=0, max=10, step=0.01,
                  description="标准规则通常为 0.27；修改规则的服务器应按实际值设置。"),
            field("status_hz", "number", "状态卡片刷新频率", min=0.2, max=10, step=0.2),
        ]},
        {"section": "⚡ 生命值 → 强度层", "fields": [
            field("mode", "select", "强度模式", options=[
                {"value": "dynamic", "label": "动态：随当前损失生命值"},
                {"value": "fixed", "label": "固定：保持固定强度"},
                {"value": "challenge", "label": "挑战：累计本次会话损失生命值"},
            ]),
            field("hp_scale", "number", "每损失 1 HP 对应强度百分比", min=0, max=10, step=0.1,
                  description="动态：满血为 0，受伤升高，治疗降低；倒地/挂边时使用上限。"),
            field("strength_cap", "percent", "本插件强度上限"),
            field("fixed_strength", "percent", "固定模式强度"),
            field("channel", "channel", "基础强度通道"),
        ]},
    ]
    for name, (label, detail) in EVENTS.items():
        sections.append({"section": label, "fields": [
            field(f"{name}_enabled", "bool", "启用此事件", description=detail),
            field(f"{name}_preset", "preset", "波形预设", description="留空时只显示事件；不会凭空选择预设。"),
            field(f"{name}_duration", "duration", "事件持续时间", min=0.05, max=300, step=0.1),
            field(f"{name}_channel", "select", "事件通道", options=[
                {"value": "inherit", "label": "跟随基础通道"}, {"value": "a", "label": "A"},
                {"value": "b", "label": "B"}, {"value": "both", "label": "双通道"},
            ]),
            field(f"{name}_strength", "percent", "事件期间最低强度",
                  description="0 表示只用基础强度；事件不叠加，取较高值，结束后回基础层。"),
            field(f"{name}_cooldown", "duration", "重复触发最短间隔", min=0, max=60, step=0.05),
        ]})
        if name == "incap":
            sections[-1]["fields"].insert(1, field("incap_sustain", "bool", "倒地期间持续反馈",
                description="持续续播短波形，扶起或死亡后停止续播；最后一段最多 0.5 秒。关闭则只触发一次。"))
    manifest = {"id": "l4d2_memory", "name": "L4D2 玩家状态（外部采集）", "version": "1.0.0", "sdk": "1",
                "author": "MMFelix", "entry": "main.py", "capabilities": {"startup_check": True},
                "description": "只读本地玩家状态；独立事件波形与生命值强度层。无隐藏或反作弊绕过功能。",
                "config_schema": sections}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # The SDK exposes sections/fields, not custom accordion renderers. This
    # preview shows the requested arrangement without changing the host UI.
    template = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>L4D2 玩家状态 · 配置预览</title><style>
:root{color-scheme:dark;font-family:"Segoe UI","Microsoft YaHei",sans-serif;color:#e9e8e3;background:#0e0e0d}*{box-sizing:border-box}body{margin:0;padding:36px 24px}main{max-width:1050px;margin:auto}h1{font-size:25px;margin:0 0 10px}p{font-size:13px;line-height:1.8;color:#b9b8b0}.note{color:#e9cd75}.top{display:flex;align-items:center;justify-content:space-between;gap:16px}button,select,input{font:inherit;color:inherit;background:#191813;border:1px solid #45402d;border-radius:9px;padding:10px}button{cursor:pointer}button:hover{border-color:#d0bb71}h2{font-size:18px;margin-top:30px}.controls{display:flex;gap:20px;align-items:center;flex-wrap:wrap}.controls label{display:grid;gap:8px;font-size:13px;color:#c4c2b5}.controls input{width:115px}select{min-width:180px}details{border:1px solid #383426;border-radius:11px;background:#141412;margin:10px 0}summary{list-style:none;display:flex;align-items:center;gap:12px;padding:17px;cursor:pointer;font-size:17px}summary::-webkit-details-marker{display:none}.label{flex:1}.chevron{color:#b9ad87}input[type=checkbox]{accent-color:#e7cd83;width:18px;height:18px;cursor:pointer}.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:15px;padding:0 18px 20px}.fields label{display:grid;gap:8px;font-size:12px;color:#bdbba9}.fields input,.fields select{width:100%;min-width:0}.description{padding:0 18px;color:#a7a496;font-size:12px}.badge{font-size:11px;border:1px solid #51482c;color:#e9cd75;border-radius:20px;padding:6px 10px}.sim{padding:18px;border:1px solid #383426;border-radius:12px;background:#171610;margin-top:28px}.sim input{accent-color:#e7cd83;width:220px;padding:0}.sim output{font-size:25px;font-variant-numeric:tabular-nums;color:#ead384}.footer{margin-top:24px;border-top:1px solid #2c2a22;padding-top:14px;color:#8d8b7f;font-size:12px}</style>
<main><div class="top"><h1>L4D2 · 玩家状态联动</h1><span class="badge">离线配置预览</span></div>
<p>参考图的独立事件布局。DGHub 实际配置由 manifest 的分区字段生成，折叠样式取决于主程序。此页面不连接设备。</p>
<h2>◎ 输出设备</h2><select disabled><option>跟随 DGHub 当前目标设备</option></select>
<h2>⚡ 生命值 → 强度层</h2><div class="controls"><label>模式<select id="mode"><option value="dynamic">动态：损失生命值</option><option value="fixed">固定强度</option><option value="challenge">挑战：累计损失</option></select></label><label>每损失 1 HP 对应百分比<input id="hp_scale" type="number" min="0" max="10" step="0.1"></label><label>强度上限<input id="strength_cap" type="number" min="0" max="100"></label><label>固定强度<input id="fixed_strength" type="number" min="0" max="100"></label></div>
<p class="note">动态模式满血为 0，治疗时降低；事件波形独立配置。波形未选择时只显示事件，不播放。采集失效时清零本插件层。</p>
<h2>🪄 触发事件（波形）</h2><p>各事件单独开关、波形、时长和通道。控制状态在开始时触发；脱离控制在所有控制关系解除时触发。</p><div id="events"></div>
<div class="sim"><div class="controls"><label>模拟当前有效生命值<input id="health" type="range" min="0" max="100" value="93"></label><span id="hptext">93 HP</span><output id="strength"></output></div><p>仅演示基础强度公式，不连接游戏或设备。挑战模式累计本次滑动中的下降值。</p></div>
<p><button id="export">导出配置参考 JSON</button> <button id="reset">恢复默认</button></p><div class="footer">MMFelix · v1.0.0 · 适配 L4D2 2.2.4.3。离线预览不会连接游戏或设备。</div></main>
<script>const definitions=__EVENTS__,defaults=__DEFAULTS__;let cfg={...defaults},challenge=0,lastHp=93;
function init(){for(const key of ['mode','hp_scale','strength_cap','fixed_strength']){let e=document.getElementById(key);e.value=cfg[key];e.onchange=()=>{cfg[key]=key==='mode'?e.value:Number(e.value);renderStrength()}}const container=document.getElementById('events');container.replaceChildren();for(const [name,[label,desc]]of Object.entries(definitions)){let row=document.createElement('details'),summary=document.createElement('summary'),title=document.createElement('span'),enabled=document.createElement('input'),arrow=document.createElement('span');title.className='label';title.textContent=label;enabled.type='checkbox';enabled.checked=cfg[name+'_enabled'];enabled.onclick=e=>e.stopPropagation();enabled.onchange=()=>cfg[name+'_enabled']=enabled.checked;arrow.className='chevron';arrow.textContent='⌄';summary.append(title,enabled,arrow);let p=document.createElement('p');p.className='description';p.textContent=desc;let fields=document.createElement('div');fields.className='fields';for(const [suffix,text,type]of [['preset','波形预设名','text'],['duration','持续时间（秒）','number'],['channel','通道','select'],['strength','事件期间最低强度（%）','number'],['cooldown','重复触发最短间隔（秒）','number']]){let l=document.createElement('label'),input=document.createElement(type==='select'?'select':'input');l.append(document.createTextNode(text));if(type==='select'){for(const [v,t]of [['inherit','跟随基础通道'],['a','A'],['b','B'],['both','双通道']]){let o=document.createElement('option');o.value=v;o.textContent=t;input.append(o)}}else{input.type=type;if(type==='number'){input.min=suffix==='duration'?'0.05':'0';input.max=suffix==='strength'?'100':suffix==='cooldown'?'60':'300';input.step='0.05'}else input.placeholder='留空：只显示事件'}input.value=cfg[name+'_'+suffix];input.onchange=()=>{cfg[name+'_'+suffix]=type==='number'?Number(input.value):input.value};l.append(input);fields.append(l)}row.append(summary,p,fields);container.append(row)}renderStrength()}
function renderStrength(){const hp=Number(document.getElementById('health').value);challenge+=Math.max(0,lastHp-hp)*cfg.hp_scale;lastHp=hp;let strength=cfg.mode==='dynamic'?(100-hp)*cfg.hp_scale:cfg.mode==='fixed'?cfg.fixed_strength:challenge;document.getElementById('hptext').textContent=hp+' HP';document.getElementById('strength').textContent=Math.round(Math.max(0,Math.min(cfg.strength_cap,strength)))+'%'}document.getElementById('health').oninput=renderStrength;document.getElementById('reset').onclick=()=>{cfg={...defaults};challenge=0;init()};document.getElementById('export').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(cfg,null,2)],{type:'application/json'}));let a=document.createElement('a');a.href=url;a.download='l4d2-settings-reference.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),500)};init();</script></html>'''
    template = template.replace("__EVENTS__", json.dumps(EVENTS, ensure_ascii=False)).replace("__DEFAULTS__", json.dumps(cfg, ensure_ascii=False))
    (root / "settings-preview.html").write_text(template, encoding="utf-8")


if __name__ == "__main__": main()
