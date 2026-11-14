const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();
for(const n of ['print','auto']){for(const [s,h,css] of [['feed',1350,''],['story',1920,'body{height:1920px}.top{top:200px}.phone{top:600px}.note{margin-top:200px}.chips{top:1220px}.foot{bottom:250px}.url{bottom:210px}']]){
const p=await b.newPage({viewport:{width:1080,height:h}});await p.goto('file://'+__dirname+'/'+n+'.html');
if(css)await p.addStyleTag({content:css});await p.waitForTimeout(1500);
await p.screenshot({path:`${__dirname}/${n}-${s}.png`});await p.screenshot({path:`${__dirname}/jpg/${n}-${s}.jpg`,type:'jpeg',quality:88});await p.close();}}
await b.close()})();
