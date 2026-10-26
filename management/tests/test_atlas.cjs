"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {groupFlows, quadratic, routeColor, projection, unwrapRing, pacificInset, toggledSelection, SessionAtlas} = require("../taco-console/static/atlas.js");
const world = JSON.parse(fs.readFileSync(path.join(__dirname,"../taco-console/static/world.json"), "utf8"));
const privateIP = {kind:"private",code:null}, us = {kind:"country",code:"US"};
const flow = {src:"10.1.0.10",dst:"8.8.8.8",sport:50000,dport:443,proto:"TCP",service:"HTTPS",state:"ESTABLISHED",
  scope:"AI",src_country:privateIP,dst_country:us,tx_bytes:100,rx_bytes:200};

// Aggregate sessions without mistaking an animation or a route for packet counts.
const grouped = groupFlows([flow,{...flow,sport:50001,tx_bytes:300,rx_bytes:400}],world.countries);
assert.equal(grouped.totalFlows,2);
assert.equal(grouped.totalRoutes,1);
assert.equal(grouped.routes[0].count,2);
assert.equal(grouped.routes[0].totalBytes,1000);
assert.deepEqual(grouped.routes[0].dst.point,world.countries.US.point);
assert.equal(grouped.countryCount,1);

// Management SSH remains a local-to-local route and never receives a fake country.
const ssh={...flow,src:"10.0.0.9",dst:"10.1.0.10",dport:22,service:"SSH",scope:"MANAGEMENT",dst_country:privateIP};
const inbound=groupFlows([ssh],world.countries);
assert.equal(inbound.routes[0].src.kind,"local");
assert.equal(inbound.routes[0].dst.kind,"local");
assert.equal(inbound.countryCount,0);
assert.notEqual(routeColor(ssh,"flows"),routeColor(ssh,"drops"));

// Missing country data remains unlocated rather than silently defaulting to Japan.
const unlocated=groupFlows([{...flow,dst_country:{kind:"unknown",code:null}}],world.countries);
assert.equal(unlocated.routes[0].dst.kind,"unknown");
assert.equal(unlocated.countryCount,0);
assert.equal(unlocated.totalFlows,1);

// A display cap does not change the actual number of observed sessions/routes.
const many=Array.from({length:100},(_,i)=>({...flow,dport:10000+i}));
const capped=groupFlows(many,world.countries,"flows",80);
assert.equal(capped.routes.length,80);assert.equal(capped.totalRoutes,100);assert.equal(capped.totalFlows,100);

// Finished history and denied attempts do not produce moving session particles.
assert.equal(groupFlows([{...flow,ended_at:"2026-10-26T00:00:00Z"}],world.countries,"history").routes[0].animate,false);
assert.equal(groupFlows([flow],world.countries,"drops").routes[0].animate,false);
assert.equal(groupFlows([],world.countries).routes.length,0);
const noAccounting=groupFlows([{...flow,tx_bytes:null,rx_bytes:null}],world.countries);
assert.equal(noAccounting.routes[0].totalBytes,null);
assert.deepEqual(quadratic({x:0,y:0},{x:5,y:10},{x:10,y:0},.5),{x:5,y:5});
assert.ok(world.features.length>100);assert.ok(world.countries.JP && world.countries.US && world.countries.AU);
assert.equal(world.license,"public domain");

// Japan is west of a Pacific LAN inset; the Americas remain to its east.
for(const [width,height] of [[1280,720],[1024,640],[420,800],[900,330],[720,280]]){
  const inset=pacificInset(width,height);
  assert.ok(inset.x>=0 && inset.x+inset.width<=width);
  assert.ok(inset.y>=0 && inset.y+inset.height<=height);
}
const jp=projection(world.countries.JP.point,1280,720),usPoint=projection(world.countries.US.point,1280,720),inset=pacificInset(1280,720);
assert.ok(jp.x<inset.x && inset.x+inset.width<usPoint.x);
assert.ok(jp.y<inset.y);
const dateline=unwrapRing([[170,10],[-170,10],[-170,0],[170,0]]);
for(let i=1;i<dateline.length;i++)assert.ok(Math.abs(dateline[i][0]-dateline[i-1][0])<=180);

// The actual selection method must toggle off and remain off after polling.
assert.equal(toggledSelection("route-a","route-a"),null);
const atlas=Object.create(SessionAtlas.prototype);
atlas.world=world;atlas.selected=null;
for(const name of ["paintList","paintDetail","paintSummary","layout","draw","schedule"])atlas[name]=()=>{};
const key=grouped.routes[0].key;
atlas.select(key);assert.equal(atlas.selected,key);
atlas.select(key);assert.equal(atlas.selected,null);
atlas.update([flow]);assert.equal(atlas.selected,null);
atlas.select(key);atlas.clearSelection();atlas.update([flow]);assert.equal(atlas.selected,null);
console.log("Session atlas: grouping, country map, Pacific LAN placement, dateline wrapping, selection toggle/clear, polling, and limits passed.");

// Dragging moves all map points together, survives updates, and can reset.
const pan=Object.create(SessionAtlas.prototype);
pan.world=world;pan.width=1280;pan.height=500;pan.offset={x:0,y:0};pan.model=grouped;
pan.paintBackground=()=>{};pan.draw=()=>{};
pan.layout();
const before=pan.points.get("local:10.1.0.10"), publicBefore=pan.points.get("country:US");
pan.offset={x:90,y:40};pan.layout();
assert.equal(pan.points.get("local:10.1.0.10").x-before.x,90);
assert.equal(pan.points.get("country:US").y-publicBefore.y,40);
pan.canvas={classList:{remove:()=>{}},hasPointerCapture:()=>false};
pan.resetPosition();assert.deepEqual(pan.offset,{x:0,y:0});
assert.equal(pan.points.get("local:10.1.0.10").x,before.x);
pan.drag={id:2,moved:true};pan.endPan({pointerId:2});
assert.equal(pan.suppressClick,true);assert.equal(pan.drag,null);
pan.drag={id:3,moved:false};pan.endPan({pointerId:3});assert.equal(pan.suppressClick,false);
// Every supported theme can repaint; invalid stored preferences fall back safely.
for(const theme of ["amber","cyan","violet","white","green","blue","red"]){pan.setTheme(theme);assert.equal(pan.theme,theme);}
pan.setTheme("__proto__");assert.equal(pan.theme,"amber");
pan.setTheme("cyan");assert.notEqual(pan.color("#ed9630"),"#ed9630");
console.log("Session atlas: map pan/reset, drag-versus-click, and theme validation passed.");
