const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'../templates/index.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements={};
for(const id of ['island','region','province','municipality','barangay','layer','status','download-frame'])elements[id]={value:'',dataset:{},addEventListener(type,fn){this[type]=fn},replaceChildren(...children){this.value=children[0]?.value||''}};
const buttons=['jpeg','zip','gpkg'].map(fmt=>({dataset:{fmt},addEventListener(type,fn){this[type]=fn}}));
let mode='error',downloaded=false,timers=0;
const context={document:{getElementById:id=>elements[id],querySelectorAll:()=>buttons,cookie:'',body:{appendChild(){}},createElement:()=>({click(){downloaded=true},remove(){}})},Option:function(text,value){this.value=value},console,URLSearchParams,URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},crypto:{randomUUID:()=> 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'},setTimeout:()=>1,setInterval:()=>{timers++;return 1},clearInterval:()=>{},Date,fetch:async(url)=>url==='/api/datasets'?{ok:true,json:async()=>({items:[{island:'mindanao',province:'camiguin',layer:'npaaad'}]})}:mode==='error'?{ok:false,json:async()=>({error:'Download interrupted by the server'})}:{ok:true,blob:async()=>({}),headers:{get:()=> 'attachment; filename="guinsiliban_npaaad.jpg"'}}};
vm.createContext(context);vm.runInContext(script,context);
setImmediate(async()=>{
 elements.island.value='mindanao';elements.region.value='Region X – Northern Mindanao';elements.layer.value='npaaad';elements.province.value='camiguin';elements.municipality.value='Guinsiliban';
 await buttons[0].click();
 assert.equal(elements.status.textContent,'Download interrupted by the server');
 assert.equal(timers,0,'JPEG should not use the cookie confirmation timer');
 mode='success';await buttons[0].click();assert(downloaded);assert(elements.status.textContent.includes('Your JPEG is ready'));
 for(const [province,municipality,barangay,scope] of [['','','','region'],['camiguin','','','province'],['camiguin','Guinsiliban','','municipality'],['camiguin','Guinsiliban','Test','barangay']]){
  Object.assign(elements.province,{value:province});elements.municipality.value=municipality;elements.barangay.value=barangay;
  await buttons[2].click();const params=new URLSearchParams(elements['download-frame'].src.split('?')[1]);assert.equal(params.get('scope'),scope);
 }
 console.log('PASS: JPEG success and HTTP errors are handled directly; GIS scope is retained at every level');
});
