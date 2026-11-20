const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();
for(const [n,w,h] of [['cover',1640,624],['profile',1000,1000]]){const p=await b.newPage({viewport:{width:w,height:h}});
await p.goto('file://'+__dirname+'/'+n+'.html');await p.waitForTimeout(1500);
await p.screenshot({path:`${__dirname}/${n}.png`});await p.screenshot({path:`${__dirname}/${n}.jpg`,type:'jpeg',quality:92});await p.close();}
await b.close()})();
