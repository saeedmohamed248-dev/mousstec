const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();
for(const n of ['print','auto','market']){for(const [s,h] of [['feed',1350],['story',1920]]){
const p=await b.newPage({viewport:{width:1080,height:h}});await p.goto('file://'+__dirname+'/'+n+'.html');
if(s=='story')await p.addStyleTag({content:'body{height:1920px}.wrap{padding-top:260px;padding-bottom:300px}.sticker{top:380px}.cmyk{top:266px!important}'});
await p.waitForTimeout(1500);await p.screenshot({path:`${__dirname}/${n}-${s}.png`});await p.close();}}
await b.close()})();
