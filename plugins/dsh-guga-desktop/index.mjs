// Native DSH plugin: one official MCP connection for actions and host events.
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';

export const name='guga-desktop';
export const inject=['tools'];

// turn/end 的对外终态。只做精确 token 匹配，未知或空原因一律 waiting，绝不当成 finished。
// 真实安装版 DSH 0.2.1-rc.5 的 TurnEndReasonMap 为 completed/aborted/blocked/error/
// max-tokens/interrupted/forked；其余 token 来自任务书与可扩展的历史字符串。
const TURN_END_STATES=Object.freeze({
  completed:'finished',
  aborted:'cancelled',cancelled:'cancelled',canceled:'cancelled',interrupted:'cancelled',
  failed:'failed',error:'failed',
  suspended:'waiting',needs_input:'waiting',waiting:'waiting',yielded:'waiting',
  blocked:'waiting','max-tokens':'waiting',forked:'waiting'
});
const TURN_END_TAILS=Object.freeze({
  finished:'仅代表本轮执行结束，不是验收通过。',
  cancelled:'本轮被取消或中断，不能当作完成。',
  failed:'本轮失败，不能当作完成。',
  waiting:'本轮等待输入、挂起或让出，尚未判定完成。'
});
const UNKNOWN_REASON_TAIL='结束原因待核对，尚未判定完成。';

function turnEndText(reason){
  let raw='';
  if(typeof reason==='string')raw=reason;
  else if(reason&&typeof reason==='object'&&typeof reason.kind==='string')raw=reason.kind;
  return String(raw).replace(/[\u0000-\u001f\u007f]+/g,' ').replace(/\s+/g,' ').trim().slice(0,64);
}

function turnEndKey(text){
  // 只做明确要求的去空白与大小写兼容；不提取前缀，未知后缀仍是未知原因。
  return text.toLowerCase();
}

function knownTurnEndState(key){
  // 只接受表自身属性：constructor/__proto__ 等原型链 key 必须落到 waiting。
  return Object.prototype.hasOwnProperty.call(TURN_END_STATES,key)?TURN_END_STATES[key]:'waiting';
}

/** 从对象读取 kind、字符串沿用其值；有界、无敏感内容，不产生 [object Object]。 */
export function normalizeTurnEndReason(reason){
  const text=turnEndText(reason);
  return text.toLowerCase()==='[object object]'?'':text;
}

/** 结束原因到对外状态；完整原因值精确映射，空值/未知对象或未知字符串返回 waiting。 */
export function turnEndState(reason){
  return knownTurnEndState(turnEndKey(normalizeTurnEndReason(reason)));
}

/** 有界可读摘要；未知/空原因必须明确写出“结束原因待核对，尚未判定完成”。 */
export function turnEndSummary(reason,previous={}){
  const text=normalizeTurnEndReason(reason);
  const key=turnEndKey(text);
  const known=Object.prototype.hasOwnProperty.call(TURN_END_STATES,key);
  const tail=known?TURN_END_TAILS[TURN_END_STATES[key]]:UNKNOWN_REASON_TAIL;
  const prior=previous.summary?.startsWith('DSH 的公开答复：')?previous.summary:'';
  return 'DSH 本轮已结束（'+(text||'未知')+'）。'+tail+prior;
}

/** 模型可见注册路径：宿主内部事件通道永不注册；其余定义由服务端允许清单决定。 */
export function modelFacingToolDefinitions(definitions){
  return (Array.isArray(definitions)?definitions:[]).filter(tool=>tool&&typeof tool.name==='string'&&tool.name!=='dsh_host_event');
}

export function projectEvent(session,event,previous={}) {
  if(event.surfaceOp && event.surfaceOp!=='append')return null;
  const data=event.data??{};
  const next={...previous,session_id:String(session.id),event_id:randomUUID(),kind:event.type};
  if(session.header?.parentSession)next.parent_session_id=String(session.header.parentSession);
  switch(event.type){
    case 'user/message':
      next.goal=(Array.isArray(data.content)?data.content:[]).filter(x=>x.type==='text').map(x=>x.text).join('\n').slice(0,500);
      return next;
    case 'turn/start':return {...next,state:'running',turn:data.turn,step:0,tool:'',summary:'DSH 已开始处理这个会话的任务'};
    case 'step/start':return {...next,state:'running',step:data.step,summary:'DSH 正在处理第 '+String(data.step)+' 步'};
    case 'tool/call':return {...next,state:'running',tool:String(data.name??'').slice(0,100),summary:'DSH 正在调用工具：'+String(data.name??'').slice(0,100)};
    case 'tool/result':return {...next,state:'running',summary:data.message?.isError?'上一次工具返回了错误，DSH 尚未结束任务':'上一次工具已经返回；DSH 仍在处理任务'};
    case 'assistant/message':{
      const visible=(data.message?.content??[]).filter(x=>x.type==='text').map(x=>x.text).join('\n').trim();
      return visible?{...next,state:'running',summary:'DSH 的公开答复：'+visible.slice(0,400)}:null;
    }
    case 'turn/end':return {...next,state:turnEndState(data.reason),reason:normalizeTurnEndReason(data.reason),
      summary:turnEndSummary(data.reason,previous)};
    default:return null; // Never forward reasoning, raw tool output, audio or full transcripts.
  }
}

export async function apply(ctx,config={}){
  if(!config.projectRoot||!config.python||!config.runtimeAnchor)throw new Error('咕嘎插件缺少本机连接配置，请运行 setup_dsh_guga_plugin.py --native');
  const require=createRequire(config.runtimeAnchor);
  const load=async id=>import(pathToFileURL(require.resolve(id)).href);
  const {Client}=await load('@modelcontextprotocol/client');
  const {StdioClientTransport}=await load('@modelcontextprotocol/client/stdio');
  const {createMcpToolDefinition}=await load('@deepseek-ai/dsh-mcp-client');
  const client=new Client({name:'dsh-guga-desktop',version:'0.1.0'});
  const transport=new StdioClientTransport({command:config.python,args:[join(config.projectRoot,'tools/guga_mcp_server.py'),'--plugin-events'],cwd:config.projectRoot,
    env:config.pipeName?{GUGA_MCP_PIPE:config.pipeName}:undefined});
  let disposed=false;
  ctx.effect(()=>()=>{disposed=true;return client.close();});
  await client.connect(transport);
  const definitions=(await client.listTools()).tools;
  for(const tool of modelFacingToolDefinitions(definitions)){
    const dispose=ctx.tools.register(createMcpToolDefinition(ctx,{
      name:'mcp__guga__'+tool.name,rawName:tool.name,description:tool.description??'',inputSchema:tool.inputSchema,
      outputSchema:tool.outputSchema,taskRequired:false,
      call:(args,execution)=>client.callTool({name:tool.name,arguments:args},{signal:execution.signal,timeout:50000,toolDefinition:tool})
    }));
    ctx.effect(()=>dispose);
  }
  const sessions=new Map(),pending=new Map();
  let sending=false;
  const flush=async()=>{
    if(sending||disposed)return;
    sending=true;
    try{
      for(const [key,value] of pending){
        pending.delete(key);
        try{
          const result=await client.callTool({name:'dsh_host_event',arguments:value},{timeout:3000});
          if(result.isError)ctx.logger?.warn?.('咕嘎没有接受本次进度回执');
        }catch{ctx.logger?.warn?.('咕嘎暂未连接，进度未送达；不会自动重放电脑操作');}
      }
    }finally{sending=false;}
  };
  const queue=value=>{
    const key=value.session_id+':'+String(value.turn??'')+':'+(value.state==='running'?'progress':value.kind);
    pending.set(key,value);
    while(pending.size>32)pending.delete(pending.keys().next().value);
    void flush();
  };
  queue({session_id:'_host_',kind:'host/heartbeat',event_id:randomUUID()});
  ctx.on('session/event',(session,event)=>{
    const previous=sessions.get(String(session.id))??{};
    const next=projectEvent(session,event,previous);
    if(!next)return;
    sessions.set(String(session.id),next);
    while(sessions.size>16)sessions.delete(sessions.keys().next().value);
    queue(next);
  });
  const heartbeat=setInterval(()=>{
    queue({session_id:'_host_',kind:'host/heartbeat',event_id:randomUUID()});
    for(const value of sessions.values())if(value.state==='running')queue({...value,kind:'heartbeat',event_id:randomUUID()});
  },15000);
  heartbeat.unref?.();ctx.effect(()=>()=>clearInterval(heartbeat));
}
