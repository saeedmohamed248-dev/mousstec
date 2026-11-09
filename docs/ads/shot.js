const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();
for(const n of ['print','auto','parts','market','brand']){for(const [s,h] of [['feed',1350],['story',1920]]){
const p=await b.newPage({viewport:{width:1080,height:h}});await p.goto('file://'+__dirname+'/'+n+'.html');
if(s=='story')await p.addStyleTag({content:'body{height:1920px}.wrap{padding-top:230px;padding-bottom:260px}.sticker{top:380px}.biz .wrap{padding:230px 60px 250px}.biz .sticker{top:330px}.biz .g p{font-size:23px}.biz .g{padding:16px 20px}.biz .groups{gap:16px}.biz h1{font-size:76px}'});
await p.waitForTimeout(1500);await p.screenshot({path:`${__dirname}/${n}-${s}.png`});await p.close();}}
await b.close()})();
